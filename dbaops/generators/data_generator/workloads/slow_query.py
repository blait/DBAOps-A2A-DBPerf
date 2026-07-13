"""slow_query — MySQL 인덱스 누락 풀스캔 조인 (의도적으로 느린 SQL)."""

from __future__ import annotations

import logging
import random
import time

import pymysql

from .._schema import ensure_mysql_schema
from .._secrets import mysql_dsn

logger = logging.getLogger(__name__)


def _ensure_data(dsn: dict, n_users: int = 100_000, n_orders: int = 1_000_000) -> None:
    conn = pymysql.connect(autocommit=False, **dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dbaops_users")
            if cur.fetchone()[0] >= n_users:
                return
            logger.info("seeding %d users + %d orders", n_users, n_orders)
            for i in range(0, n_users, 1000):
                rows = [(uid, f"user-{uid}", random.choice(["KR", "JP", "US", "DE"])) for uid in range(i, i + 1000)]
                cur.executemany("INSERT IGNORE INTO dbaops_users(id,name,region) VALUES (%s,%s,%s)", rows)
            for i in range(0, n_orders, 1000):
                rows = [(random.randint(1, n_users - 1), random.choice(["new", "paid"]), 100.0) for _ in range(1000)]
                cur.executemany("INSERT INTO dbaops_orders(user_id,status,amount) VALUES (%s,%s,%s)", rows)
            conn.commit()
    finally:
        conn.close()


def _slow_query_loop(end: float) -> None:
    dsn = mysql_dsn()
    conn = pymysql.connect(autocommit=True, **dsn)
    try:
        with conn.cursor() as cur:
            while time.time() < end:
                # name 인덱스 부재 → 풀스캔 + 조인
                cur.execute(
                    """SELECT u.region, COUNT(*) AS c, SUM(o.amount) AS s
                         FROM dbaops_users u
                         LEFT JOIN dbaops_orders o ON o.user_id = u.id
                        WHERE u.name LIKE %s
                        GROUP BY u.region""",
                    (f"%user-{random.randint(0, 9)}%",),
                )
                cur.fetchall()
                time.sleep(2.0)
    finally:
        conn.close()


def run(duration_sec: int) -> int:
    dsn = mysql_dsn()
    ensure_mysql_schema(dsn)
    _ensure_data(dsn)
    _slow_query_loop(time.time() + duration_sec)
    logger.info("slow_query finished")
    return 0
