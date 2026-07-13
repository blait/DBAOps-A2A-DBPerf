"""pab1it0/prometheus-mcp-server stdio adapter.

self-hosted Prometheus 의 HTTP API 직접 쿼리. 우리 EC2 (port 9090) 가 target.
PROMETHEUS_URL env 는 terraform 에서 자동 주입.
"""

from __future__ import annotations

import os
import sys

from mcp.client.stdio import StdioServerParameters
from mcp_lambda import (
    BedrockAgentCoreGatewayTargetHandler,
    StdioServerAdapterRequestHandler,
)

def _child_env() -> dict:
    env = {
        "PROMETHEUS_URL":                  os.environ["PROMETHEUS_URL"],
        "PROMETHEUS_MCP_SERVER_TRANSPORT": "stdio",
        "FASTMCP_LOG_LEVEL":               os.environ.get("FASTMCP_LOG_LEVEL", "INFO"),
        "PATH":                            os.environ.get("PATH", ""),
        "HOME":                            os.environ.get("HOME", "/tmp"),
    }
    return env


server_params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "prometheus_mcp_server.main"],
    env=_child_env(),
)

request_handler = StdioServerAdapterRequestHandler(server_params)
event_handler = BedrockAgentCoreGatewayTargetHandler(request_handler)


def handler(event, context):
    return event_handler.handle(event, context)
