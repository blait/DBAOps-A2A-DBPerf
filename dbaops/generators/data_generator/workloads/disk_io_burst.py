"""disk_io_burst — Aurora PG 디스크 IO + 메모리 압박 메트릭 자극.

워킹셋이 shared_buffers 보다 작으면 buffer cache 에 통째로 들어가 ReadIOPS 가 안 잡힌다.
db.t4g.medium 의 RAM 4GB / shared_buffers ~3GB 를 의도적으로 초과하도록 ~5GB 워킹셋을
seed 하고, random PK scattered SELECT 로 buffer eviction 을 강제. 결과:

  · VolumeReadIOPs / ReadLatency      ← random PK SELECT (cache miss)
  · VolumeWriteIOPs / WriteLatency    ← INSERT/UPDATE
  · FreeableMemory                    ← 워킹셋 > shared_buffers
  · NetworkReceive/TransmitThroughput ← INSERT payload + SELECT 결과

OS·인프라 supervisor 로 분석 시 위 메트릭들이 동시에 spike → cache eviction 가설.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time

import psycopg

from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


_TABLE = "dbaops_io_burst"
# 1500B payload × 3M rows ≈ 5GB — db.t4g.medium 4GB RAM 보다 명백히 큼.
# (실제로는 PG 의 row overhead 30B 정도 + tuple overhead 추가 → ~5.5GB)
_ROW_TARGET = int(os.environ.get("DISK_IO_ROW_TARGET", "3000000"))
_PAYLOAD_BYTES = 1500  # TOAST threshold(2KB) 미만이라 main heap 에 그대로
_SEED_BATCH = 100_000


def _ensure_seed(dsn: dict) -> None:
    """대용량 시드. 이미 충분하면 skip. 미달이면 batch 로 채움."""
    with psycopg.connect(**dsn, autocommit=True, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id  BIGINT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    n   INT DEFAULT 0
                )
            """)
            cur.execute(f"SELECT count(*) FROM {_TABLE}")
            cnt = cur.fetchone()[0]
            if cnt >= _ROW_TARGET:
                logger.info("seed already present rows=%d (target=%d) — skip", cnt, _ROW_TARGET)
                return
            logger.info("seeding %s rows=%d → %d (batch=%d)", _TABLE, cnt, _ROW_TARGET, _SEED_BATCH)
            t0 = time.time()
            start = cnt + 1
            while start <= _ROW_TARGET:
                end = min(start + _SEED_BATCH - 1, _ROW_TARGET)
                cur.execute(f"""
                    INSERT INTO {_TABLE}(id, payload)
                    SELECT g, repeat(md5(g::text), {_PAYLOAD_BYTES // 32 + 1})
                    FROM generate_series({start}, {end}) g
                    ON CONFLICT (id) DO NOTHING
                """)
                logger.info("  seeded %d / %d  (%.1fs)", end, _ROW_TARGET, time.time() - t0)
                start = end + 1
            # ANALYZE — planner 가 random PK lookup 을 정확히 IndexScan 으로 잡게
            cur.execute(f"ANALYZE {_TABLE}")
            logger.info("seed done rows=%d elapsed=%.1fs", _ROW_TARGET, time.time() - t0)


def _read_worker(end: float, idx: int) -> None:
    """random PK 200개씩 한 번에 — buffer cache eviction 강제."""
    dsn = pg_dsn()
    with psycopg.connect(**dsn, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            n = 0
            while time.time() < end:
                ids = [random.randint(1, _ROW_TARGET) for _ in range(200)]
                cur.execute(
                    f"SELECT id, length(payload) FROM {_TABLE} WHERE id = ANY(%s)",
                    (ids,),
                )
                cur.fetchall()
                n += 1
                if n % 100 == 0:
                    logger.info("read worker=%d iters=%d", idx, n)


def _write_worker(end: float, idx: int) -> None:
    """신규 INSERT + 기존 random row UPDATE — WriteIOPS / WAL 자극."""
    dsn = pg_dsn()
    with psycopg.connect(**dsn, autocommit=True, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            n = 0
            while time.time() < end:
                # 신규 INSERT (overlap 회피용 큰 offset)
                row_id = _ROW_TARGET + random.randint(1, 50_000_000)
                payload = "X" * _PAYLOAD_BYTES
                try:
                    cur.execute(
                        f"INSERT INTO {_TABLE}(id, payload) VALUES (%s, %s) "
                        f"ON CONFLICT (id) DO UPDATE SET n = {_TABLE}.n + 1",
                        (row_id, payload),
                    )
                except Exception:  # noqa: BLE001
                    pass
                # 기존 random row UPDATE — buffer dirty + WAL
                if n % 3 == 0:
                    rid = random.randint(1, _ROW_TARGET)
                    try:
                        cur.execute(
                            f"UPDATE {_TABLE} SET n = n + 1 WHERE id = %s",
                            (rid,),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                n += 1
                if n % 500 == 0:
                    logger.info("write worker=%d iters=%d", idx, n)


def run(duration_sec: int) -> int:
    dsn = pg_dsn()
    _ensure_seed(dsn)

    end = time.time() + duration_sec
    logger.info("disk_io_burst: duration=%ds working_set≈%dMB",
                duration_sec, (_ROW_TARGET * _PAYLOAD_BYTES) // 1_000_000)
    threads = (
        [threading.Thread(target=_read_worker,  args=(end, i), name=f"read-{i}",  daemon=True) for i in range(6)]
        + [threading.Thread(target=_write_worker, args=(end, i), name=f"write-{i}", daemon=True) for i in range(3)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_sec + 60)
    logger.info("disk_io_burst finished")
    return 0
