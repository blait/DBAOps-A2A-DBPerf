"""connection_spike — PG 10초간 200 short connection burst."""

from __future__ import annotations

import logging
import threading
import time

import psycopg

from .._secrets import pg_dsn

logger = logging.getLogger(__name__)


# 모듈 전역 카운터 (스레드 간 공유) — 한 번 burst 실행 단위로 reset
_lock = threading.Lock()
_succ = 0
_fail = 0
_first_err: Exception | None = None


def _short_conn_burst(dsn: dict, hold_sec: float) -> None:
    """connection 1개를 만들고 hold_sec 동안 잡고 있다 닫는다.

    너무 짧게 닫으면 RDS DatabaseConnections (1min 집계) 에 안 잡힌다.
    """
    global _succ, _fail, _first_err
    try:
        conn = psycopg.connect(**dsn, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        time.sleep(hold_sec)  # 동시 점유 시간을 늘려 메트릭 노출
        conn.close()
        with _lock:
            _succ += 1
    except Exception as e:  # noqa: BLE001
        with _lock:
            _fail += 1
            if _first_err is None:
                _first_err = e


def _one_burst(dsn: dict, burst_conns: int, hold_sec: float) -> tuple[int, int]:
    """한 번의 burst — burst_conns 개 thread 동시 시작 후 hold_sec 동안 잡고 닫는다."""
    global _succ, _fail, _first_err
    _succ = 0
    _fail = 0
    _first_err = None
    threads = [
        threading.Thread(target=_short_conn_burst, args=(dsn, hold_sec), daemon=True)
        for _ in range(burst_conns)
    ]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=hold_sec + 30)
    elapsed = time.time() - t0
    logger.info("burst done in %.2fs: success=%d fail=%d", elapsed, _succ, _fail)
    if _first_err is not None:
        logger.warning("first connection error: %s", _first_err)
    return _succ, _fail


def run(duration_sec: int, burst_conns: int = 200, hold_sec: float = 35.0,
        repeat_every_sec: int = 60) -> int:
    """duration_sec 동안 burst 를 반복 — CloudWatch 1-min sampling 에 무조건 걸리도록.

    기본값: 200 conns × 35s hold, 60s 마다 한 번 반복.
    - hold_sec 35s: AWS/RDS DatabaseConnections 의 1-min Maximum 통계가 burst 를
      놓치지 않도록 한 sampling window 를 넘겨 잡고 있게 한다.
    - 짧은 hold 면 jitter 로 max 가 8 ~ 200 사이로 들쭉날쭉 잡혀 진단 신호가 약해진다.
    """
    dsn = pg_dsn()
    logger.info(
        "connection_spike: %d threads, hold=%.1fs, repeat every %ds (duration %ds)",
        burst_conns, hold_sec, repeat_every_sec, duration_sec,
    )

    end = time.time() + duration_sec
    n_bursts = 0
    while time.time() < end:
        burst_t0 = time.time()
        succ, fail = _one_burst(dsn, burst_conns, hold_sec)
        n_bursts += 1
        elapsed = time.time() - burst_t0

        # 다음 burst 까지 남은 인터벌만큼 sleep — burst 자체가 길면 바로 다음 시작
        wait_for = max(0.0, repeat_every_sec - elapsed)
        remaining = end - time.time()
        if remaining <= 0:
            break
        logger.info("burst %d done; sleeping %.1fs (remaining %.1fs)",
                    n_bursts, min(wait_for, remaining), remaining)
        time.sleep(min(wait_for, remaining))

    logger.info("connection_spike finished — total %d bursts", n_bursts)
    return 0
