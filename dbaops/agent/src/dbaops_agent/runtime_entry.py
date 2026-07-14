"""AgentCore Runtime entrypoint — pipeline / single mode.

AgentCore Runtime 은 컨테이너 내부에서 :8080/invocations 로 들어오는 POST 를 처리.
응답 형태:
- 기본: 동기 호출, JSON 한 객체 반환
- (Accept: application/x-ndjson 또는 request.stream=true) → NDJSON streaming

Modes:
- pipeline (default) : 3 도메인 단일 에이전트 + validation + report. request.domain 필요.
- single             : 모든 도구 풀 평탄화한 1 명 에이전트 (비교용).
- swarm              : 폐기됨 — 명시적 에러.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def handler(event: dict, context: Any | None = None) -> dict:
    logger.info("invoke: %s", json.dumps(event)[:500])
    request = event.get("request") or {}
    mode = (request.get("mode") or "pipeline").lower()

    if mode == "swarm":
        return {
            "error":   "mode=swarm has been removed in the pipeline migration. Use mode=pipeline with a domain field.",
            "request": request,
        }

    if mode == "single":
        from .single_graph import invoke_single
        try:
            result = invoke_single(
                request,
                recursion_limit=int(os.environ.get("SINGLE_RECURSION_LIMIT", "80")),
            )
            return {"swarm": result, "request": request}
        except Exception as e:  # noqa: BLE001
            logger.exception("single invoke failed")
            return {"error": str(e), "request": request}

    # default: pipeline
    from .pipeline_graph import invoke_pipeline, domain_keys
    domain = request.get("domain")
    if domain not in domain_keys():
        return {
            "error":   f"mode=pipeline requires request.domain in {domain_keys()}, got {domain!r}",
            "request": request,
        }
    try:
        result = invoke_pipeline(request)
        return {"swarm": result, "request": request}  # UI 호환 — 응답 키 이름은 'swarm' 유지
    except Exception as e:  # noqa: BLE001
        logger.exception("pipeline invoke failed")
        return {"error": str(e), "request": request}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        logger.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self):  # noqa: N802
        if self.path in ("/ping", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_response(404)
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def _respond_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _start_ndjson(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Stream", "ndjson")
        self.end_headers()

    def _write_chunk(self, payload: bytes) -> None:
        self.wfile.write(f"{len(payload):X}\r\n".encode())
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _end_chunked(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    def _stream_iterator(self, gen) -> None:
        self._start_ndjson()
        try:
            for ev in gen:
                line = (json.dumps(ev, ensure_ascii=False, default=str) + "\n").encode()
                self._write_chunk(line)
        except Exception as e:  # noqa: BLE001
            logger.exception("stream failed")
            err = (json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n").encode()
            try:
                self._write_chunk(err)
            except Exception:
                pass
        finally:
            self._end_chunked()

    def do_POST(self):  # noqa: N802
        if self.path not in ("/invocations", "/invoke"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            event = self._read_body()
            request = event.get("request") or {}
            stream_requested = (
                request.get("stream") is True
                or "ndjson" in (self.headers.get("Accept") or "").lower()
            )

            if stream_requested:
                mode = (request.get("mode") or "pipeline").lower()
                if mode == "swarm":
                    err = {"type": "error", "error": "mode=swarm has been removed; use mode=pipeline."}
                    self._start_ndjson()
                    self._write_chunk((json.dumps(err) + "\n").encode())
                    self._end_chunked()
                    return
                if mode == "single":
                    from .single_graph import iter_single
                    self._stream_iterator(iter_single(
                        request,
                        recursion_limit=int(os.environ.get("SINGLE_RECURSION_LIMIT", "80")),
                    ))
                else:
                    from .pipeline_graph import iter_pipeline
                    self._stream_iterator(iter_pipeline(request))
                return

            result = handler(event)
            self._respond_json(200, result)
        except Exception as e:
            logger.exception("invoke failed")
            try:
                self._respond_json(500, {"error": str(e)})
            except Exception:
                pass


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    # 레거시 단독 서버 — 정식 진입점은 dbaops/a2a_server.py (A2A + /invocations 통합, 한 포트)
    srv = ThreadingHTTPServer((host, port), _Handler)
    logger.info("serving on %s:%d", host, port)
    srv.serve_forever()


def main(argv: list[str]) -> int:
    if "--once" in argv:
        out = handler({"request": {"mode": "pipeline", "domain": "os_metric", "free_text": "smoke"}})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    serve(port=int(os.environ.get("PORT", "8080")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
