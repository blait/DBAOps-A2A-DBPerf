"""도구 카탈로그 통합 — 커스텀 4 + stdio 6. connections.json 변경 시 stdio 세션 재구성.

AgentCore Gateway 의 역할을 router 안에서 재현:
  - tools/list  : enabled 된 모든 target 의 도구를 '<target>___<tool>' 이름으로 합쳐 반환
  - tools/call  : 이름에서 target 을 분리해 커스텀 핸들러 또는 stdio 세션으로 위임

connections.json 의 mtime 을 매 요청 직전에 확인해, 바뀌었으면 stdio 세션을 lazy 재구성한다.
(UI 가 연결설정을 저장하면 다음 호출부터 자동 반영.)
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import connections
from .custom_tools import CustomToolHost
from .stdio_proxy import StdioProxy

logger = logging.getLogger(__name__)


def _probe_err_text(res: dict[str, Any]) -> str:
    """MCP tools/call 결과의 content[].text 를 이어붙여 반환(에러 판정용)."""
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
    return " ".join(parts).strip()


class Registry:
    def __init__(self) -> None:
        self._custom = CustomToolHost()
        self._stdio = StdioProxy()
        self._lock = threading.Lock()
        self._cfg: dict[str, Any] = {}
        self._cfg_mtime: float = -1.0
        self.reload()

    # ─────────── config 동기화 ───────────

    def reload(self, force: bool = False) -> None:
        """connections.json 이 바뀌었으면 다시 읽고 stdio 세션 재구성."""
        m = connections.mtime()
        if not force and m == self._cfg_mtime:
            return
        with self._lock:
            # double-check under lock
            m = connections.mtime()
            if not force and m == self._cfg_mtime:
                return
            cfg = connections.load()
            self._cfg = cfg
            self._cfg_mtime = m
            region = cfg.get("aws_region", "ap-northeast-2")

            enabled = set(connections.enabled_targets(cfg))

            # stdio: enabled 인 것만 연결, 아닌 것은 끊기.
            for target in connections.STDIO_TARGETS:
                conf = cfg["tools"].get(target, {})
                if target in enabled:
                    spec = connections.stdio_spec(target, conf, region)
                    if spec is None:
                        logger.warning("stdio %s enabled but spec incomplete — skip", target)
                        self._stdio.disconnect(target)
                        continue
                    try:
                        self._stdio.ensure(target, spec)
                    except Exception as e:  # noqa: BLE001
                        logger.error("stdio %s connect error: %s", target, e)
                else:
                    self._stdio.disconnect(target)
            logger.info("registry reloaded — enabled targets: %s", sorted(enabled))

    # ─────────── MCP 메서드 ───────────

    def list_tools(self) -> list[dict[str, Any]]:
        self.reload()
        cfg = self._cfg
        enabled = set(connections.enabled_targets(cfg))
        tools: list[dict[str, Any]] = []

        for target in connections.CUSTOM_TARGETS:
            if target in enabled:
                try:
                    tools.extend(self._custom.list_tools(target))
                except Exception as e:  # noqa: BLE001
                    logger.error("custom list_tools %s failed: %s", target, e)

        for target in connections.STDIO_TARGETS:
            if target in enabled:
                tools.extend(self._stdio.list_tools(target))

        return tools

    def call_tool(self, full_name: str, args: dict[str, Any]) -> Any:
        self.reload()
        if "___" not in full_name:
            raise ValueError(f"tool name missing namespace: {full_name}")
        target, sub_tool = full_name.split("___", 1)

        if target in connections.CUSTOM_TARGETS:
            return self._custom.call(target, sub_tool, args)
        if target in connections.STDIO_TARGETS:
            return self._stdio.call(target, sub_tool, args)
        raise ValueError(f"unknown target: {target}")

    # ─────────── health ───────────

    # target → (sub_tool, args). 실제 연결을 검증하는 가벼운 read-only probe.
    # verify=True 일 때만 사용. 여기 없는 stdio target 은 tools/list 도달만 확인.
    _PROBES: dict[str, tuple[str, dict[str, Any]]] = {
        "community-postgres":   ("execute_sql", {"sql": "SELECT 1"}),
        "community-mysql":      ("mysql_query", {"sql": "SELECT 1"}),
        "community-prometheus": ("execute_query", {"query": "up"}),
    }
    _PROBE_TIMEOUT = 15.0

    def _probe(self, t: str) -> dict[str, Any] | None:
        """실제 read-only 쿼리를 1회 날려 진짜 연결되는지 확인.

        반환: 성공 시 {"ok": True}, 실패 시 {"ok": False, "error": ...}.
        probe 정의가 없는 target 은 None(상위에서 tools/list 기준으로 판단).
        """
        probe = self._PROBES.get(t)
        if probe is None:
            return None
        sub_tool, args = probe
        try:
            res = self._stdio.call(t, sub_tool, args, timeout=self._PROBE_TIMEOUT)
        except Exception as e:  # noqa: BLE001  (timeout/연결 끊김 등)
            return {"ok": False, "error": f"probe failed: {type(e).__name__}: {e}"}
        # MCP 결과가 isError 거나, content 텍스트가 에러 메시지면 실패로 본다.
        if isinstance(res, dict):
            if res.get("isError"):
                return {"ok": False, "error": _probe_err_text(res)}
            txt = _probe_err_text(res)
            if txt and txt.lower().startswith("error"):
                return {"ok": False, "error": txt}
        return {"ok": True}

    def health(self, target: str | None = None, verify: bool = False) -> dict[str, Any]:
        """전체 또는 특정 target 상태. UI 연결테스트용.

        verify=False: stdio 세션이 떴는지(tools/list)만 본다 — 빠르지만 DB 실접속은
                      확인 못 함(MCP 서버는 DB 없이도 tools/list 가 됨).
        verify=True : DB/Prometheus 는 실제 read-only probe 쿼리를 1회 날려 진짜 연결을
                      확인한다. UI '연결 테스트' 버튼이 사용.
        """
        self.reload()
        cfg = self._cfg
        enabled = set(connections.enabled_targets(cfg))
        connected = set(self._stdio.connected_targets())

        def _status(t: str) -> dict[str, Any]:
            if t not in enabled:
                return {"enabled": False, "ok": None, "tools": 0}
            if t in connections.CUSTOM_TARGETS:
                try:
                    n = len(self._custom.list_tools(t))
                    return {"enabled": True, "ok": True, "tools": n}
                except Exception as e:  # noqa: BLE001
                    return {"enabled": True, "ok": False, "error": str(e), "tools": 0}
            # stdio: 우선 세션을 보장(연결 안 됐으면 재시도)
            if t in connected:
                base = {"enabled": True, "ok": True, "tools": len(self._stdio.list_tools(t))}
            else:
                conf = cfg["tools"].get(t, {})
                spec = connections.stdio_spec(t, conf, cfg.get("aws_region", "ap-northeast-2"))
                if spec is None:
                    return {"enabled": True, "ok": False,
                            "error": "connection config incomplete", "tools": 0}
                try:
                    toolset = self._stdio.ensure(t, spec)
                    base = {"enabled": True, "ok": True, "tools": len(toolset)}
                except Exception as e:  # noqa: BLE001
                    return {"enabled": True, "ok": False, "error": str(e), "tools": 0}
            # verify: 세션은 떴지만 DB 에 실제로 붙는지 probe 로 확정
            if verify:
                pr = self._probe(t)
                if pr is not None and not pr["ok"]:
                    return {"enabled": True, "ok": False, "verified": False,
                            "error": pr["error"], "tools": base["tools"]}
                if pr is not None:
                    base["verified"] = True
            return base

        if target:
            return {target: _status(target)}
        return {t: _status(t) for t in connections.ALL_TARGETS}


_registry: Registry | None = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = Registry()
    return _registry
