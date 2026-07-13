"""Agent invoke wrapper — AgentCore Runtime 또는 직접 HTTP(올인원 EC2).

두 가지 백엔드:
  - AGENT_HTTP_URL 이 설정되면 (올인원 EC2 / docker compose): boto3·AgentCore 없이
    같은 박스의 agent:8080/invocations 를 urllib 로 직접 POST. NDJSON 스트리밍 그대로.
  - 아니면 (기존 AgentCore 배포): boto3 invoke_agent_runtime.

Streamlit UI 와 Slack 봇이 이 모듈을 공유한다.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Iterator

logger = logging.getLogger(__name__)

REGION = os.environ.get("BEDROCK_REGION", "ap-northeast-2")
RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
SERVICE_NAME = os.environ.get("AGENTCORE_SERVICE_NAME", "bedrock-agentcore")
# swarm streaming 은 LLM 호출이 길게 이어져 read 사이 60s+ 공백이 흔하다 — 충분히 길게.
READ_TIMEOUT = int(os.environ.get("AGENTCORE_READ_TIMEOUT", "900"))
CONNECT_TIMEOUT = int(os.environ.get("AGENTCORE_CONNECT_TIMEOUT", "10"))

# 올인원 EC2: agent HTTP 엔드포인트 직접 호출 (예: http://agent:8080/invocations)
AGENT_HTTP_URL = os.environ.get("AGENT_HTTP_URL", "")


_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        cfg = Config(
            read_timeout=READ_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        _client = boto3.client(SERVICE_NAME, region_name=REGION, config=cfg)
        if not hasattr(_client, "invoke_agent_runtime"):
            raise RuntimeError(
                f"boto3 service '{SERVICE_NAME}' has no invoke_agent_runtime — "
                f"upgrade boto3 (current={boto3.__version__})"
            )
    return _client


def _http_invoke(request: dict[str, Any]) -> dict[str, Any]:
    """올인원 EC2 — agent:8080/invocations 직접 POST (동기, JSON 한 객체)."""
    payload = json.dumps({"request": request}).encode()
    req = urllib.request.Request(
        AGENT_HTTP_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        logger.exception("agent http invoke failed")
        return {"error": f"agent HTTP invoke failed: {e!r}"}


def _http_invoke_stream(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """올인원 EC2 — agent:8080/invocations 직접 POST + NDJSON 스트리밍 파싱."""
    body_bytes = json.dumps({"request": {**request, "stream": True}}).encode()
    req = urllib.request.Request(
        AGENT_HTTP_URL, data=body_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        logger.exception("agent http stream failed")
        yield {"type": "error", "error": f"agent HTTP invoke failed: {e!r}"}
        return

    buf = b""
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1:]
                if not line.strip():
                    continue
                try:
                    yield json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    logger.warning("ndjson parse failed: %s | %s", e, line[:200])
        tail = buf.strip()
        if tail:
            try:
                yield json.loads(tail.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.exception("agent http stream read failed")
        yield {"type": "error", "error": f"stream read failed: {e!r}"}


def invoke(request: dict[str, Any]) -> dict[str, Any]:
    """단발 호출 — 응답 전체를 한 번에 받음 (fast 모드용)."""
    if AGENT_HTTP_URL:
        return _http_invoke(request)
    if not RUNTIME_ARN:
        return {"error": "AGENTCORE_RUNTIME_ARN env not set"}

    payload = json.dumps({"request": request}).encode()
    try:
        client = _get_client()
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            payload=payload,
            contentType="application/json",
        )
        body = resp.get("response") or resp.get("body")
        if hasattr(body, "read"):
            body = body.read()
        return json.loads(body) if body else {}
    except Exception as e:  # noqa: BLE001
        logger.exception("invoke_agent_runtime failed")
        return {"error": f"AgentCore invoke failed: {e!r}"}


def invoke_stream(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """NDJSON streaming 호출 — 한 줄당 한 이벤트 yield (swarm 모드용).

    Runtime 컨테이너가 application/x-ndjson 으로 chunked 응답하면 boto3 StreamingBody 가
    그대로 chunk 를 노출하므로 줄 단위로 파싱한다.
    """
    if AGENT_HTTP_URL:
        yield from _http_invoke_stream(request)
        return
    if not RUNTIME_ARN:
        yield {"type": "error", "error": "AGENTCORE_RUNTIME_ARN env not set"}
        return

    body_bytes = json.dumps({"request": {**request, "stream": True}}).encode()
    try:
        client = _get_client()
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN,
            payload=body_bytes,
            contentType="application/json",
            accept="application/x-ndjson",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("invoke_agent_runtime (stream) failed")
        yield {"type": "error", "error": f"AgentCore invoke failed: {e!r}"}
        return

    body = resp.get("response") or resp.get("body")
    if body is None:
        yield {"type": "error", "error": "empty response"}
        return

    buf = b""
    try:
        for chunk in body.iter_chunks() if hasattr(body, "iter_chunks") else iter(lambda: body.read(4096), b""):
            if not chunk:
                continue
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1:]
                if not line.strip():
                    continue
                try:
                    yield json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    logger.warning("ndjson parse failed: %s | %s", e, line[:200])
        # tail (no trailing newline)
        tail = buf.strip()
        if tail:
            try:
                yield json.loads(tail.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                pass
    except Exception as e:  # noqa: BLE001
        logger.exception("stream read failed")
        yield {"type": "error", "error": f"stream read failed: {e!r}"}
