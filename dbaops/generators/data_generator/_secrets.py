"""SecretsManager 자격증명 헬퍼."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import boto3

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def get_secret(arn_or_name: str) -> dict:
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=arn_or_name)
    raw = resp.get("SecretString") or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"password": raw}


def pg_dsn() -> dict:
    """environment:
    PG_HOST, PG_PORT (default 5432), PG_DBNAME, PG_SECRET_ARN
    """
    secret = get_secret(os.environ["PG_SECRET_ARN"])
    return {
        "host":     os.environ["PG_HOST"],
        "port":     int(os.environ.get("PG_PORT", "5432")),
        "dbname":   os.environ.get("PG_DBNAME", "dbaops"),
        "user":     secret.get("username", "dbaops_admin"),
        "password": secret["password"],
    }


def mysql_dsn() -> dict:
    secret = get_secret(os.environ["MYSQL_SECRET_ARN"])
    return {
        "host":     os.environ["MYSQL_HOST"],
        "port":     int(os.environ.get("MYSQL_PORT", "3306")),
        "database": os.environ.get("MYSQL_DBNAME", "dbaops"),
        "user":     secret.get("username", "dbaops_admin"),
        "password": secret["password"],
    }
