"""DB/Kafka 로그 라인 템플릿 — baseline + burst 모드."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

# 단순화된 형식. drain3 가 이걸 패턴으로 잘 묶을 수 있도록 변수만 자리 잡아둔다.

PG_BASELINE = [
    'LOG:  duration: {ms} ms  statement: SELECT * FROM dbaops_orders WHERE user_id = {uid}',
    'LOG:  connection authorized: user=dbaops_admin database=dbaops',
    'LOG:  checkpoint starting: time',
]
PG_BURST = [
    'ERROR:  deadlock detected',
    'DETAIL:  Process {pid1} waits for ShareLock on transaction {xid}; blocked by process {pid2}',
    'FATAL:  too many connections for database "dbaops"',
    'LOG:  process {pid1} acquired ExclusiveLock on tuple ({a},{b}) of relation {rel} after {ms} ms',
]

MYSQL_BASELINE = [
    '[Note] Aborted connection {conn} to db: dbaops user: dbaops_admin host: 10.40.{a}.{b}',
    '# Time: {ts}\n# Query_time: {qt} Lock_time: {lt} Rows_sent: {rs}\nSELECT * FROM dbaops_orders WHERE user_id={uid};',
]
MYSQL_BURST = [
    '[ERROR] InnoDB: Operating system error number 28 in a file operation.',
    '[ERROR] [MY-013183] [InnoDB] Assertion failure: trx0trx.cc:{ln}',
    '# Query_time: {qt} Lock_time: {lt} Rows_examined: {re}\nSELECT u.region, COUNT(*) FROM dbaops_users u LEFT JOIN dbaops_orders o ON o.user_id=u.id WHERE u.name LIKE \'%user-{uid}%\' GROUP BY u.region;',
]

KAFKA_BASELINE = [
    '[{ts}] INFO [GroupCoordinator {gid}]: Member dbaops-{cid} in group dbaops-orders has joined',
    '[{ts}] INFO [Log partition=dbaops.orders-{p}, dir=/data] Rolled new log segment',
]
KAFKA_BURST = [
    '[{ts}] WARN [ReplicaManager broker={bid}]: Shrinking ISR for partition dbaops.orders-{p} from {a},{b},{c} to {a}',
    '[{ts}] ERROR [Log partition=dbaops.orders-{p}, dir=/data] Could not append, lost leadership',
    '[{ts}] WARN Connect task task-{tid} failed: org.apache.kafka.connect.errors.RetriableException: Connection refused',
]


def _render(template: str) -> str:
    return template.format(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ms=random.randint(10, 5000),
        qt=round(random.uniform(0.001, 8.0), 3),
        lt=round(random.uniform(0.0, 1.5), 3),
        rs=random.randint(0, 1000),
        re=random.randint(0, 1_000_000),
        uid=random.randint(1, 9999),
        pid1=random.randint(1000, 9999),
        pid2=random.randint(1000, 9999),
        xid=random.randint(1_000_000, 9_999_999),
        rel=random.choice(["dbaops_orders", "dbaops_hot_counter"]),
        a=random.randint(1, 254),
        b=random.randint(1, 254),
        c=random.randint(1, 254),
        conn=random.randint(1, 9999),
        bid=random.randint(1, 5),
        gid=random.randint(1, 100),
        cid=random.randint(1, 50),
        p=random.randint(0, 2),
        tid=random.randint(0, 9),
        ln=random.randint(100, 2000),
    )


def line_for(source: str, mode: str) -> str:
    if source == "postgres":
        pool = PG_BURST if mode == "burst" else PG_BASELINE
    elif source == "mysql":
        pool = MYSQL_BURST if mode == "burst" else MYSQL_BASELINE
    elif source == "kafka":
        pool = KAFKA_BURST if mode == "burst" else KAFKA_BASELINE
    else:
        return f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] unknown source"
    return _render(random.choice(pool))
