"""MCP 도구를 LangChain Tool 로 wrap — swarm/ReAct 에이전트가 호출할 수 있도록.

같은 MCPClient 를 재사용하므로 인증/retry/budget 가드는 그대로 유지된다.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.tools import tool

from .mcp_client import MCPClient

_client: MCPClient | None = None


def _get_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client


def _truncate(obj: Any, max_chars: int = 8000) -> str:
    """LLM 컨텍스트 폭주 방지용 — JSON 문자열로 직렬화 후 길이 제한."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    if len(s) > max_chars:
        return s[:max_chars] + f"\n... (truncated, total {len(s)} chars)"
    return s


# ───────────────────────── OS / 인프라 ─────────────────────────
# Prometheus: pab1it0/prometheus-mcp-server (community-prometheus target)
# CloudWatch: awslabs.cloudwatch-mcp-server (awslabs-cloudwatch target)


@tool
def prometheus_query(query: str, time: str | None = None) -> str:
    """[Prometheus metric · instant] self-hosted Prometheus 의 한 시점 PromQL 평가 (pab1it0/prometheus-mcp-server).

    이 Prometheus 는 node_exporter(호스트 OS) 뿐 아니라 **postgres_exporter / mysqld_exporter
    로 수집한 RDS 내부 지표**도 담고 있다. 따라서 다음 계열은 전부 이 도구로 질의한다:
      - DB exporter: pg_up, mysql_up, pg_stat_activity_count, pg_database_size_bytes,
        pg_stat_*, mysql_global_status_*, mysql_global_variables_* 등
      - 호스트 OS: node_* (load, fd, conntrack, fs.*)
    주의: 이런 exporter 메트릭은 awslabs cloudwatch 의 PromQL(execute_promql_query)에는 **없다**
    (그쪽은 CloudWatch vended 메트릭 전용). pg_*/mysql_*/node_* 이름은 반드시 이 도구를 쓴다.
    For trends use prometheus_range_query.

    Args:
        query: PromQL one-liner (instant vector compatible).
        time:  RFC3339 시각. 생략 시 서버 현재.

    Returns: `{status, data:{resultType:'vector', result:[{metric, value:[ts, "v"]}]}}`.
    """
    args: dict[str, Any] = {"query": query}
    if time:
        args["time"] = time
    r = _get_client().call("community-prometheus___execute_query", args)
    return _truncate(r or {})


@tool
def prometheus_range_query(query: str, start: str, end: str, step: str = "30s") -> str:
    """[Prometheus metric · range] self-hosted Prometheus 의 시계열 PromQL 평가 (pab1it0/prometheus-mcp-server).

    node_exporter(호스트 OS) 와 **postgres_exporter/mysqld_exporter 로 수집한 RDS 내부 지표**
    (pg_*, mysql_*, node_*) 의 추세를 본다. 이 exporter 메트릭은 cloudwatch PromQL 에는 없으니
    pg_*/mysql_*/node_* 은 반드시 이 도구를 쓴다. 어떤 metric 이름이 존재하는지 불확실하면
    prometheus_query 로 먼저 확인한다. training data 만으로 metric 존재를 가정하지 말 것.

    step sizing:
      - 결과 점 수 = (end - start) / step. range/step 가 ≈ 50~120 정도가 적당.
      - rate/increase 의 lookback ([Xm]) 은 step 보다 충분히 커야 함 (보통 step 의 3~5배).

    Args:
        query:     PromQL (range-vector compatible)
        start/end: RFC3339 UTC
        step:      '30s' / '1m' / '5m' 등

    Returns: `{status, data:{resultType:'matrix', result:[{metric, values:[[ts,"v"],...]}]}}`.
    """
    r = _get_client().call("community-prometheus___execute_range_query",
                           {"query": query, "start": start, "end": end, "step": step})
    return _truncate(r or {})


@tool
def cloudwatch_metric(namespace: str, metric_name: str, start_time: str, end_time: str,
                      dimensions: list[dict] | None = None,
                      statistic: str = "Average", period: int = 60) -> str:
    """[AWS managed metric · time-series] CloudWatch GetMetricData (awslabs cloudwatch-mcp).

    Use this for AWS-managed service metrics (RDS, Aurora, EC2, MSK, Lambda, S3, ELB).
    For self-managed host OS metrics (load, fd, conntrack), use prometheus_range_query.

    Empty result usually means: dimensions wrong / window has no traffic / metric name mismatch.
    Verify the dimension schema for the namespace before retrying.

    Statistic guidance:
      - 'Average' for general trend
      - 'Maximum' for burst peak (use this for IOPS / connection spike)
      - 'Sum' for cumulative volume

    Period: 60s default. For windows >6h consider 300. RDS metrics support 1-min granularity.

    For MSK consumer lag / topic throughput, prefer msk_metric — it auto-wires the
    Cluster Name + Topic + Consumer Group dimensions.

    Args:
        namespace:   e.g. 'AWS/EC2', 'AWS/RDS', 'AWS/Kafka', 'AWS/Lambda'
        metric_name: e.g. 'CPUUtilization', 'DatabaseConnections', 'ReadIOPS', 'FreeableMemory'
        start_time / end_time: ISO8601 UTC
        dimensions:  [{'Name': key, 'Value': value}, ...]
        statistic:   'Average' / 'Maximum' / 'Minimum' / 'Sum' / 'SampleCount'
        period:      seconds (60 / 300 / 3600)

    Returns: `{metricDataResults: [{id, label, timestamps, values, statusCode}], messages}`.
    """
    args: dict[str, Any] = {
        "namespace":   namespace,
        "metric_name": metric_name,
        "start_time":  start_time,
        "end_time":    end_time,
        "statistic":   statistic,
        "period":      period,
    }
    if dimensions:
        args["dimensions"] = dimensions
    r = _get_client().call("awslabs-cloudwatch___get_metric_data", args)
    return _truncate(r or {})


@tool
def cloudwatch_get_active_alarms(max_items: int = 50) -> str:
    """현재 ALARM 상태인 CloudWatch 알람 목록 (awslabs cloudwatch-mcp).

    Args:
        max_items: 최대 반환 수
    """
    r = _get_client().call("awslabs-cloudwatch___get_active_alarms", {"max_items": max_items})
    return _truncate(r or {})


@tool
def cloudwatch_get_alarm_history(alarm_name: str, max_items: int = 30) -> str:
    """특정 알람의 상태 전이 이력.

    Args:
        alarm_name: 알람 이름
        max_items: 최대 항목 수
    """
    r = _get_client().call("awslabs-cloudwatch___get_alarm_history",
                           {"alarm_name": alarm_name, "max_items": max_items})
    return _truncate(r or {})


@tool
def cloudwatch_describe_log_groups(log_group_name_prefix: str | None = None,
                                    max_items: int = 50) -> str:
    """CloudWatch Logs 의 log group 목록.

    Args:
        log_group_name_prefix: 이름 prefix 필터
        max_items: 최대 반환 수
    """
    args: dict[str, Any] = {"max_items": max_items}
    if log_group_name_prefix:
        args["log_group_name_prefix"] = log_group_name_prefix
    r = _get_client().call("awslabs-cloudwatch___describe_log_groups", args)
    return _truncate(r or {})


@tool
def cloudwatch_execute_log_insights_query(log_group_names: list[str],
                                           query_string: str,
                                           start_time: str, end_time: str,
                                           limit: int = 100) -> str:
    """[Log · query] CloudWatch Logs Insights query (awslabs cloudwatch-mcp).

    Use this for frequency / pattern aggregation across one or more log groups (stats by bin),
    not just plain grep. Discover concrete group names with cloudwatch_describe_log_groups
    before calling — do not guess.

    Args:
        log_group_names: list of CloudWatch Logs group names (e.g., '/aws/...', '/dbaops/poc/postgres')
        query_string:    Logs Insights query (pipe-separated stages)
        start_time / end_time: ISO8601 UTC
        limit:           result-row cap (default 100). stats outputs are not bounded by limit.

    Returns: `{queryId, status, results:[{field:val, ...}], statistics}`.
    """
    r = _get_client().call("awslabs-cloudwatch___execute_log_insights_query", {
        "log_group_names": log_group_names,
        "query_string":    query_string,
        "start_time":      start_time,
        "end_time":        end_time,
        "limit":           limit,
    })
    return _truncate(r or {}, max_chars=14000)


# ───────────────────────── DB (PG / MySQL) ─────────────────────────
# PG:    crystaldba/postgres-mcp restricted RO (community-postgres target)
# MySQL: benborla/mcp-server-mysql RO default (community-mysql target)


@tool
def pg_execute_sql(sql: str) -> str:
    """Aurora PG 에 read-only SQL 실행 (postgres-mcp restricted mode).

    DML/DDL 은 서버 레벨에서 거부됨. SELECT 만 가능.
    """
    r = _get_client().call("community-postgres___execute_sql", {"sql": sql})
    return _truncate(r or {})


@tool
def pg_explain_query(sql: str, hypothetical_indexes: list[dict] | None = None) -> str:
    """[PG · EXPLAIN] PG 의 실행계획 분석. **SELECT 본문만 넣으세요 — 'EXPLAIN' 접두 금지** (도구가 자동으로 붙입니다).

    가상 인덱스(hypothetical_indexes)로 'CREATE INDEX 했을 때' 시뮬레이션 가능.

    Args:
        sql: 분석할 **SELECT 한 줄** — `EXPLAIN`/`EXPLAIN ANALYZE`/`EXPLAIN (FORMAT JSON)` 접두를 붙이지 마세요.
             도구가 내부적으로 EXPLAIN [ANALYZE] [FORMAT] 을 자동 wrap. 접두를 붙이면 'EXPLAIN EXPLAIN ...' 이 되어 파서 거부.
             OK :   `SELECT * FROM dbaops_orders WHERE user_id=123`
             BAD:   `EXPLAIN SELECT * FROM ...`  ← 거부됨
        hypothetical_indexes: [{"table":"orders","columns":["user_id"],"using":"btree"}, ...] — 가상 인덱스 시뮬.
    """
    # idempotent 안전장치 — agent 가 EXPLAIN 붙여 보내도 자동 stripping.
    cleaned = sql.strip().rstrip(";")
    upper = cleaned.upper().lstrip()
    while upper.startswith("EXPLAIN"):
        # EXPLAIN, EXPLAIN ANALYZE, EXPLAIN (FORMAT JSON), EXPLAIN VERBOSE 등 접두 모두 제거
        # 다음 SQL keyword (SELECT/WITH/INSERT/UPDATE/DELETE) 까지 잘라내기
        m = re.search(r"\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b", cleaned, re.IGNORECASE)
        if not m:
            break
        cleaned = cleaned[m.start():]
        upper = cleaned.upper().lstrip()
    args: dict[str, Any] = {"sql": cleaned}
    if hypothetical_indexes:
        args["hypothetical_indexes"] = hypothetical_indexes
    r = _get_client().call("community-postgres___explain_query", args)
    return _truncate(r or {}, max_chars=12000)


@tool
def pg_analyze_db_health(health_type: str = "all") -> str:
    """PG 데이터베이스 헬스 종합 분석 — buffer/cache/connections/replication/vacuum 등.

    Args:
        health_type: 'all' / 'index' / 'connection' / 'vacuum' / 'sequence' / 'replication' / 'buffer' / 'constraint'
    """
    r = _get_client().call("community-postgres___analyze_db_health", {"health_type": health_type})
    return _truncate(r or {})


@tool
def pg_get_top_queries(sort_by: str = "resources", limit: int = 10) -> str:
    """pg_stat_statements 기반 top 쿼리.

    Args:
        sort_by: 'resources' / 'mean_time' / 'total_time'
        limit: 반환 쿼리 수
    """
    r = _get_client().call("community-postgres___get_top_queries",
                           {"sort_by": sort_by, "limit": limit})
    return _truncate(r or {})


@tool
def pg_analyze_workload_indexes(max_index_size_mb: int = 10000) -> str:
    """워크로드 분석 후 추가 인덱스 권고."""
    r = _get_client().call("community-postgres___analyze_workload_indexes",
                           {"max_index_size_mb": max_index_size_mb})
    return _truncate(r or {})


@tool
def pg_list_schemas() -> str:
    """PG 의 모든 스키마 목록."""
    r = _get_client().call("community-postgres___list_schemas", {})
    return _truncate(r or {})


@tool
def pg_list_objects(schema_name: str, object_type: str = "table") -> str:
    """스키마 내 객체 목록.

    Args:
        schema_name: 'public', 'information_schema' 등
        object_type: 'table' / 'view' / 'sequence' / 'extension'
    """
    r = _get_client().call("community-postgres___list_objects",
                           {"schema_name": schema_name, "object_type": object_type})
    return _truncate(r or {})


@tool
def mysql_query(sql: str) -> str:
    """[MySQL · RO SQL] RDS MySQL read-only SELECT (benborla mcp-server-mysql).

    Use cases (검증된 출처):
      - mysql.slow_log SELECT — log_output=TABLE 이라 직접 SELECT 가능.
      - performance_schema (events_statements_summary_by_digest, data_lock_waits, processlist).
      - information_schema (STATISTICS, TABLES, COLUMNS).
      - 사용자 스키마 SELECT (LIMIT 필수).

    Constraints:
      - DML/DDL 차단 (INSERT/UPDATE/DELETE/CREATE/DROP). SELECT 만.
      - 결과는 14000자에서 truncate. 큰 테이블 SELECT * 금지 — LIMIT 필수.
      - 파서가 EXPLAIN 뒤에 SELECT/WITH 만 허용 (EXPLAIN ANALYZE / FORMAT=... 거부).
        실행계획은 mysql_explain 사용.

    Args:
        sql: SELECT 한 줄.
    """
    r = _get_client().call("community-mysql___mysql_query", {"sql": sql})
    return _truncate(r or {}, max_chars=14000)


@tool
def mysql_explain(sql: str) -> str:
    """[MySQL · EXPLAIN] MySQL 실행계획. **SELECT 본문만 넣으세요** (도구가 'EXPLAIN ' 자동 prepend).

    제약:
      - benborla mcp-server-mysql 의 SQL 파서는 `EXPLAIN` 뒤에 `SELECT`/`WITH` 만 허용.
        `EXPLAIN ANALYZE` / `EXPLAIN FORMAT=TREE` / `EXPLAIN FORMAT=JSON` 은 모두 거부됨 (파서 한계).
        따라서 본 도구는 **plain `EXPLAIN <SELECT>`** 만 실행. 결과 형식: tabular (id/select_type/table/type/possible_keys/key/rows/Extra).
      - 'ANALYZE' 같은 실측 통계가 필요하면 우회 — performance_schema.events_statements_summary_by_digest 의 sum_no_index_used / rows_examined / sum_timer_wait 를 보세요.

    Args:
        sql: **SELECT 한 줄** — 'EXPLAIN'/'EXPLAIN ANALYZE'/'EXPLAIN FORMAT=...' 접두 금지 (붙여도 자동 strip).

    PG 는 pg_explain_query 를 쓰세요 (postgres-mcp 가 ANALYZE/JSON 모두 지원해 훨씬 풍부).
    """
    base = sql.strip().rstrip(";")
    m = re.search(r"\b(SELECT|WITH)\b", base, re.IGNORECASE)
    body = base[m.start():] if m else base
    r = _get_client().call("community-mysql___mysql_query", {"sql": f"EXPLAIN {body}"})
    return _truncate(r or {}, max_chars=12000)


@tool
def rds_performance_insights(db_id: str, start: str, end: str,
                             group_by: str = "db.sql_tokenized") -> str:
    """RDS Performance Insights 의 top SQL by AAS 를 가져온다.

    Args:
        db_id:   RDS dbi-resource-id (예 db-XXXXXX...)
        start/end: RFC3339
        group_by: PI group prefix (예 'db.sql_tokenized', 'db.wait_event', 'db.host', 'db.user').
                  dimension full name 도 허용 — Lambda 에서 prefix 로 잘라낸다.
    """
    r = _get_client().call("rds-pi___rds_performance_insights",
                           {"db_id": db_id, "start": start, "end": end, "group_by": group_by})
    return _truncate(r or {})


@tool
def msk_metric(cluster_arn: str, metric: str, start: str, end: str,
               stat: str = "Average", topic: str | None = None,
               consumer_group: str | None = None,
               period: int = 60) -> str:
    """MSK (AWS/Kafka) CloudWatch 메트릭 조회.

    중요 — 메트릭별로 필요한 dimension 이 다르다 (handler 가 자동 구성):
      - BytesInPerSec / BytesOutPerSec / MessagesInPerSec → Cluster Name + Topic 필수
      - MaxOffsetLag / SumOffsetLag / EstimatedMaxTimeLag → Cluster Name + Consumer Group + Topic 필수
      - UnderReplicatedPartitions / GlobalPartitionCount → Cluster Name (broker level)

    topic / consumer_group 인자를 명시하지 않으면 default (dbaops.orders / dbaops-paused) 사용.
    series 가 비어 있으면 (1) 시간 윈도 안에 트래픽 없음, 또는 (2) 잘못된 topic/consumer_group.

    Args:
        cluster_arn:    MSK cluster ARN. "msk-cluster" placeholder 도 OK.
        metric:         AWS/Kafka 메트릭명
        start/end:      RFC3339
        stat:           Average / Sum / Maximum / Minimum
        topic:          예 'dbaops.orders'. BytesIn/Out/MessagesIn/Lag 류 모두에 권장.
        consumer_group: 예 'dbaops-paused'. Lag 류 메트릭에 권장.
        period:         초 단위 (기본 60)
    """
    args: dict[str, Any] = {
        "cluster_arn": cluster_arn, "metric": metric,
        "start": start, "end": end, "stat": stat, "period": period,
    }
    if topic:
        args["topic"] = topic
    if consumer_group:
        args["consumer_group"] = consumer_group
    r = _get_client().call("msk-metrics___msk_metrics", args)
    series = (r or {}).get("series") or []
    return _truncate({
        "n_points":   len(series),
        "metric":     (r or {}).get("metric") or metric,
        "stat":       (r or {}).get("stat") or stat,
        "dimensions": (r or {}).get("dimensions") or [],
        "series":     series[:200],
    })


# ───────────────────────── Log ─────────────────────────


@tool
def s3_list_logs(bucket: str, prefix: str, since_minutes: int = 60,
                 max_keys: int = 50) -> str:
    """S3 prefix 아래 로그 객체 목록 — 어떤 key 가 존재하는지 먼저 탐색.

    log specialist 가 s3_log_fetch 를 호출하기 전에 이 도구로 객체 목록을
    먼저 받아야 한다 (key 를 추측해 호출하면 NoSuchKey 로 실패).

    Args:
        bucket: S3 버킷명 (infra log_bucket)
        prefix: 예 'logs-burst/postgres/' (디렉토리), 'logs/mysql/' 등
        since_minutes: 최근 N 분 안에 last_modified 된 것만 (기본 60)
        max_keys: 반환 객체 수 한도 (기본 50, 최대 1000)
    """
    r = _get_client().call("s3-log-fetch___s3_list_logs", {
        "bucket": bucket, "prefix": prefix,
        "since_minutes": since_minutes, "max_keys": max_keys,
    })
    objs = (r or {}).get("objects") or []
    return _truncate({
        "count": (r or {}).get("count", len(objs)),
        "is_truncated": (r or {}).get("is_truncated", False),
        "objects": objs[:max_keys],
    })


@tool
def s3_log_fetch(bucket: str, key: str, regex: str | None = None,
                 max_lines: int = 2000) -> str:
    """S3 의 gzip 로그 객체에서 정규식 매치 라인을 가져온다.

    Args:
        bucket: S3 버킷명
        key:    객체 키 (.gz / .log / .txt). 디렉토리 prefix 가 아닌 단일 객체 키여야 한다.
                키 모르면 먼저 `s3_list_logs(prefix=...)` 로 목록 조회.
        regex:  적용할 정규식 (None 이면 모든 라인)
        max_lines: 반환할 최대 라인 수
    """
    r = _get_client().call("s3-log-fetch___s3_log_fetch", {
        "bucket": bucket, "key": key, "regex": regex, "max_lines": max_lines,
    })
    lines = (r or {}).get("lines") or []
    return _truncate({"line_count": len(lines), "truncated": (r or {}).get("truncated", False),
                      "lines": lines[:max_lines]})


# ───────────────────────── AWS 인프라 (read-only) ─────────────────────────


def _aws_call(tool_name: str, args: dict) -> dict:
    """aws-api Lambda 의 dispatch wrapper — handler.py 의 _TOOLS 키와 일치."""
    payload = {"tool_name": tool_name, "arguments": args}
    return _get_client().call(f"aws-api___{tool_name}", payload) or {}


@tool
def aws_describe_rds_instances(db_instance_identifier: str | None = None,
                                max_records: int = 50) -> str:
    """AWS RDS DescribeDBInstances — 인스턴스 메타정보(엔진/버전/엔드포인트/PI/Multi-AZ 등) 조회.

    Args:
        db_instance_identifier: 특정 인스턴스 id(비우면 전체)
        max_records: 20~100
    """
    args: dict[str, Any] = {"max_records": max_records}
    if db_instance_identifier:
        args["db_instance_identifier"] = db_instance_identifier
    return _truncate(_aws_call("describe_rds_instances", args))


@tool
def aws_describe_rds_clusters(db_cluster_identifier: str | None = None) -> str:
    """AWS RDS DescribeDBClusters — Aurora 클러스터/멤버(쓰기/리더) 조회.

    Args:
        db_cluster_identifier: 특정 클러스터 id(비우면 전체)
    """
    args: dict[str, Any] = {}
    if db_cluster_identifier:
        args["db_cluster_identifier"] = db_cluster_identifier
    return _truncate(_aws_call("describe_rds_clusters", args))


@tool
def aws_describe_db_log_files(db_instance_identifier: str,
                               filename_contains: str | None = None) -> str:
    """RDS 인스턴스의 DB 엔진 로그 파일 목록(파일명/크기/마지막 갱신).

    Args:
        db_instance_identifier: RDS DBInstanceIdentifier
        filename_contains: 예 'error', 'slowquery', 'audit', 'postgresql.log'
    """
    args: dict[str, Any] = {"db_instance_identifier": db_instance_identifier}
    if filename_contains:
        args["filename_contains"] = filename_contains
    return _truncate(_aws_call("describe_db_log_files", args))


@tool
def aws_download_db_log_file_portion(db_instance_identifier: str, log_file_name: str,
                                      lines: int = 200, regex: str | None = None,
                                      marker: str | None = None) -> str:
    """RDS DB 엔진 로그 파일의 마지막 N 라인을 가져온다(MySQL slow/error, PG postgresql.log).

    marker 없이 호출하면 Lambda 가 자동으로 끝까지 페이지를 돌며 누적 (최대 50 페이지).
    regex 가 주어지면 매칭 라인만 필터링해 lines 줄 반환.

    Args:
        db_instance_identifier: RDS DBInstanceIdentifier
        log_file_name: describe_db_log_files 결과의 log_filename
        lines: 마지막 N 라인 (기본 200, 최대 1000)
        regex: 적용할 정규식 (예: 'still waiting|deadlock', 'Query Text:|duration:')
        marker: 이어 받을 marker(생략 시 끝까지 자동 페이징)
    """
    args: dict[str, Any] = {
        "db_instance_identifier": db_instance_identifier,
        "log_file_name": log_file_name,
        "lines": lines,
    }
    if regex:
        args["regex"] = regex
    if marker:
        args["marker"] = marker
    return _truncate(_aws_call("download_db_log_file_portion", args), max_chars=14000)


@tool
def aws_list_msk_clusters() -> str:
    """AWS MSK ListClustersV2 — Provisioned/Serverless 클러스터 목록."""
    return _truncate(_aws_call("list_msk_clusters", {}))


@tool
def aws_describe_ec2_instances(instance_ids: list[str] | None = None,
                                tag_name_contains: str | None = None,
                                max: int = 50) -> str:
    """AWS EC2 DescribeInstances — EC2 인스턴스 상태/타입/AZ/Name 태그 조회.

    Args:
        instance_ids: i-xxxx... 목록
        tag_name_contains: Name 태그 포함어 검색
        max: 기본 50, 최대 100
    """
    args: dict[str, Any] = {"max": max}
    if instance_ids:
        args["instance_ids"] = instance_ids
    if tag_name_contains:
        args["tag_name_contains"] = tag_name_contains
    return _truncate(_aws_call("describe_ec2_instances", args))


# NOTE: aws_list_cloudwatch_alarms / aws_list_metric_namespaces 는
# awslabs cloudwatch-mcp 의 cloudwatch_get_active_alarms / get_metric_metadata
# 로 대체되어 제거됨. supervisor 가 자동으로 라우팅.


@tool
def aws_describe_pi_dimensions(dbi_resource_id: str, metric: str = "db.load.avg",
                                group_by: str = "db.sql_tokenized",
                                start: str | None = None, end: str | None = None) -> str:
    """RDS PI DescribeDimensionKeys — 그룹별 top dimension(SQL/wait_event 등) 조회.

    Args:
        dbi_resource_id: RDS DbiResourceId(예 db-XXXXXXXX)
        metric: 예 db.load.avg
        group_by: PI group prefix — 'db.sql_tokenized' / 'db.wait_event' / 'db.host' / 'db.user'.
                  dimension full name 도 허용 (Lambda 에서 prefix 로 정규화).
        start/end: RFC3339 (생략 시 최근 1시간)
    """
    args: dict[str, Any] = {
        "dbi_resource_id": dbi_resource_id,
        "metric": metric,
        "group_by": group_by,
    }
    if start:
        args["start"] = start
    if end:
        args["end"] = end
    return _truncate(_aws_call("describe_pi_dimensions", args))


# ───────────────────────── awslabs aws-documentation MCP ─────────────────────────


@tool
def aws_doc_search(search_phrase: str, limit: int = 10) -> str:
    """AWS 공식 docs 검색 (awslabs aws-documentation-mcp).

    Args:
        search_phrase: 검색어 (예: 'Aurora PostgreSQL max_connections default')
        limit: 결과 수
    """
    r = _get_client().call("awslabs-aws-doc___search_documentation",
                           {"search_phrase": search_phrase, "limit": limit})
    return _truncate(r or {})


@tool
def aws_doc_read(url: str, max_length: int = 8000, start_index: int = 0) -> str:
    """AWS 공식 docs URL 의 본문을 markdown 으로 가져온다.

    Args:
        url: 'https://docs.aws.amazon.com/...' 형식 URL
        max_length: 한 번에 받을 최대 글자
        start_index: 페이지네이션용 offset
    """
    r = _get_client().call("awslabs-aws-doc___read_documentation",
                           {"url": url, "max_length": max_length, "start_index": start_index})
    return _truncate(r or {}, max_chars=12000)


@tool
def aws_doc_recommend(url: str) -> str:
    """특정 AWS docs 페이지와 관련된 추천 페이지 목록.

    Args:
        url: 기준이 될 AWS docs URL
    """
    r = _get_client().call("awslabs-aws-doc___recommend", {"url": url})
    return _truncate(r or {})


# ───────────────────────── awslabs aws-api MCP (call_aws fallback) ─────────────────────────


@tool
def aws_call_cli(cli_command: str) -> str:
    """임의 AWS CLI read-only 명령 실행 (awslabs aws-api-mcp).

    READ_OPERATIONS_ONLY=true 강제 — 변경/생성/삭제 명령 차단.
    PoC 의 우리 aws-api Lambda 가 안 만든 AWS API 도 이 fallback 으로 호출 가능.

    Args:
        cli_command: 'aws sts get-caller-identity' 같은 완성된 CLI 한 줄
    """
    r = _get_client().call("awslabs-aws-api___call_aws", {"cli_command": cli_command})
    return _truncate(r or {}, max_chars=12000)


@tool
def aws_suggest_cli(query: str) -> str:
    """자연어 → AWS CLI 명령 추천 (awslabs aws-api-mcp).

    Args:
        query: '내 RDS 인스턴스 다 보여줘' 같은 자연어
    """
    r = _get_client().call("awslabs-aws-api___suggest_aws_commands", {"query": query})
    return _truncate(r or {})


# ───────────────────────── 그룹 헬퍼 ─────────────────────────


AWS_TOOLS = [
    # 우리 aws-api Lambda (응답 정제된 7개)
    aws_describe_rds_instances,
    aws_describe_rds_clusters,
    aws_describe_db_log_files,
    aws_download_db_log_file_portion,
    aws_list_msk_clusters,
    aws_describe_ec2_instances,
    aws_describe_pi_dimensions,
    # awslabs cloudwatch-mcp 의 알람
    cloudwatch_get_active_alarms,
    cloudwatch_get_alarm_history,
    # awslabs aws-api-mcp fallback
    aws_call_cli,
    aws_suggest_cli,
]

OS_TOOLS = [
    # community pab1it0 prometheus
    prometheus_query,
    prometheus_range_query,
    # awslabs cloudwatch
    cloudwatch_metric,
]

DB_TOOLS = [
    # community postgres-mcp
    pg_execute_sql,
    pg_analyze_db_health,
    pg_get_top_queries,
    pg_list_schemas,
    pg_list_objects,
    # community mysql-mcp
    mysql_query,
    # 우리 PoC 특화
    rds_performance_insights,
    msk_metric,
    # awslabs cloudwatch (RDS 메트릭 조회용으로 공유)
    cloudwatch_metric,
]

LOG_TOOLS = [
    # 우리 s3
    s3_list_logs,
    s3_log_fetch,
    # 우리 aws-api 의 RDS 엔진 로그
    aws_describe_db_log_files,
    aws_download_db_log_file_portion,
    # awslabs cloudwatch Logs Insights
    cloudwatch_describe_log_groups,
    cloudwatch_execute_log_insights_query,
]

QUERY_TOOLS = [
    pg_explain_query,
    pg_analyze_workload_indexes,
    pg_execute_sql,
    mysql_explain,
    mysql_query,
]

DOCS_TOOLS = [
    aws_doc_search,
    aws_doc_read,
    aws_doc_recommend,
]


def _connections_infra_context() -> dict[str, str]:
    """올인원 EC2: connections.json 의 infra_context 섹션 (UI 연결설정 페이지가 write).

    env 가 비어있을 때의 fallback 소스. 파일이 없거나 키가 없으면 빈 dict.
    """
    path = os.environ.get(
        "DBAOPS_CONNECTIONS_PATH",
        os.path.join(os.environ.get("DBAOPS_DATA_DIR", "/data"), "connections.json"),
    )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ctx = data.get("infra_context")
        return ctx if isinstance(ctx, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def infra_context() -> dict[str, str]:
    """인프라 식별자(prom instance id, aurora writer id 등).

    우선순위: 환경변수 INFRA_* > connections.json infra_context > 빈 문자열.
    (PoC 의 dbaops-poc-* 하드코딩 default 는 제거 — 고객 환경에선 무의미.)
    """
    conn = _connections_infra_context()

    def pick(env_key: str, conn_key: str) -> str:
        return os.environ.get(env_key) or conn.get(conn_key) or ""

    return {
        "prom_instance_id":  pick("INFRA_PROM_INSTANCE_ID", "prom_instance_id"),
        "aurora_cluster_id": pick("INFRA_AURORA_CLUSTER_ID", "aurora_cluster_id"),
        "aurora_writer_id":  pick("INFRA_AURORA_WRITER_ID", "aurora_writer_id"),
        "aurora_reader_id":  pick("INFRA_AURORA_READER_ID", "aurora_reader_id"),
        "mysql_db_id":       pick("INFRA_MYSQL_DB_ID", "mysql_db_id"),
        "msk_cluster_name":  pick("INFRA_MSK_CLUSTER_NAME", "msk_cluster_name"),
        "log_bucket":        pick("INFRA_LOG_BUCKET", "log_bucket"),
    }
