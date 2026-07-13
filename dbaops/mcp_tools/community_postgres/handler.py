"""crystaldba/postgres-mcp stdio adapter (restricted RO mode).

DB URL 은 PG_HOST/PG_DBNAME/PG_SECRET_ARN env 로 주고 startup 시 Secrets Manager
에서 user/password 만 fetch 해 connection string 을 구성한다.
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache

import boto3
from mcp.client.stdio import StdioServerParameters
from mcp_lambda import (
    BedrockAgentCoreGatewayTargetHandler,
    StdioServerAdapterRequestHandler,
)


@lru_cache(maxsize=1)
def _build_database_url() -> str:
    secret_arn = os.environ["PG_SECRET_ARN"]
    host       = os.environ["PG_HOST"]
    dbname     = os.environ.get("PG_DBNAME", "dbaops")
    port       = os.environ.get("PG_PORT", "5432")
    sm = boto3.client("secretsmanager")
    creds = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
    user     = creds["username"]
    password = creds["password"]
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode=require"


def _make_event_handler():
    """Lazy: invoke 시점에 server_params 를 만들어야 lru_cache 가 fresh secret 가져옴."""
    env = {
        "FASTMCP_LOG_LEVEL": os.environ.get("FASTMCP_LOG_LEVEL", "INFO"),
        "PATH":              os.environ.get("PATH", "/var/lang/bin:/usr/local/bin:/usr/bin:/bin"),
        "HOME":              os.environ.get("HOME", "/tmp"),
    }
    # postgres-mcp 는 DB 연결만이라 AWS 인증 불필요. PG 연결만 정상이면 됨.
    server_params = StdioServerParameters(
        command="postgres-mcp",
        args=["--access-mode", "restricted", "--transport", "stdio", _build_database_url()],
        env=env,
    )
    request_handler = StdioServerAdapterRequestHandler(server_params)
    return BedrockAgentCoreGatewayTargetHandler(request_handler)


_EVENT_HANDLER = None


def handler(event, context):
    global _EVENT_HANDLER
    if _EVENT_HANDLER is None:
        _EVENT_HANDLER = _make_event_handler()
    return _EVENT_HANDLER.handle(event, context)
