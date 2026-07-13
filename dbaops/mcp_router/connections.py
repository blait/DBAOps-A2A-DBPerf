"""connections.json 로드 + 각 MCP 도구의 연결 env 구성.

AgentCore Gateway 없이 EC2 단일 박스에서 MCP 서버들을 직접 spawn 하므로,
terraform 이 Lambda env 로 주던 PG_HOST / PROMETHEUS_URL 등을 이 파일이 대신 공급한다.

connections.json 은 Streamlit 연결설정 페이지가 write 하고, router 가 read 한다.
DATA_DIR(기본 /data) 아래 connections.json 을 본다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DBAOPS_DATA_DIR", "/data")
CONNECTIONS_PATH = os.environ.get(
    "DBAOPS_CONNECTIONS_PATH", os.path.join(DATA_DIR, "connections.json")
)

# router 가 다루는 10개 target 의 표준 이름 (Gateway target 이름과 동일).
ALL_TARGETS = [
    "rds-pi",
    "msk-metrics",
    "s3-log-fetch",
    "aws-api",
    "community-postgres",
    "community-mysql",
    "community-prometheus",
    "awslabs-cloudwatch",
    "awslabs-aws-doc",
    "awslabs-aws-api",
]

# 커스텀(직접 import) vs stdio(subprocess spawn) 구분.
CUSTOM_TARGETS = {"rds-pi", "msk-metrics", "s3-log-fetch", "aws-api"}
STDIO_TARGETS = {
    "community-postgres",
    "community-mysql",
    "community-prometheus",
    "awslabs-cloudwatch",
    "awslabs-aws-doc",
    "awslabs-aws-api",
}

# 연결 정보 없이 instance role 만으로 동작하는 도구 — connections.json 이 없어도 기본 ON.
# (PG/MySQL/Prometheus/MSK 는 연결 정보가 필요하므로 기본 OFF — UI 에서 채운 뒤 켜진다.)
NO_CONFIG_TARGETS = {
    "rds-pi", "s3-log-fetch", "aws-api",
    "awslabs-cloudwatch", "awslabs-aws-doc", "awslabs-aws-api",
}


_DEFAULTS: dict[str, Any] = {
    "aws_region": os.environ.get("AWS_REGION", "ap-northeast-2"),
    "bedrock_model_id": os.environ.get(
        "BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7"
    ),
    # 연결정보 불필요 도구는 기본 ON, 나머지는 OFF (연결정보 채운 뒤 켜야 함).
    "tools": {t: {"enabled": t in NO_CONFIG_TARGETS} for t in ALL_TARGETS},
    "infra_context": {
        "prom_instance_id": "",
        "aurora_cluster_id": "",
        "aurora_writer_id": "",
        "aurora_reader_id": "",
        "mysql_db_id": "",
        "msk_cluster_name": "",
        "log_bucket": "",
    },
}

_lock = threading.Lock()


def load() -> dict[str, Any]:
    """connections.json 을 읽어 default 와 병합. 파일이 없으면 default 반환."""
    cfg = json.loads(json.dumps(_DEFAULTS))  # deep copy
    try:
        with open(CONNECTIONS_PATH, encoding="utf-8") as f:
            disk = json.load(f)
    except FileNotFoundError:
        logger.warning("connections.json not found at %s — using defaults", CONNECTIONS_PATH)
        return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.error("failed to read connections.json: %s — using defaults", e)
        return cfg

    if isinstance(disk.get("aws_region"), str):
        cfg["aws_region"] = disk["aws_region"]
    if isinstance(disk.get("bedrock_model_id"), str):
        cfg["bedrock_model_id"] = disk["bedrock_model_id"]
    if isinstance(disk.get("tools"), dict):
        for t, conf in disk["tools"].items():
            if t in cfg["tools"] and isinstance(conf, dict):
                cfg["tools"][t].update(conf)
    if isinstance(disk.get("infra_context"), dict):
        cfg["infra_context"].update(disk["infra_context"])
    return cfg


def save(cfg: dict[str, Any]) -> None:
    """connections.json 원자적 write (UI 가 호출)."""
    os.makedirs(os.path.dirname(CONNECTIONS_PATH) or ".", exist_ok=True)
    tmp = CONNECTIONS_PATH + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONNECTIONS_PATH)
    logger.info("connections.json saved to %s", CONNECTIONS_PATH)


def mtime() -> float:
    """connections.json 의 mtime — router 가 변경 감지용. 없으면 0."""
    try:
        return os.path.getmtime(CONNECTIONS_PATH)
    except OSError:
        return 0.0


def enabled_targets(cfg: dict[str, Any]) -> list[str]:
    return [t for t in ALL_TARGETS if cfg["tools"].get(t, {}).get("enabled")]


# ───────────────────────── stdio 서버 spawn 사양 ─────────────────────────
# 각 target → (command, args, env builder). env builder 는 connections.json 의
# 해당 tool conf 를 받아 자식 프로세스 env dict 를 만든다. 기존 mcp_tools/*/handler.py
# 의 StdioServerParameters 와 동일한 child 실행을 router 에서 재현한다.


def _pass_aws_creds() -> dict[str, str]:
    """instance role 자격증명을 자식 stdio 프로세스에 전파 (boto3 자동 체인 키 + 명시 키)."""
    keys = (
        "AWS_REGION", "AWS_DEFAULT_REGION",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE",
        "AWS_EC2_METADATA_DISABLED",
        "PATH", "HOME", "LANG", "LC_ALL",
    )
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env.setdefault("AWS_REGION", os.environ.get("AWS_REGION", "ap-northeast-2"))
    env.setdefault("HOME", "/tmp")
    return env


def _resolve_secret(conf: dict, user_key: str, pass_key: str, secret_arn_key: str,
                    region: str) -> tuple[str | None, str | None]:
    """user/password 를 직접 conf 에서 받거나, secret_arn 이 있으면 Secrets Manager 에서 fetch.

    고객이 Secret 없이 user/pass 직접 줄 수도, Secret ARN 만 줄 수도 있어 둘 다 지원.
    """
    user = conf.get(user_key) or None
    password = conf.get(pass_key) or None
    if user and password:
        return user, password
    secret_arn = conf.get(secret_arn_key) or None
    if secret_arn:
        try:
            import boto3
            sm = boto3.client("secretsmanager", region_name=region)
            creds = json.loads(sm.get_secret_value(SecretId=secret_arn)["SecretString"])
            return creds.get("username") or user, creds.get("password") or password
        except Exception as e:  # noqa: BLE001
            logger.error("secret fetch failed for %s: %s", secret_arn, e)
    return user, password


def stdio_spec(target: str, conf: dict, region: str) -> dict[str, Any] | None:
    """target 의 stdio 서버 spawn 사양 반환: {command, args, env}. 미지원이면 None."""
    import sys

    base_aws = _pass_aws_creds()
    base_aws["FASTMCP_LOG_LEVEL"] = os.environ.get("FASTMCP_LOG_LEVEL", "INFO")

    if target == "community-prometheus":
        url = conf.get("PROMETHEUS_URL")
        if not url:
            logger.warning("community-prometheus: PROMETHEUS_URL missing")
            return None
        return {
            "command": sys.executable,
            "args": ["-m", "prometheus_mcp_server.main"],
            "env": {
                "PROMETHEUS_URL": url,
                "PROMETHEUS_MCP_SERVER_TRANSPORT": "stdio",
                "FASTMCP_LOG_LEVEL": base_aws["FASTMCP_LOG_LEVEL"],
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
            },
        }

    if target == "community-postgres":
        host = conf.get("PG_HOST")
        if not host:
            logger.warning("community-postgres: PG_HOST missing")
            return None
        user, password = _resolve_secret(
            conf, "PG_USER", "PG_PASSWORD", "PG_SECRET_ARN", region
        )
        dbname = conf.get("PG_DBNAME", "postgres")
        port = conf.get("PG_PORT", "5432")
        sslmode = conf.get("PG_SSLMODE", "require")
        if not (user and password):
            logger.warning("community-postgres: credentials missing")
            return None
        url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode={sslmode}"
        # unrestricted 는 EXPLAIN/튜닝 도구가 열림 — read-only DB 유저와 함께 쓸 것.
        access_mode = conf.get("PG_ACCESS_MODE", "restricted")
        if access_mode not in ("restricted", "unrestricted"):
            access_mode = "restricted"
        return {
            "command": "postgres-mcp",
            "args": ["--access-mode", access_mode, "--transport", "stdio", url],
            "env": {
                "FASTMCP_LOG_LEVEL": base_aws["FASTMCP_LOG_LEVEL"],
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
            },
        }

    if target == "community-mysql":
        host = conf.get("MYSQL_HOST")
        if not host:
            logger.warning("community-mysql: MYSQL_HOST missing")
            return None
        user, password = _resolve_secret(
            conf, "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_SECRET_ARN", region
        )
        if not (user and password):
            logger.warning("community-mysql: credentials missing")
            return None
        node_entry = os.environ.get(
            "MYSQL_MCP_ENTRY",
            "/app/node_modules/@benborla29/mcp-server-mysql/dist/index.js",
        )
        return {
            "command": "node",
            "args": [node_entry],
            "env": {
                "MYSQL_HOST": host,
                "MYSQL_PORT": str(conf.get("MYSQL_PORT", "3306")),
                "MYSQL_USER": user,
                "MYSQL_PASS": password,
                "MYSQL_DB": conf.get("MYSQL_DB", conf.get("MYSQL_DBNAME", "mysql")),
                "ALLOW_INSERT_OPERATION": "false",
                "ALLOW_UPDATE_OPERATION": "false",
                "ALLOW_DELETE_OPERATION": "false",
                "PATH": os.environ.get("PATH", ""),
                "NODE_PATH": os.environ.get("NODE_PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
            },
        }

    if target == "awslabs-cloudwatch":
        return {
            "command": sys.executable,
            "args": ["-m", "awslabs.cloudwatch_mcp_server.server"],
            "env": base_aws,
        }

    if target == "awslabs-aws-doc":
        return {
            "command": sys.executable,
            "args": ["-m", "awslabs.aws_documentation_mcp_server.server"],
            "env": {
                "FASTMCP_LOG_LEVEL": base_aws["FASTMCP_LOG_LEVEL"],
                "AWS_DOCUMENTATION_PARTITION": conf.get("AWS_DOCUMENTATION_PARTITION", "aws"),
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", "/tmp"),
            },
        }

    if target == "awslabs-aws-api":
        os.makedirs("/tmp/.aws/aws-api-mcp", exist_ok=True)
        env = dict(base_aws)
        env["HOME"] = "/tmp"
        env["READ_OPERATIONS_ONLY"] = "true"
        env["AWS_API_MCP_WORKING_DIR"] = "/tmp/.aws/aws-api-mcp"
        env["AWS_API_MCP_TELEMETRY"] = "false"
        return {
            "command": sys.executable,
            "args": ["-m", "awslabs.aws_api_mcp_server.server"],
            "env": env,
        }

    return None
