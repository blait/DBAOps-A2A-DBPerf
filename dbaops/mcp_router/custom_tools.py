"""커스텀 MCP 도구 4개(rds-pi/msk-metrics/s3-log-fetch/aws-api) 직접 import 래퍼.

이 도구들은 stdio MCP 서버가 아니라 단순 boto3 함수(mcp_tools/*/handler.py)다.
AgentCore Gateway 가 하던 일 — tool_io.json 의 schema 노출 + handler 호출 — 을 router 가 직접 한다.

handler 호출 규약(기존 Gateway/Lambda 와 동일):
  - rds-pi:       handler({"body": args}, None) → dict
  - msk-metrics:  handler({"body": args}, None) → dict
  - s3-log-fetch: handler({"body": args}, None) → dict   (s3_log_fetch / s3_list_logs 분기는 body 내용으로)
  - aws-api:      handler({"tool_name": sub, "arguments": args}, None) → dict  (sub-tool dispatch)

tools/list 에 노출되는 이름은 Gateway namespacing 과 동일: '<target>___<tool>'.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

# repo 루트의 mcp_tools/ 경로. router 가 어디서 실행되든 찾도록 env 우선.
MCP_TOOLS_DIR = os.environ.get(
    "DBAOPS_MCP_TOOLS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp_tools"),
)


def _load_handler(tool_dir: str) -> Callable:
    """mcp_tools/<dir>/handler.py 의 handler 함수를 동적 import."""
    path = os.path.join(MCP_TOOLS_DIR, tool_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(f"dbaops_handler_{tool_dir}", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load handler at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


def _load_tool_io(tool_dir: str) -> list[dict[str, Any]]:
    """tool_io.json → MCP tool spec 리스트 (단일 객체도 리스트로 정규화)."""
    path = os.path.join(MCP_TOOLS_DIR, tool_dir, "tool_io.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "tools" in data:
        return data["tools"]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    return []


def _to_mcp_tool(target: str, spec: dict[str, Any]) -> dict[str, Any]:
    """tool_io.json 의 항목 → MCP tools/list 항목. 이름은 '<target>___<tool>'."""
    name = spec.get("name")
    return {
        "name": f"{target}___{name}",
        "description": (spec.get("description") or "").strip() or f"Call {name}",
        "inputSchema": spec.get("input_schema") or {"type": "object", "properties": {}},
    }


# target → (handler dir, dispatch 방식)
_CUSTOM_DIRS = {
    "rds-pi": "rds_pi",
    "msk-metrics": "msk_metrics",
    "s3-log-fetch": "s3_log_fetch",
    "aws-api": "aws_api",
}


class CustomToolHost:
    """커스텀 4개 target 의 catalog 제공 + tools/call 실행."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._specs: dict[str, list[dict]] = {}

    def _ensure(self, target: str) -> None:
        if target not in self._handlers:
            tool_dir = _CUSTOM_DIRS[target]
            self._handlers[target] = _load_handler(tool_dir)
            self._specs[target] = _load_tool_io(tool_dir)

    def list_tools(self, target: str) -> list[dict[str, Any]]:
        self._ensure(target)
        return [_to_mcp_tool(target, s) for s in self._specs[target]]

    def call(self, target: str, sub_tool: str, args: dict[str, Any]) -> Any:
        """sub_tool 은 namespacing 제거된 실제 도구 이름 (예: 'describe_rds_instances')."""
        self._ensure(target)
        handler = self._handlers[target]

        if target == "aws-api":
            # aws_api handler 는 tool_name 으로 dispatch + arguments
            event = {"tool_name": sub_tool, "arguments": args}
        else:
            # rds-pi / msk-metrics / s3-log-fetch 는 body 가 곧 인자
            event = {"body": args}
        return handler(event, None)
