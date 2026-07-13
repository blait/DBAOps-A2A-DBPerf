"""awslabs.aws-api-mcp-server stdio adapter.

READ_OPERATIONS_ONLY=true 로 강제 — 우리 PoC 는 read-only 분석만 허용.
"""

from __future__ import annotations

import os
import sys

from mcp.client.stdio import StdioServerParameters
from mcp_lambda import (
    BedrockAgentCoreGatewayTargetHandler,
    StdioServerAdapterRequestHandler,
)

# aws-api-mcp 가 startup 시 working dir 존재 확인을 함 — 미리 mkdir.
os.makedirs("/tmp/aws-api-mcp", exist_ok=True)


def _child_env() -> dict:
    """Lambda IAM credentials + 필수 env 를 자식 stdio 프로세스에 전파."""
    pass_keys = (
        "AWS_REGION", "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_LAMBDA_RUNTIME_API",
        "PATH", "HOME", "LANG", "LC_ALL",
    )
    env = {k: os.environ[k] for k in pass_keys if k in os.environ}
    env.setdefault("AWS_REGION", "ap-northeast-2")
    env["FASTMCP_LOG_LEVEL"]       = os.environ.get("FASTMCP_LOG_LEVEL", "INFO")
    env["READ_OPERATIONS_ONLY"]    = "true"
    env["AWS_API_MCP_WORKING_DIR"] = "/tmp/aws-api-mcp"
    env["AWS_API_MCP_TELEMETRY"]   = "false"
    env.setdefault("HOME", "/tmp")
    return env


server_params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "awslabs.aws_api_mcp_server.server"],
    env=_child_env(),
)

request_handler = StdioServerAdapterRequestHandler(server_params)
event_handler = BedrockAgentCoreGatewayTargetHandler(request_handler)


def handler(event, context):
    return event_handler.handle(event, context)
