"""awslabs.cloudwatch-mcp-server 를 stdio 로 spawn 해 AgentCore Gateway Lambda target 으로 노출.

awslabs/run-mcp-with-aws-lambda 의 BedrockAgentCoreGatewayTargetHandler 가
Gateway 의 inline tools/call payload → stdio MCP request 로 변환.
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
    """Lambda 의 IAM role 자격증명을 자식 stdio 프로세스에 전파.

    StdioServerParameters(env=...) 는 명시한 키만 자식에게 넘기므로
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN 을 직접 포함해야 함.
    """
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
    env.setdefault("FASTMCP_LOG_LEVEL", os.environ.get("FASTMCP_LOG_LEVEL", "INFO"))
    env.setdefault("HOME", "/tmp")
    return env


server_params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "awslabs.cloudwatch_mcp_server.server"],
    env=_child_env(),
)

request_handler = StdioServerAdapterRequestHandler(server_params)
event_handler = BedrockAgentCoreGatewayTargetHandler(request_handler)


def handler(event, context):
    return event_handler.handle(event, context)
