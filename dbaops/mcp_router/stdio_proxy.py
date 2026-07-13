"""stdio MCP 서버 6개를 subprocess 로 spawn 해 tools/list · tools/call 을 프록시.

mcp_tools/*/handler.py 가 mcp_lambda 의 StdioServerAdapterRequestHandler 로 하던 일을
router 가 직접 한다: `mcp` python SDK 의 stdio_client + ClientSession 으로 자식 MCP 서버에
붙어 핸드셰이크 후, 그 서버의 tools 를 '<target>___<tool>' namespace 로 노출하고
tools/call 을 그대로 위임한다.

MCP SDK 는 async 이고 stdio 세션은 단일 task group 안에서 살아야 하므로,
전용 백그라운드 이벤트 루프 스레드 1개에서 모든 세션을 운영하고,
동기 호출자(HTTP 핸들러)는 run_coroutine_threadsafe 로 결과를 받는다.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

# 자식 서버 핸드셰이크/응답 대기 한도
_CALL_TIMEOUT = 60.0
_CONNECT_TIMEOUT = 45.0


class _Session:
    """단일 stdio MCP 서버 세션 (이벤트 루프 스레드 안에서만 접근)."""

    def __init__(self, target: str, spec: dict[str, Any]) -> None:
        self.target = target
        self.spec = spec
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[dict[str, Any]] = []

    async def connect(self) -> None:
        params = StdioServerParameters(
            command=self.spec["command"],
            args=self.spec.get("args", []),
            env=self.spec.get("env", {}),
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [
            {
                "name": f"{self.target}___{t.name}",
                "description": (t.description or "").strip() or f"Call {t.name}",
                "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
            }
            for t in listed.tools
        ]
        logger.info("stdio session connected: %s (%d tools)", self.target, len(self.tools))

    async def call(self, sub_tool: str, args: dict[str, Any]) -> Any:
        if self.session is None:
            raise RuntimeError(f"session {self.target} not connected")
        result = await self.session.call_tool(sub_tool, args)
        # MCP CallToolResult → 우리 mcp_client 가 기대하는 형태로:
        # content[0].text 가 JSON 문자열이면 그대로 텍스트로 넘기면 client 가 파싱한다.
        out_content = []
        for c in result.content:
            if getattr(c, "type", None) == "text":
                out_content.append({"type": "text", "text": c.text})
            else:
                out_content.append({"type": getattr(c, "type", "unknown"),
                                    "data": getattr(c, "data", None)})
        return {"content": out_content, "isError": bool(result.isError)}

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("error closing session %s: %s", self.target, e)
        self._stack = None
        self.session = None


class StdioProxy:
    """모든 stdio 세션을 단일 이벤트 루프 스레드에서 운영하는 동기 facade."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._sessions: dict[str, _Session] = {}
        self._specs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="stdio-proxy")
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ─────────── 동기 API (HTTP 핸들러가 호출) ───────────

    def ensure(self, target: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        """세션 보장 + tools 반환. spec 이 바뀌었으면 재연결."""
        with self._lock:
            existing = self._sessions.get(target)
            if existing and self._specs.get(target) == spec:
                return existing.tools
            # 기존 세션 정리
            if existing:
                self._submit(existing.close()).result(timeout=20)
            sess = _Session(target, spec)
            try:
                self._submit(sess.connect()).result(timeout=_CONNECT_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                logger.error("stdio connect failed for %s: %s", target, e)
                try:
                    self._submit(sess.close()).result(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._sessions[target] = sess
            self._specs[target] = spec
            return sess.tools

    def list_tools(self, target: str) -> list[dict[str, Any]]:
        sess = self._sessions.get(target)
        return sess.tools if sess else []

    def call(self, target: str, sub_tool: str, args: dict[str, Any],
             timeout: float = _CALL_TIMEOUT) -> Any:
        sess = self._sessions.get(target)
        if sess is None:
            raise RuntimeError(f"stdio target '{target}' not connected")
        return self._submit(sess.call(sub_tool, args)).result(timeout=timeout)

    def disconnect(self, target: str) -> None:
        with self._lock:
            sess = self._sessions.pop(target, None)
            self._specs.pop(target, None)
        if sess:
            try:
                self._submit(sess.close()).result(timeout=20)
            except Exception:  # noqa: BLE001
                pass

    def connected_targets(self) -> list[str]:
        return list(self._sessions.keys())
