"""MCP client → AgentCore Gateway.

retry + tool_budget + dedup cache + Cognito JWT 인증.
ToolNode 미사용.

env:
  GATEWAY_ENDPOINT (필수)
  COGNITO_TOKEN_URL  — https://<domain>.auth.<region>.amazoncognito.com/oauth2/token
  COGNITO_CLIENT_ID
  COGNITO_CLIENT_SECRET
  COGNITO_SCOPE      (default "dbaops-gateway/invoke")
  GATEWAY_BEARER_TOKEN — 정적 토큰을 직접 주입 (위 셋 대신 사용)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


def _cache_key(tool: str, params: dict[str, Any]) -> str:
    payload = json.dumps(params, sort_keys=True, default=str)
    return f"{tool}:{hashlib.sha1(payload.encode()).hexdigest()}"


class _CognitoTokenProvider:
    """client_credentials 흐름 토큰 발급 + 만료 캐시."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._exp: float = 0.0

    def _enabled(self) -> bool:
        return all(
            os.environ.get(k)
            for k in ("COGNITO_TOKEN_URL", "COGNITO_CLIENT_ID", "COGNITO_CLIENT_SECRET")
        )

    def get(self) -> str | None:
        static = os.environ.get("GATEWAY_BEARER_TOKEN")
        if static:
            return static
        if not self._enabled():
            return None
        with self._lock:
            now = time.time()
            if self._token and now < self._exp - 30:
                return self._token
            self._refresh()
            return self._token

    def _refresh(self) -> None:
        url = os.environ["COGNITO_TOKEN_URL"].rstrip("/")
        client_id = os.environ["COGNITO_CLIENT_ID"]
        client_secret = os.environ["COGNITO_CLIENT_SECRET"]
        scope = os.environ.get("COGNITO_SCOPE", "dbaops-gateway/invoke")

        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": scope}
        ).encode()
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        self._token = data["access_token"]
        self._exp = time.time() + int(data.get("expires_in", 3600))
        logger.info("refreshed cognito token (exp in %ds)", int(self._exp - time.time()))


_TOKENS = _CognitoTokenProvider()


class MCPClient:
    """AgentCore Gateway MCP JSON-RPC 호출 클라이언트.

    Phase 1: GATEWAY_ENDPOINT 가 비어있으면 stub 반환.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
    ) -> None:
        self.endpoint = (endpoint or os.environ.get("GATEWAY_ENDPOINT", "")).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def call(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        cache: dict[str, Any] | None = None,
        budget: list[int] | None = None,
    ) -> Any:
        if cache is not None:
            key = _cache_key(tool, params)
            if key in cache:
                logger.debug("cache hit %s", key)
                return cache[key]
        if budget is not None:
            if budget[0] <= 0 and os.environ.get("DBAOPS_IGNORE_BUDGET", "").lower() not in ("1", "true", "yes"):
                raise RuntimeError(f"tool_budget exhausted on {tool}")
            budget[0] -= 1

        result = self._invoke(tool, params)

        if cache is not None:
            cache[_cache_key(tool, params)] = result
        return result

    def list_tools(self, *, max_pages: int = 20) -> list[dict[str, Any]]:
        """MCP `tools/list` 호출 — Gateway 가 노출하는 모든 도구를 페이지네이션으로 모두 수집.

        AgentCore Gateway 는 한 페이지당 ~30 도구를 보내고 nextCursor 로 이어진다.
        max_pages 까지 follow.
        """
        if not self.endpoint:
            logger.warning("GATEWAY_ENDPOINT empty — returning empty tool list")
            return []

        all_tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(max_pages):
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            body = json.dumps({
                "jsonrpc": "2.0",
                "id":      f"tools-list-{page}",
                "method":  "tools/list",
                "params":  params,
            }).encode()
            headers = {"Content-Type": "application/json"}
            token = _TOKENS.get()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            last_err: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = resp.read()
                    data = json.loads(raw)
                    if isinstance(data, dict) and "error" in data:
                        raise RuntimeError(f"MCP tools/list error: {data['error']}")
                    result = data.get("result", data) if isinstance(data, dict) else data
                    tools = (result or {}).get("tools") if isinstance(result, dict) else None
                    if isinstance(tools, list):
                        all_tools.extend(tools)
                    cursor = (result or {}).get("nextCursor") if isinstance(result, dict) else None
                    last_err = None
                    break
                except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                    last_err = e
                    wait = 0.5 * (2**attempt)
                    logger.warning("tools/list page=%d attempt %d failed: %s (retry in %.1fs)", page, attempt + 1, e, wait)
                    time.sleep(wait)
            if last_err:
                raise RuntimeError(f"tools/list failed at page {page}: {last_err}")
            if not cursor:
                break

        logger.info("tools/list collected %d tools across %d pages", len(all_tools), page + 1)
        return all_tools

    def _invoke(self, tool: str, params: dict[str, Any]) -> Any:
        if not self.endpoint:
            logger.warning("GATEWAY_ENDPOINT empty — returning stub for %s", tool)
            return {"tool": tool, "params": params, "stub": True}

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": _cache_key(tool, params),
                "method": "tools/call",
                "params": {"name": tool, "arguments": params},
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        token = _TOKENS.get()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                logger.info("MCP %s raw=%s", tool, raw[:600])
                data = json.loads(raw)
                if isinstance(data, dict) and "error" in data:
                    raise RuntimeError(f"MCP error: {data['error']}")
                # MCP tools/call 결과는 result.content[0].text 안에 JSON 문자열로 들어오는 경우가 많다.
                result = data.get("result", data)
                content = result.get("content") if isinstance(result, dict) else None
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    text = content[0].get("text")
                    if isinstance(text, str):
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return {"raw": text}
                return result
            except urllib.error.HTTPError as e:
                last_err = e
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:600]
                except Exception:
                    err_body = "<unreadable>"
                wait = 0.5 * (2**attempt)
                logger.warning("MCP call %s HTTP %s attempt %d body=%s (retry in %.1fs)", tool, e.code, attempt + 1, err_body, wait)
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
                last_err = e
                wait = 0.5 * (2**attempt)
                logger.warning("MCP call %s attempt %d failed: %s (retry in %.1fs)", tool, attempt + 1, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"MCP call {tool} failed after {self.max_retries + 1} attempts: {last_err}")
