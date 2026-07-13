"""lock_contention — hot row SELECT … FOR UPDATE 동시 다발 (PG)."""

from __future__ import annotations

import logging
import threading
import time

import psycopg

from .._schema import ensure_pg_schema
from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


def _worker(end: float, idx: int) -> None:
    dsn = pg_dsn()
    with psycopg.connect(**dsn) as conn:
        while time.time() < end:
            try:
                with conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("SELECT n FROM dbaops_hot_counter WHERE id = 1 FOR UPDATE")
                    cur.fetchone()
                    # 잠시 잡고 있다 풀기 — 다른 워커가 대기하게
                    time.sleep(0.2 + 0.05 * idx)
                    cur.execute("UPDATE dbaops_hot_counter SET n = n + 1 WHERE id = 1")
                    cur.execute("COMMIT")
            except Exception as e:  # noqa: BLE001
                logger.warning("worker %d error: %s", idx, e)
                conn.rollback()


def run(duration_sec: int, workers: int = 8) -> int:
    ensure_pg_schema(pg_dsn())
    end = time.time() + duration_sec
    threads = [
        threading.Thread(target=_worker, args=(end, i), name=f"lock-{i}", daemon=True)
        for i in range(workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_sec + 30)
    logger.info("lock_contention finished")
    return 0
