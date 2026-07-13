"""baseline workload — PG ~50 TPS / MySQL ~30 QPS / Kafka ~100 msg/s 모사."""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time

import psycopg
import pymysql

from .._kafka import ensure_topic, make_producer
from .._schema import ensure_mysql_schema, ensure_pg_schema
from .._secrets import mysql_dsn, pg_dsn

logger = logging.getLogger(__name__)


def _stop_at(end: float) -> bool:
    return time.time() >= end


def _pg_loop(end: float, target_tps: int = 50) -> None:
    dsn = pg_dsn()
    ensure_pg_schema(dsn)
    interval = 1.0 / target_tps
    statuses = ("new", "paid", "shipped", "cancelled")
    with psycopg.connect(**dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            while not _stop_at(end):
                start = time.time()
                op = random.random()
                if op < 0.6:
                    cur.execute(
                        "INSERT INTO dbaops_orders(user_id, status, amount) VALUES (%s,%s,%s)",
                        (random.randint(1, 10_000), random.choice(statuses), round(random.uniform(1, 999), 2)),
                    )
                elif op < 0.9:
                    cur.execute("SELECT * FROM dbaops_orders WHERE user_id = %s LIMIT 5",
                                (random.randint(1, 10_000),))
                    cur.fetchall()
                else:
                    cur.execute("UPDATE dbaops_hot_counter SET n = n + 1 WHERE id = 1")
                elapsed = time.time() - start
                if elapsed < interval:
                    time.sleep(interval - elapsed)


def _mysql_loop(end: float, target_qps: int = 30) -> None:
    dsn = mysql_dsn()
    ensure_mysql_schema(dsn)
    interval = 1.0 / target_qps
    statuses = ("new", "paid", "shipped", "cancelled")
    conn = pymysql.connect(autocommit=True, **dsn)
    try:
        with conn.cursor() as cur:
            while not _stop_at(end):
                start = time.time()
                op = random.random()
                if op < 0.6:
                    cur.execute(
                        "INSERT INTO dbaops_orders(user_id,status,amount) VALUES (%s,%s,%s)",
                        (random.randint(1, 10_000), random.choice(statuses), round(random.uniform(1, 999), 2)),
                    )
                else:
                    cur.execute("SELECT id, status FROM dbaops_orders WHERE user_id=%s LIMIT 5",
                                (random.randint(1, 10_000),))
                    cur.fetchall()
                elapsed = time.time() - start
                if elapsed < interval:
                    time.sleep(interval - elapsed)
    finally:
        conn.close()


def _kafka_loop(end: float, target_rps: int = 100) -> None:
    if not os.environ.get("MSK_BOOTSTRAP"):
        logger.info("MSK_BOOTSTRAP not set — skipping kafka baseline")
        return
    topic = os.environ.get("KAFKA_TOPIC", "dbaops.orders")
    if not ensure_topic(topic):
        logger.warning("kafka topic '%s' unavailable — skipping kafka baseline", topic)
        return
    producer = make_producer()
    interval = 1.0 / target_rps
    while not _stop_at(end):
        msg = {
            "ts": time.time(),
            "user_id": random.randint(1, 10_000),
            "amount": round(random.uniform(1, 999), 2),
        }
        producer.produce(topic, json.dumps(msg).encode())
        producer.poll(0)
        time.sleep(interval)
    producer.flush(5.0)


def run(duration_sec: int) -> int:
    end = time.time() + duration_sec
    threads = [
        threading.Thread(target=_pg_loop, args=(end,), name="pg", daemon=True),
        threading.Thread(target=_mysql_loop, args=(end,), name="mysql", daemon=True),
        threading.Thread(target=_kafka_loop, args=(end,), name="kafka", daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_sec + 30)
    logger.info("baseline finished")
    return 0
