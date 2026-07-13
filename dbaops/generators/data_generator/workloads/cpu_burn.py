"""cpu_burn — Aurora PG 호스트 CPU 압박.

연속 PG 측 in-memory 집계로 CPU 100% 도달 + 1-min CloudWatch 샘플 윈도 안에 안정적
캡처. 디스크 IO 거의 없음 — 순수 CPU 시그널만 만들어 OS·인프라 supervisor 가
'CPU peak vs baseline' 분석을 명확히 하도록.

  · workers          = max(vCPU, 4)  → t4g.medium(2 vCPU) 기준 4 worker 면 saturate
  · 한 쿼리 ≈ 0.8~1.5s (3M generate_series + md5 hash)
  · 3분 duration → CW 1-min sample 3개 모두 70~95% 캡처
"""

from __future__ import annotations

import logging
import os
import threading
import time

import psycopg

from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


# 한 회 실행이 ~1초 걸리는 in-memory 집계. 너무 짧으면 worker 간 connect 오버헤드,
# 너무 길면 worker 별 CPU 점유가 균일하지 않음.
_BURN_SQL = """
SELECT count(*), sum(length(h)) FROM (
    SELECT md5(g::text || random()::text) AS h
    FROM generate_series(1, 3000000) AS g
) t
"""

_N_WORKERS = int(os.environ.get("CPU_BURN_WORKERS", "8"))


def _worker(end: float, idx: int) -> None:
    dsn = pg_dsn()
    while time.time() < end:
        try:
            with psycopg.connect(**dsn, autocommit=True, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    n = 0
                    while time.time() < end:
                        t0 = time.time()
                        cur.execute(_BURN_SQL)
                        cur.fetchone()
                        n += 1
                        if n % 5 == 0:
                            logger.info("burn worker=%d iters=%d last=%.2fs", idx, n, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            logger.warning("worker %d aborted (will retry): %s", idx, e)
            time.sleep(1.0)


def run(duration_sec: int) -> int:
    end = time.time() + duration_sec
    logger.info("cpu_burn: workers=%d duration=%ds", _N_WORKERS, duration_sec)
    threads = [
        threading.Thread(target=_worker, args=(end, i), name=f"burn-{i}", daemon=True)
        for i in range(_N_WORKERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_sec + 30)
    logger.info("cpu_burn finished")
    return 0
