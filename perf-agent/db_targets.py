"""
db_targets.py - 분석 대상 DB 레지스트리 (멀티엔진).

DB_TARGETS 환경변수(JSON)로 대상 목록을 정의한다. 각 항목:
  name    : 도구 호출 시 쓰는 식별자 (target 파라미터)
  engine  : mssql | postgres | mysql
  자격증명: secret_id(Secrets Manager JSON {host,port,username,password})
           또는 host/port/username/password 직접 + password_secret_id(값만 시크릿)
  database: 기본 접속 DB (mssql: master, postgres: postgres)

예:
  DB_TARGETS='[
    {"name":"mssql-main","engine":"mssql","secret_id":"dbops-sqlserver-secret","database":"master"},
    {"name":"pg-test","engine":"postgres","host":"...rds.amazonaws.com","port":5432,
     "username":"dbaops","password":"...","database":"appdb","sslmode":"require"}
  ]'

미설정 시 기존 동작과 호환되는 기본값(mssql-main, DB_SECRET_ID 사용).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")

_DEFAULT = [{
    "name": "mssql-main",
    "engine": "mssql",
    "secret_id": os.environ.get("DB_SECRET_ID", "dbops-sqlserver-secret"),
    "database": os.environ.get("DB_NAME", "master"),
}]


@lru_cache(maxsize=1)
def targets() -> dict[str, dict]:
    raw = os.environ.get("DB_TARGETS", "")
    items = json.loads(raw) if raw.strip() else _DEFAULT
    return {t["name"]: t for t in items}


def default_target() -> str:
    return next(iter(targets()))


def describe_targets() -> list[dict]:
    """자격증명 제외한 타깃 요약 (list_db_targets 도구용)."""
    out = []
    for name, t in targets().items():
        out.append({"name": name, "engine": t["engine"],
                    "database": t.get("database", ""),
                    "host": t.get("host", f"(secret:{t.get('secret_id','')})")})
    return out


def _resolve_creds(t: dict) -> dict:
    """타깃 정의 → {host, port, username, password, database} 완성."""
    creds = {k: t[k] for k in ("host", "port", "username", "password", "database", "sslmode") if k in t}
    if t.get("secret_id"):
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        s = json.loads(sm.get_secret_value(SecretId=t["secret_id"])["SecretString"])
        creds.setdefault("host", s.get("host"))
        creds.setdefault("port", s.get("port"))
        creds.setdefault("username", s.get("username"))
        creds.setdefault("password", s.get("password"))
    if t.get("password_secret_id"):
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        creds["password"] = sm.get_secret_value(SecretId=t["password_secret_id"])["SecretString"]
    default_port = {"mssql": 1433, "postgres": 5432, "mysql": 3306}[t["engine"]]
    default_db = {"mssql": "master", "postgres": "postgres", "mysql": "mysql"}[t["engine"]]
    creds.setdefault("port", default_port)
    creds.setdefault("database", default_db)
    return creds


def get_connection(target: str):
    """타깃 이름 → 열린 DB 커넥션. 호출자가 close 책임."""
    t = targets().get(target)
    if not t:
        raise ValueError(f"unknown target '{target}' — available: {list(targets())}")
    c = _resolve_creds(t)
    if t["engine"] == "mssql":
        import pymssql
        return pymssql.connect(server=c["host"], user=c["username"], password=c["password"],
                               port=int(c["port"]), database=c["database"], timeout=15)
    if t["engine"] == "postgres":
        import psycopg2
        return psycopg2.connect(host=c["host"], user=c["username"], password=c["password"],
                                port=int(c["port"]), dbname=c["database"],
                                sslmode=c.get("sslmode", "prefer"), connect_timeout=15)
    if t["engine"] == "mysql":
        import pymysql
        return pymysql.connect(host=c["host"], user=c["username"], password=c["password"],
                               port=int(c["port"]), database=c["database"],
                               connect_timeout=15, read_timeout=30)
    raise ValueError(f"unsupported engine '{t['engine']}'")


def engine_of(target: str) -> str:
    t = targets().get(target)
    if not t:
        raise ValueError(f"unknown target '{target}' — available: {list(targets())}")
    return t["engine"]


def run_query(target: str, sql: str) -> list[dict]:
    """쿼리 실행 → list[dict]. 엔진 무관 공통 헬퍼."""
    conn = get_connection(target)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()
