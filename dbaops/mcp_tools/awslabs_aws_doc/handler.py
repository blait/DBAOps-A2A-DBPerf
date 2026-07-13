"""awslabs.aws-documentation-mcp-server stdio adapter."""

from __future__ import annotations

import os
import sys

from mcp.client.stdio import StdioServerParameters
from mcp_lambda import (
    BedrockAgentCoreGatewayTargetHandler,
    StdioServerAdapterRequestHandler,
)

def _child_env() -> dict:
    """aws-doc 는 AWS API 안 쓰지만 system PATH/HOME 은 필요."""
    env = {
        "FASTMCP_LOG_LEVEL":           os.environ.get("FASTMCP_LOG_LEVEL", "INFO"),
        "AWS_DOCUMENTATION_PARTITION": os.environ.get("AWS_DOCUMENTATION_PARTITION", "aws"),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    return env


server_params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "awslabs.aws_documentation_mcp_server.server"],
    env=_child_env(),
)

request_handler = StdioServerAdapterRequestHandler(server_params)
event_handler = BedrockAgentCoreGatewayTargetHandler(request_handler)


def handler(event, context):
    return event_handler.handle(event, context)
