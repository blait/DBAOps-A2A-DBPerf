"""MCP Router HTTP 서버 — AgentCore Gateway 대체.

에이전트의 tools/mcp_client.py 가 GATEWAY_ENDPOINT 로 보내는 MCP JSON-RPC 를 그대로 처리:
  POST /mcp   {"jsonrpc":"2.0","id":..,"method":"tools/list","params":{...}}
              {"jsonrpc":"2.0","id":..,"method":"tools/call","params":{"name":..,"arguments":..}}
  GET  /healthz                 → 전체 target 상태 (tools/list 도달만 확인, 빠름)
  GET  /healthz?tool=<target>   → 특정 target 상태
  GET  /healthz?tool=<t>&verify=1 → DB/Prometheus 는 실제 probe 쿼리로 진짜 연결 확인 (UI 연결테스트)

응답은 Gateway 와 동일하게 result.content[0].text(JSON 문자열) 형태로 돌려준다 —
mcp_client._invoke 가 그 text 를 json.loads 한다.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .registry import get_registry

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _jsonrpc_result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _as_text_content(obj) -> dict:
    """임의 결과 → MCP result(content[0].text=JSON). 이미 content 형태면 그대로."""
    if isinstance(obj, dict) and "content" in obj:
        return obj
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A002
        logger.info("%s - %s", self.client_address[0], fmt % args)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/healthz", "/ping"):
            qs = parse_qs(parsed.query)
            target = (qs.get("tool") or [None])[0]
            verify = (qs.get("verify") or ["0"])[0] in ("1", "true", "yes")
            try:
                status = get_registry().health(target, verify=verify)
                self._send(200, {"status": "ok", "targets": status})
            except Exception as e:  # noqa: BLE001
                logger.exception("health failed")
                self._send(500, {"status": "error", "error": str(e)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in ("/mcp", "/"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, _jsonrpc_error(None, -32700, "parse error"))
            return

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}
        reg = get_registry()

        try:
            if method == "tools/list":
                tools = reg.list_tools()
                # Gateway 는 nextCursor 로 페이지네이션하지만 우리는 한 번에 다 준다.
                self._send(200, _jsonrpc_result(req_id, {"tools": tools}))
                return

            if method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not name:
                    self._send(200, _jsonrpc_error(req_id, -32602, "missing tool name"))
                    return
                result = reg.call_tool(name, arguments)
                self._send(200, _jsonrpc_result(req_id, _as_text_content(result)))
                return

            if method in ("initialize", "ping"):
                self._send(200, _jsonrpc_result(req_id, {"ok": True}))
                return

            self._send(200, _jsonrpc_error(req_id, -32601, f"method not found: {method}"))
        except Exception as e:  # noqa: BLE001
            logger.exception("rpc %s failed", method)
            self._send(200, _jsonrpc_error(req_id, -32603, f"{type(e).__name__}: {e}"))


def serve(host: str = "0.0.0.0", port: int | None = None) -> None:
    port = port or int(os.environ.get("MCP_ROUTER_PORT", "9000"))
    # 커스텀 핸들러(rds_pi/msk/s3/aws_api)는 import 시 boto3.client() 를 호출하므로
    # region 이 반드시 환경에 있어야 한다. AWS_REGION 만 있고 DEFAULT 가 없으면 보강.
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        os.environ.setdefault("AWS_DEFAULT_REGION", region)
        os.environ.setdefault("AWS_REGION", region)
    # 시작 시 registry 초기화 (stdio 세션 lazy connect)
    get_registry()
    srv = ThreadingHTTPServer((host, port), _Handler)
    logger.info("mcp-router serving on %s:%d", host, port)
    srv.serve_forever()


if __name__ == "__main__":
    serve()
