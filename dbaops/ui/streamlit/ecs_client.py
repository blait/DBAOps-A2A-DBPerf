"""ECS task 조회/실행 헬퍼 — Streamlit 'Generators' 패널용."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
CLUSTER = os.environ.get("ECS_CLUSTER", "dbaops-poc")

# 시나리오 카탈로그 — UI 카드 + ECS RunTask 양쪽에 동일 사용.
# 각 항목 키:
#   key:       내부 식별자
#   category:  "data" (실제 부하) / "log" (로그 burst)
#   icon, title, summary, impact, signals, suggested_lens, suggested_prompt — UI 카드용
#   label, task_def, duration, env — RunTask 호출용
SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "data-baseline",
        "category": "data",
        "icon": "🌊",
        "title": "정상 트래픽 (Baseline)",
        "summary": "PG 50 TPS / MySQL 30 QPS / Kafka 100 msg/s 가벼운 정상 트래픽을 60초간 발생시킵니다. 비교 베이스라인용.",
        "impact": [
            "PG: dbaops_orders INSERT/SELECT, dbaops_hot_counter UPDATE",
            "MySQL: dbaops_orders INSERT/SELECT",
            "Kafka: dbaops.orders topic 으로 producer 메시지",
        ],
        "signals": [
            "AWS/RDS DatabaseConnections 살짝 증가",
            "Aurora pg_stat_database 의 xact_commit 증가",
            "MSK BytesInPerSec 약 100 msg/s",
        ],
        "suggested_lens": "multi",
        "suggested_prompt": "최근 1시간 호스트와 DB 양쪽의 베이스라인 트래픽이 어떤지 요약해줘.",
        "label": "🌊 Baseline 트래픽 (60초)",
        "task_def": "dbaops-poc-data-baseline",
        "duration": 60,
        "env": [],
    },
    {
        "key": "data-lock-contention",
        "category": "data",
        "icon": "🔒",
        "title": "PG 락 경합 (lock contention)",
        "summary": "8 worker 가 PG dbaops_hot_counter (1행 테이블)에 동시에 SELECT FOR UPDATE → row-level lock 직렬화. 3분.",
        "impact": [
            "pg_stat_activity 에 wait_event=Lock/transactionid·tuple 다수",
            "트랜잭션이 'idle in transaction' 상태로 락 보유",
            "처리량(throughput) 직렬화로 급락",
        ],
        "signals": [
            "pg_locks 의 not granted 행 증가",
            "RDS Performance Insights 의 Lock wait 비중 급등",
            "Aurora CPU 가 connection 수 대비 비정상 상승",
        ],
        "suggested_lens": "db",
        "suggested_prompt": "Aurora 락 경합이 의심된다. 활성 세션과 hot row 락 / 보유자를 분석해줘.",
        "label": "🔒 PG 락 경합 (3분)",
        "task_def": "dbaops-poc-data-lock-contention",
        "duration": 180,
        "env": [],
    },
    {
        "key": "data-slow-query",
        "category": "data",
        "icon": "🐢",
        "title": "MySQL 슬로우 쿼리 (full scan)",
        "summary": "MySQL dbaops_orders.user_id 에 인덱스가 없어 30만 행 풀스캔이 반복됩니다. 2분.",
        "impact": [
            "MySQL CPUUtilization 30% 지속",
            "EXPLAIN: type=ALL, possible_keys=NULL, rows ≈ 308K",
            "performance_schema digests 가 OFF 상태에서도 slow log 에 기록",
        ],
        "signals": [
            "RDS SlowQuery 로그 burst",
            "AWS/RDS ReadIOPS 증가",
            "ConnectionCount 는 적은데 CPU 만 상승",
        ],
        "suggested_lens": "db",
        "suggested_prompt": "MySQL 의 슬로우 쿼리와 인덱스 상태를 점검하고 EXPLAIN 으로 비효율 지점을 찾아줘.",
        "label": "🐢 MySQL 슬로우 쿼리 (2분)",
        "task_def": "dbaops-poc-data-slow-query",
        "duration": 120,
        "env": [],
    },
    {
        "key": "data-connection-spike",
        "category": "data",
        "icon": "🌪",
        "title": "PG 연결 스파이크 (connection burst)",
        "summary": "PG 에 200개의 connection 을 35초씩 잡았다 풀었다 60초 간격으로 3회 반복 (총 3분). CloudWatch 1-min sampling 에 안정적으로 200 connections 가 잡힘.",
        "impact": [
            "DatabaseConnections 메트릭이 sampling window 마다 200 까지 폭등",
            "PG max_connections 근접 가능 (FATAL: too many connections 위험)",
            "각 burst 가 35초 hold 라 1-min CloudWatch aggregate 의 Maximum 통계가 안정적으로 caputre",
        ],
        "signals": [
            "AWS/RDS DatabaseConnections 1-min Maximum=200 (반복 burst)",
            "pg_stat_activity 활성 세션 200 (burst 시점)",
            "PG 로그에 connection authorized 라인 burst",
        ],
        "suggested_lens": "multi",
        "suggested_prompt": "PG 연결 수 폭증이 의심된다. 호스트 네트워크와 DB 연결 양쪽을 같이 분석해줘.",
        "label": "🌪 PG 연결 스파이크 (3분)",
        "task_def": "dbaops-poc-data-connection-spike",
        "duration": 180,
        "env": [],
    },
    {
        "key": "data-cpu-burn",
        "category": "data",
        "icon": "🔥",
        "title": "Aurora PG 호스트 CPU saturation",
        "summary": "8 worker 가 in-memory 집계(`generate_series 3M + md5+random`)를 병렬로 반복 → Aurora db.t4g.medium(2 vCPU) 의 CPUUtilization 을 90% 이상으로 saturate. 3분간 1-min CW sample 3개 모두 캡처되도록 설계.",
        "impact": [
            "AWS/RDS CPUUtilization 90~100% 로 step up (baseline 대비 명백한 단차)",
            "Performance Insights DBLoad 의 CPU wait 비중 급등 (IO/Lock 대비)",
            "ReadIOPS / WriteIOPS 는 거의 변동 없음 → 순수 CPU 시그널",
        ],
        "signals": [
            "AWS/RDS `CPUUtilization` 1-min Max ≥ 90%",
            "AWS/RDS `CPUCreditBalance` 감소 (t4g 는 burstable — credit 소진 추세)",
            "Performance Insights `DBLoad`(group=db.wait_event) 의 CPU 비중 우세",
            "DatabaseConnections 는 8개 부근 평탄 — 연결 폭주가 아님",
        ],
        "suggested_lens": "os",
        "suggested_prompt": "Aurora PG 호스트 CPU 가 최근에 갑자기 높아졌어. CPUUtilization / CPUCreditBalance 추세와 peak 시점, baseline 대비 격차를 정리해줘.",
        "label": "🔥 PG 호스트 CPU saturation (3분)",
        "task_def": "dbaops-poc-data-cpu-burn",
        "duration": 180,
        "env": [],
    },
    {
        "key": "data-disk-io-burst",
        "category": "data",
        "icon": "💾",
        "title": "Aurora PG 디스크 IO + 메모리 압박",
        "summary": "약 5GB 워킹셋(3M rows × 1.5KB) 시드 후 random PK SELECT (6 reader) + INSERT/UPDATE (3 writer) 4분 반복. 워킹셋이 db.t4g.medium 의 RAM 4GB 를 초과해 buffer cache eviction → ReadIOPS 가 명확히 spike. ⚠️ 첫 실행은 시드 단계 5~10분 추가 소요 (이후 PG 에 잔존, skip).",
        "impact": [
            "AWS/RDS ReadIOPS / WriteIOPS 동시 spike — workload 시작 시점에 step up",
            "AWS/RDS FreeableMemory 감소 (워킹셋이 shared_buffers 초과)",
            "AWS/RDS NetworkReceive/TransmitThroughput 동반 상승 (INSERT payload + SELECT 결과)",
        ],
        "signals": [
            "AWS/RDS `VolumeReadIOPs` 1-min Max 가 baseline 대비 10x 이상",
            "AWS/RDS `ReadLatency` / `WriteLatency` 동반 상승 (cache miss 에서 disk 까지)",
            "AWS/RDS `FreeableMemory` 감소 추세 (메모리 압박)",
            "AWS/RDS `NetworkReceiveThroughput` 상승",
        ],
        "suggested_lens": "os",
        "suggested_prompt": "Aurora PG 의 디스크 IO 와 메모리 메트릭이 최근 spike 했다. ReadIOPS / WriteIOPS / FreeableMemory / NetworkReceive 의 추세와 이상 지점을 정리해줘.",
        "label": "💾 PG 디스크 IO burst (4분)",
        "task_def": "dbaops-poc-data-disk-io-burst",
        "duration": 240,
        "env": [],
    },
    {
        "key": "data-kafka-isr-shrink",
        "category": "data",
        "icon": "🌀",
        "title": "Kafka 컨슈머 lag (paused consumer)",
        "summary": "producer 가 5000 batch 를 한꺼번에 밀고 consumer 는 paused → ConsumerLag 누적. 60초.",
        "impact": [
            "MSK BytesInPerSec 급등 후 BytesOutPerSec 가 따라가지 못함",
            "MaxOffsetLag (consumer lag) 누적",
            "MSK Serverless 는 ISR 메트릭 노출 제한 — lag 메트릭 위주로 관찰",
        ],
        "signals": [
            "AWS/Kafka MaxOffsetLag 증가",
            "BytesIn vs BytesOut 격차 확대",
            "consumer group dbaops-paused 의 offset 정체",
        ],
        "suggested_lens": "db",
        "suggested_prompt": "Kafka consumer lag 이 누적되고 있다. lag 추세와 BytesIn/Out 격차를 분석해줘.",
        "label": "🌀 Kafka 컨슈머 lag (60초)",
        "task_def": "dbaops-poc-data-kafka-isr-shrink",
        "duration": 60,
        "env": [],
    },
    {
        "key": "log-postgres-burst",
        "category": "log",
        "icon": "📕",
        "title": "PG 에러 로그 burst",
        "summary": "deadlock detected / FATAL: too many connections / waits-for 라인을 50/s 로 3분간 S3 logs-burst/postgres/ 에 적재.",
        "impact": [
            "S3 logs-burst/postgres/ 경로에 .log.gz 파일 누적",
            "agent 의 log_specialist / s3_log_fetch 가 정규식으로 패턴 검출",
            "deadlock 387 + FATAL 388 등 빈도 카운트 (실측)",
        ],
        "signals": [
            "S3 ListObjects 결과 logs-burst/postgres/ 아래 객체 수 급증",
            "Drain3 분류: 'ERROR: deadlock detected' / 'FATAL: too many connections' 템플릿이 빈도 1위",
        ],
        "suggested_lens": "log",
        "suggested_prompt": "최근 PG 에러 로그가 폭증했다. S3 logs-burst/postgres/ 아래 .log.gz 들을 deadlock / FATAL 패턴으로 분석해줘.",
        "label": "📕 PG 에러 로그 burst (3분)",
        "task_def": "dbaops-poc-log-postgres",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
    {
        "key": "log-mysql-burst",
        "category": "log",
        "icon": "📘",
        "title": "MySQL 에러 로그 burst",
        "summary": "[ERROR] InnoDB / [MY-013183] / Query_time 큰 slow 라인을 50/s 로 3분간 S3 logs-burst/mysql/ 에 적재.",
        "impact": [
            "S3 logs-burst/mysql/ 경로에 객체 누적",
            "agent 의 log_specialist 가 InnoDB 에러 / slow query 패턴 분류",
        ],
        "signals": [
            "Drain3 분류: '[ERROR] InnoDB ...' 템플릿 빈도",
            "Query_time / Lock_time 분포",
        ],
        "suggested_lens": "log",
        "suggested_prompt": "MySQL 에러 로그가 갑자기 늘었다. InnoDB 어설션·slow query 패턴을 분류해 RCA 후보를 알려줘.",
        "label": "📘 MySQL 에러 로그 burst (3분)",
        "task_def": "dbaops-poc-log-mysql",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
    {
        "key": "log-kafka-burst",
        "category": "log",
        "icon": "📗",
        "title": "Kafka 에러 로그 burst",
        "summary": "Shrinking ISR / Could not append / connect task failure 라인을 50/s 로 3분간 S3 logs-burst/kafka/ 에 적재.",
        "impact": [
            "S3 logs-burst/kafka/ 경로에 server.log/connect.log 모사 객체 누적",
            "agent 의 log_specialist 가 ISR shrink / Connect task fail 패턴 분류",
        ],
        "signals": [
            "Drain3 분류: 'Shrinking ISR for partition ...' 템플릿",
            "WARN/ERROR 비중 집계",
        ],
        "suggested_lens": "log",
        "suggested_prompt": "Kafka 에러 로그가 burst 되고 있다. ISR shrink / Connect task fail 패턴 빈도를 분석해줘.",
        "label": "📗 Kafka 에러 로그 burst (3분)",
        "task_def": "dbaops-poc-log-kafka",
        "duration": 180,
        "env": [
            {"name": "MODE",          "value": "burst"},
            {"name": "LINES_PER_SEC", "value": "50"},
            {"name": "S3_PREFIX",     "value": "logs-burst"},
        ],
    },
]


def _ecs():
    return boto3.client("ecs", region_name=REGION)


def list_running_tasks() -> list[dict[str, Any]]:
    ecs = _ecs()
    arns = ecs.list_tasks(cluster=CLUSTER, desiredStatus="RUNNING").get("taskArns") or []
    if not arns:
        return []
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") or []
    out: list[dict[str, Any]] = []
    for t in desc:
        family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
        started = t.get("startedAt") or t.get("createdAt")
        if isinstance(started, datetime):
            started_s = started.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            started_s = str(started or "")
        out.append({
            "family":        family,
            "task_id":       (t.get("taskArn") or "").rsplit("/", 1)[-1],
            "last_status":   t.get("lastStatus"),
            "container":     (t.get("containers") or [{}])[0].get("lastStatus"),
            "started_at":    started_s,
        })
    return out


def list_recent_stopped(limit: int = 10) -> list[dict[str, Any]]:
    ecs = _ecs()
    arns = ecs.list_tasks(cluster=CLUSTER, desiredStatus="STOPPED").get("taskArns") or []
    arns = arns[:limit]
    if not arns:
        return []
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") or []
    out: list[dict[str, Any]] = []
    for t in desc:
        family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
        stopped = t.get("stoppedAt") or t.get("executionStoppedAt")
        out.append({
            "family":         family,
            "task_id":        (t.get("taskArn") or "").rsplit("/", 1)[-1],
            "stop_code":      t.get("stopCode"),
            "stopped_reason": (t.get("stoppedReason") or "")[:80],
            "stopped_at":     stopped.astimezone(timezone.utc).isoformat(timespec="seconds") if isinstance(stopped, datetime) else "",
            "exit_code":      (t.get("containers") or [{}])[0].get("exitCode"),
        })
    return out


def trigger_scenario(key: str, *, subnets: list[str], security_groups: list[str] | None = None) -> dict[str, Any]:
    sc = next((s for s in SCENARIOS if s["key"] == key), None)
    if sc is None:
        raise ValueError(f"unknown scenario {key}")
    env = list(sc["env"])
    env.append({"name": "DURATION_SEC", "value": str(sc["duration"])})

    netcfg: dict[str, Any] = {
        "subnets": subnets,
        "assignPublicIp": "DISABLED",
    }
    if security_groups:
        netcfg["securityGroups"] = security_groups

    container_name = "log-gen" if sc["task_def"].startswith("dbaops-poc-log-") else "data-gen"

    resp = _ecs().run_task(
        cluster=CLUSTER,
        launchType="FARGATE",
        taskDefinition=sc["task_def"],
        networkConfiguration={"awsvpcConfiguration": netcfg},
        overrides={"containerOverrides": [{"name": container_name, "environment": env}]},
    )
    tasks = resp.get("tasks") or []
    failures = resp.get("failures") or []
    if not tasks:
        return {"ok": False, "failures": failures}
    return {
        "ok": True,
        "task_id": (tasks[0].get("taskArn") or "").rsplit("/", 1)[-1],
        "family": sc["task_def"],
    }


def default_subnets() -> list[str]:
    csv = os.environ.get("ECS_SUBNETS", "")
    return [s.strip() for s in csv.split(",") if s.strip()]


def default_security_groups() -> list[str]:
    csv = os.environ.get("ECS_SECURITY_GROUPS", "")
    return [s.strip() for s in csv.split(",") if s.strip()]


# ───────────────────────── 단일 task 진행 추적 ─────────────────────────


def describe_task(task_id: str) -> dict[str, Any] | None:
    """단일 task 상태 + 컨테이너 + 로그 stream 정보."""
    ecs = _ecs()
    arn = task_id if task_id.startswith("arn:") else f"arn:aws:ecs:{REGION}:{_account_id()}:task/{CLUSTER}/{task_id}"
    desc = ecs.describe_tasks(cluster=CLUSTER, tasks=[arn]).get("tasks") or []
    if not desc:
        return None
    t = desc[0]
    family = (t.get("taskDefinitionArn") or "").rsplit("/", 1)[-1]
    container = (t.get("containers") or [{}])[0]

    # CW Logs stream 정보 추출 — task definition 의 logConfiguration 에서
    log_group = log_stream = None
    try:
        td = ecs.describe_task_definition(taskDefinition=family).get("taskDefinition") or {}
        for c in td.get("containerDefinitions") or []:
            if c.get("name") == container.get("name"):
                lc = (c.get("logConfiguration") or {}).get("options") or {}
                log_group = lc.get("awslogs-group")
                prefix = lc.get("awslogs-stream-prefix")
                if log_group and prefix and container.get("name"):
                    # awslogs stream name = "<prefix>/<container_name>/<task_id>"
                    log_stream = f"{prefix}/{container.get('name')}/{(t.get('taskArn') or '').rsplit('/', 1)[-1]}"
                break
    except Exception:  # noqa: BLE001
        pass

    def _iso(v: Any) -> str | None:
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat(timespec="seconds")
        return str(v) if v else None

    return {
        "task_id":          (t.get("taskArn") or "").rsplit("/", 1)[-1],
        "family":           family,
        "last_status":      t.get("lastStatus"),
        "desired_status":   t.get("desiredStatus"),
        "stop_code":        t.get("stopCode"),
        "stopped_reason":   (t.get("stoppedReason") or "") or None,
        "container_name":   container.get("name"),
        "container_status": container.get("lastStatus"),
        "exit_code":        container.get("exitCode"),
        "exit_reason":      container.get("reason"),
        "created_at":       _iso(t.get("createdAt")),
        "started_at":       _iso(t.get("startedAt")),
        "stopped_at":       _iso(t.get("stoppedAt")),
        "log_group":        log_group,
        "log_stream":       log_stream,
    }


_ACCOUNT_ID: str | None = None


def _account_id() -> str:
    global _ACCOUNT_ID
    if _ACCOUNT_ID is None:
        _ACCOUNT_ID = boto3.client("sts", region_name=REGION).get_caller_identity().get("Account") or ""
    return _ACCOUNT_ID


def stop_task(task_id: str, reason: str = "stopped from streamlit") -> dict[str, Any]:
    arn = task_id if task_id.startswith("arn:") else f"arn:aws:ecs:{REGION}:{_account_id()}:task/{CLUSTER}/{task_id}"
    return _ecs().stop_task(cluster=CLUSTER, task=arn, reason=reason)


# ───────────────────────── CloudWatch Logs tail ─────────────────────────


def tail_log_events(log_group: str, log_stream: str, *, start_from_head: bool = True,
                    next_token: str | None = None, limit: int = 200) -> dict[str, Any]:
    """get_log_events 한 번 호출. 다음 token + events 를 반환.

    nextForwardToken 으로 다음 호출 시 이어서 읽을 수 있다.
    """
    cw = boto3.client("logs", region_name=REGION)
    kwargs: dict[str, Any] = {
        "logGroupName": log_group,
        "logStreamName": log_stream,
        "limit": limit,
        "startFromHead": start_from_head,
    }
    if next_token:
        kwargs["nextToken"] = next_token
        kwargs.pop("startFromHead", None)
    try:
        resp = cw.get_log_events(**kwargs)
    except cw.exceptions.ResourceNotFoundException:
        return {"events": [], "next_token": next_token, "ready": False}

    events = []
    for e in resp.get("events") or []:
        ts = e.get("timestamp")
        events.append({
            "ts": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                  if isinstance(ts, (int, float)) else None,
            "message": e.get("message") or "",
        })
    return {
        "events":     events,
        "next_token": resp.get("nextForwardToken"),
        "ready":      True,
    }
