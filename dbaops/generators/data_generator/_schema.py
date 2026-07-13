"""PG / MySQL 워크로드 스키마 부트스트랩."""

from __future__ import annotations

import logging

import psycopg
import pymysql

logger = logging.getLogger(__name__)


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS dbaops_orders (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    status      TEXT   NOT NULL,
    amount      NUMERIC(10, 2) NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dbaops_orders_user ON dbaops_orders(user_id);

CREATE TABLE IF NOT EXISTS dbaops_hot_counter (
    id    INT PRIMARY KEY,
    n     BIGINT DEFAULT 0
);
INSERT INTO dbaops_hot_counter(id, n) VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;
"""

_MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS dbaops_orders (
    id          BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id     BIGINT NOT NULL,
    status      VARCHAR(32) NOT NULL,
    amount      DECIMAL(10,2) NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dbaops_users (
    id   BIGINT PRIMARY KEY,
    name VARCHAR(64),
    region VARCHAR(32)
);
"""


def ensure_pg_schema(dsn: dict) -> None:
    with psycopg.connect(**dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            for stmt in _PG_SCHEMA.strip().split(";\n"):
                if stmt.strip():
                    cur.execute(stmt)
    logger.info("pg schema ready")


def ensure_mysql_schema(dsn: dict) -> None:
    conn = pymysql.connect(autocommit=True, **dsn)
    try:
        with conn.cursor() as cur:
            for stmt in _MYSQL_SCHEMA.strip().split(";\n"):
                if stmt.strip():
                    cur.execute(stmt)
    finally:
        conn.close()
    logger.info("mysql schema ready")
