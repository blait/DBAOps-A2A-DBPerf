"""aws_api Lambda — boto3 read-only AWS API 묶음 도구.

Gateway target 한 개에 여러 sub-tool 을 inline 으로 노출. Lambda 가 받는 event 의
'tool_name' 으로 분기하거나, AgentCore Gateway 가 도구별로 호출 시 직접 dispatch.

각 도구는 read-only (describe/list/get) 만. Gateway IAM 도 그에 맞게 제한.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ─────────────────────── 헬퍼 ───────────────────────


def _client(service: str, region: str | None = None):
    return boto3.client(service, region_name=region)


def _payload(event: dict) -> dict:
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, dict):
        return body
    return event if isinstance(event, dict) else {}


def _serialize(obj: Any) -> Any:
    """boto3 응답에 datetime 이 섞여있어 JSON 직렬화 helper."""
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    return obj


def _truncate(obj: Any, max_chars: int = 12000) -> Any:
    """크기 제한 — LLM 컨텍스트 폭주 방지."""
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= max_chars:
        return obj
    return {
        "_truncated": True,
        "_total_chars": len(s),
        "preview": s[:max_chars] + "…",
    }


# ─────────────────────── 도구 구현 ───────────────────────


def describe_rds_instances(args: dict) -> dict:
    """RDS DB 인스턴스 describe.

    args: {"db_instance_identifier": str?, "max_records": int?}
    """
    rds = _client("rds")
    kwargs: dict[str, Any] = {}
    if args.get("db_instance_identifier"):
        kwargs["DBInstanceIdentifier"] = args["db_instance_identifier"]
    if args.get("max_records"):
        kwargs["MaxRecords"] = max(20, min(100, int(args["max_records"])))
    resp = rds.describe_db_instances(**kwargs)
    items = []
    for db in resp.get("DBInstances", []):
        items.append({
            "id":              db.get("DBInstanceIdentifier"),
            "engine":          db.get("Engine"),
            "engine_version":  db.get("EngineVersion"),
            "instance_class":  db.get("DBInstanceClass"),
            "status":          db.get("DBInstanceStatus"),
            "endpoint":        (db.get("Endpoint") or {}).get("Address"),
            "port":            (db.get("Endpoint") or {}).get("Port"),
            "az":              db.get("AvailabilityZone"),
            "multi_az":        db.get("MultiAZ"),
            "storage_type":    db.get("StorageType"),
            "allocated_gb":    db.get("AllocatedStorage"),
            "pi_enabled":      db.get("PerformanceInsightsEnabled"),
            "publicly_accessible": db.get("PubliclyAccessible"),
        })
    return _truncate({"db_instances": items, "count": len(items)})


def describe_rds_clusters(args: dict) -> dict:
    """Aurora cluster describe."""
    rds = _client("rds")
    kwargs: dict[str, Any] = {}
    if args.get("db_cluster_identifier"):
        kwargs["DBClusterIdentifier"] = args["db_cluster_identifier"]
    resp = rds.describe_db_clusters(**kwargs)
    items = []
    for c in resp.get("DBClusters", []):
        items.append({
            "id":              c.get("DBClusterIdentifier"),
            "engine":          c.get("Engine"),
            "engine_version":  c.get("EngineVersion"),
            "status":          c.get("Status"),
            "endpoint":        c.get("Endpoint"),
            "reader_endpoint": c.get("ReaderEndpoint"),
            "members": [
                {
                    "id":      m.get("DBInstanceIdentifier"),
                    "writer":  m.get("IsClusterWriter"),
                    "promotion_tier": m.get("PromotionTier"),
                }
                for m in (c.get("DBClusterMembers") or [])
            ],
        })
    return _truncate({"db_clusters": items, "count": len(items)})


def describe_db_log_files(args: dict) -> dict:
    """RDS DB instance 의 로그 파일 목록.

    args: {"db_instance_identifier": str, "filename_contains": str?}
    """
    db_id = args["db_instance_identifier"]
    rds = _client("rds")
    kwargs: dict[str, Any] = {"DBInstanceIdentifier": db_id, "MaxRecords": 100}
    if args.get("filename_contains"):
        kwargs["FilenameContains"] = args["filename_contains"]
    resp = rds.describe_db_log_files(**kwargs)
    return _truncate({
        "db_instance_identifier": db_id,
        "log_files": [
            {
                "log_filename":  f.get("LogFileName"),
                "size":          f.get("Size"),
                "last_written":  f.get("LastWritten"),
            }
            for f in resp.get("DescribeDBLogFiles", [])
        ],
    })


def download_db_log_file_portion(args: dict) -> dict:
    """RDS DB log 일부 가져오기 (자동 페이지네이션).

    args: {"db_instance_identifier": str, "log_file_name": str, "marker": str?, "lines": int?, "regex": str?}

    AWS RDS DownloadDBLogFilePortion 은 한 번에 Marker 위치부터 일부만 반환.
    marker 가 없으면 파일 끝까지 페이지 돌며 누적, 그중 마지막 `lines` 줄을 반환.
    regex 가 주어지면 매칭 라인만 필터링.
    """
    import re as _re

    db_id = args["db_instance_identifier"]
    log_name = args["log_file_name"]
    target_lines = min(int(args.get("lines", 200)), 1000)
    regex = args.get("regex")
    pattern = _re.compile(regex) if regex else None
    user_marker = args.get("marker")
    rds = _client("rds")

    collected: list[str] = []
    marker = user_marker or "0"  # "0" = 파일 시작
    final_marker: str | None = None
    pending = False
    pages = 0
    max_pages = 50  # 안전 한도 — 페이지당 ~10K 라인

    while True:
        kwargs: dict[str, Any] = {
            "DBInstanceIdentifier": db_id,
            "LogFileName":          log_name,
            "Marker":                marker,
            "NumberOfLines":         1000,
        }
        resp = rds.download_db_log_file_portion(**kwargs)
        chunk = resp.get("LogFileData") or ""
        if chunk:
            for line in chunk.splitlines():
                if pattern and not pattern.search(line):
                    continue
                collected.append(line)
        final_marker = resp.get("Marker")
        pending = bool(resp.get("AdditionalDataPending"))
        pages += 1
        # marker 가 명시되면 1페이지만 (호환), 아니면 끝까지
        if user_marker:
            break
        if not pending:
            break
        if pages >= max_pages:
            break
        marker = final_marker or marker

    return _truncate({
        "db_instance_identifier":  db_id,
        "log_file_name":           log_name,
        "marker":                  final_marker,
        "additional_data_pending": pending,
        "pages_fetched":           pages,
        "total_matching_lines":    len(collected),
        "lines":                   collected[-target_lines:],
    }, max_chars=14000)


def describe_rds_events(args: dict) -> dict:
    """RDS 이벤트 이력 — failover, 재시작, 파라미터 변경, 스토리지, 백업 등.

    args: {"source_identifier": str?, "source_type": str?, "duration_minutes": int?,
           "categories": [str]?}

    RCA 의 "그 시점에 뭐가 바뀌었나"를 답하는 도구. source 를 안 주면
    계정 전체(리전 내) 이벤트를 시간 역순으로 반환.
    """
    rds = _client("rds")
    kwargs: dict[str, Any] = {
        # AWS 최대 14일(20160분). 기본 24시간.
        "Duration": max(5, min(20160, int(args.get("duration_minutes", 1440)))),
        "MaxRecords": 100,
    }
    if args.get("source_identifier"):
        kwargs["SourceIdentifier"] = args["source_identifier"]
        # SourceIdentifier 를 주면 SourceType 필수 — 기본 db-instance
        kwargs["SourceType"] = args.get("source_type") or "db-instance"
    elif args.get("source_type"):
        kwargs["SourceType"] = args["source_type"]
    if args.get("categories"):
        kwargs["EventCategories"] = list(args["categories"])

    events: list[dict] = []
    while True:
        resp = rds.describe_events(**kwargs)
        for e in resp.get("Events", []):
            events.append({
                "date":       e.get("Date"),
                "source_id":  e.get("SourceIdentifier"),
                "source_type": e.get("SourceType"),
                "categories": e.get("EventCategories"),
                "message":    e.get("Message"),
            })
        marker = resp.get("Marker")
        if not marker or len(events) >= 300:
            break
        kwargs["Marker"] = marker

    events.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return _truncate({
        "events": events,
        "count": len(events),
        "window_minutes": kwargs["Duration"],
    })


def describe_db_recommendations(args: dict) -> dict:
    """RDS 권장사항 — AWS 가 자동 분석한 rightsizing/설정/버전 권고.

    args: {"status": str?, "severity": str?, "max": int?}
    status: active|pending|resolved|dismissed (기본 active)
    """
    rds = _client("rds")
    filters = []
    status = args.get("status") or "active"
    if status != "all":
        filters.append({"Name": "status", "Values": [status]})
    if args.get("severity"):
        filters.append({"Name": "severity", "Values": [args["severity"]]})
    kwargs: dict[str, Any] = {"MaxRecords": max(20, min(100, int(args.get("max", 50))))}
    if filters:
        kwargs["Filters"] = filters
    resp = rds.describe_db_recommendations(**kwargs)
    items = []
    for r in resp.get("DBRecommendations", []):
        items.append({
            "id":          r.get("RecommendationId"),
            "severity":    r.get("Severity"),
            "status":      r.get("Status"),
            "created":     r.get("CreatedTime"),
            "resource_arn": r.get("ResourceArn"),
            "category":    r.get("Category"),
            "detection":   r.get("Detection"),
            "recommendation": r.get("Recommendation"),
            "description": (r.get("Description") or "")[:500],
        })
    return _truncate({"recommendations": items, "count": len(items), "status_filter": status})


def list_msk_clusters(args: dict) -> dict:
    """MSK cluster 목록 (Serverless 포함)."""
    out: list[dict] = []
    # MSK Provisioned
    try:
        kafka = _client("kafka")
        for c in kafka.list_clusters_v2().get("ClusterInfoList", []):
            out.append({
                "name":        c.get("ClusterName"),
                "arn":         c.get("ClusterArn"),
                "type":        c.get("ClusterType"),
                "state":       c.get("State"),
                "creation":    c.get("CreationTime"),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("list_clusters_v2 failed: %s", e)
    return _truncate({"clusters": out, "count": len(out)})


def describe_ec2_instances(args: dict) -> dict:
    """EC2 instance describe.

    args: {"instance_ids": [str]?, "tag_name_contains": str?, "max": int?}
    """
    ec2 = _client("ec2")
    kwargs: dict[str, Any] = {"MaxResults": min(int(args.get("max", 50)), 100)}
    if args.get("instance_ids"):
        kwargs.pop("MaxResults", None)
        kwargs["InstanceIds"] = args["instance_ids"]
    elif args.get("tag_name_contains"):
        kwargs["Filters"] = [{"Name": "tag:Name", "Values": [f"*{args['tag_name_contains']}*"]}]
    resp = ec2.describe_instances(**kwargs)
    items = []
    for r in resp.get("Reservations", []):
        for inst in r.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in (inst.get("Tags") or [])}
            items.append({
                "id":           inst.get("InstanceId"),
                "type":         inst.get("InstanceType"),
                "state":        (inst.get("State") or {}).get("Name"),
                "private_ip":   inst.get("PrivateIpAddress"),
                "az":           (inst.get("Placement") or {}).get("AvailabilityZone"),
                "launch_time":  inst.get("LaunchTime"),
                "name_tag":     tags.get("Name"),
            })
    return _truncate({"instances": items, "count": len(items)})


# NOTE: list_cloudwatch_alarms / list_metric_namespaces 는
# awslabs.cloudwatch-mcp-server (get_active_alarms / get_alarm_history /
# get_metric_metadata) 가 더 풍부하게 제공해서 제거했음. 라우팅: aws_specialist
# → cloudwatch-mcp 로 transfer.


_PI_VALID_GROUPS = {
    "db.sql_tokenized", "db.sql", "db.wait_event", "db.host", "db.user",
    "db.application", "db.session_type", "db.query",
}


def _normalize_pi_group(name: str) -> str:
    """PI GroupBy.Group 정규화 — 흔한 dimension 풀네임을 group prefix 로 잘라낸다."""
    if not name:
        return "db.sql_tokenized"
    if name in _PI_VALID_GROUPS:
        return name
    parts = name.split(".")
    if len(parts) >= 2:
        prefix = ".".join(parts[:2])
        if prefix in _PI_VALID_GROUPS:
            return prefix
    return "db.sql_tokenized"


def describe_pi_dimensions(args: dict) -> dict:
    """RDS Performance Insights 의 dimension 키 탐색.

    args: {"dbi_resource_id": str, "metric": str?, "group_by": str?, "start": str?, "end": str?}

    group_by 는 group prefix (예: "db.sql_tokenized") 또는 dimension full name
    (예: "db.sql_tokenized.statement"). 후자는 prefix 만 잘라 쓴다.
    """
    pi = _client("pi")
    metric = args.get("metric") or "db.load.avg"
    group = _normalize_pi_group(args.get("group_by") or "db.sql_tokenized")
    start = _parse_ts(args.get("start"))
    end = _parse_ts(args.get("end"))
    if not start or not end:
        end = datetime.now(timezone.utc)
        start = end.replace(microsecond=0)
        # last 1h
        from datetime import timedelta
        start = end - timedelta(hours=1)
    resp = pi.describe_dimension_keys(
        ServiceType="RDS",
        Identifier=args["dbi_resource_id"],
        StartTime=start, EndTime=end,
        Metric=metric,
        GroupBy={"Group": group, "Limit": 10},
        PeriodInSeconds=60,
    )
    return _truncate({
        "metric": metric,
        "group":  group,
        "keys": [
            {"dimensions": k.get("Dimensions"), "total": k.get("Total")}
            for k in resp.get("Keys") or []
        ],
        "aligned_start": resp.get("AlignedStartTime"),
        "aligned_end":   resp.get("AlignedEndTime"),
    })


def _parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    if isinstance(s, str):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    return None


@lru_cache(maxsize=64)
def _dbi_resource_id(identifier: str) -> str:
    """DBInstanceIdentifier → DbiResourceId (db-XXXX). 이미 db- prefix 면 그대로."""
    if not identifier or identifier.startswith("db-"):
        return identifier
    try:
        resp = _client("rds").describe_db_instances(DBInstanceIdentifier=identifier)
        instances = resp.get("DBInstances") or []
        if instances and instances[0].get("DbiResourceId"):
            return instances[0]["DbiResourceId"]
    except Exception as e:  # noqa: BLE001
        logger.warning("dbi resolve failed for %s: %s", identifier, e)
    return identifier


def pi_create_analysis_report(args: dict) -> dict:
    """PI 분석 리포트 생성 — AWS 가 해당 구간의 성능을 자동 분석(인사이트 포함).

    args: {"db_id": str, "start": str, "end": str}
    생성엔 수 분 걸림 → 60초까지 폴링, 안 끝나면 report_id 반환(나중에 get 으로 조회).
    """
    import time as _time

    pi = _client("pi")
    rid = _dbi_resource_id(args["db_id"])
    start, end = _parse_ts(args["start"]), _parse_ts(args["end"])
    resp = pi.create_performance_analysis_report(
        ServiceType="RDS", Identifier=rid,
        StartTime=start, EndTime=end,
    )
    report_id = resp["AnalysisReportId"]
    # 짧은 폴링 — 빨리 끝나는 경우 바로 결과까지
    for _ in range(12):
        _time.sleep(5)
        rep = pi.get_performance_analysis_report(
            ServiceType="RDS", Identifier=rid,
            AnalysisReportId=report_id, TextFormat="PLAIN_TEXT",
        ).get("AnalysisReport") or {}
        status = rep.get("Status")
        if status in ("SUCCEEDED", "FAILED"):
            return _truncate(_format_pi_report(rep, rid))
    return {
        "report_id": report_id,
        "dbi_resource_id": rid,
        "status": "RUNNING",
        "hint": "생성 중 — 1~2분 뒤 pi_get_analysis_report 로 report_id 를 조회하라.",
    }


def pi_get_analysis_report(args: dict) -> dict:
    """PI 분석 리포트 조회. report_id 없으면 최근 리포트 목록 반환.

    args: {"db_id": str, "report_id": str?}
    """
    pi = _client("pi")
    rid = _dbi_resource_id(args["db_id"])
    if not args.get("report_id"):
        resp = pi.list_performance_analysis_reports(
            ServiceType="RDS", Identifier=rid, ListTags=False,
        )
        return _truncate({
            "dbi_resource_id": rid,
            "reports": [
                {"report_id": r.get("AnalysisReportId"), "status": r.get("Status"),
                 "start": r.get("StartTime"), "end": r.get("EndTime"),
                 "created": r.get("CreateTime")}
                for r in resp.get("AnalysisReports") or []
            ],
        })
    rep = pi.get_performance_analysis_report(
        ServiceType="RDS", Identifier=rid,
        AnalysisReportId=args["report_id"], TextFormat="PLAIN_TEXT",
    ).get("AnalysisReport") or {}
    return _truncate(_format_pi_report(rep, rid))


def _format_pi_report(rep: dict, rid: str) -> dict:
    """PI AnalysisReport → 요약 구조 (insight 트리를 평탄화)."""
    def _walk(insights: list, depth: int = 0) -> list[dict]:
        out = []
        for ins in insights or []:
            out.append({
                "depth": depth,
                "type": ins.get("InsightType"),
                "description": (ins.get("Description") or "")[:600],
                "severity": ins.get("Severity"),
                "recommendations": [
                    (r.get("RecommendationDescription") or "")[:400]
                    for r in ins.get("Recommendations") or []
                ],
            })
            out.extend(_walk(ins.get("SupportingInsights"), depth + 1))
        return out

    return {
        "report_id": rep.get("AnalysisReportId"),
        "dbi_resource_id": rid,
        "status": rep.get("Status"),
        "start": rep.get("StartTime"),
        "end": rep.get("EndTime"),
        "insights": _walk(rep.get("Insights")),
    }


# ─────────────────────── dispatch ───────────────────────


_TOOLS = {
    "describe_rds_instances":      describe_rds_instances,
    "describe_rds_clusters":       describe_rds_clusters,
    "describe_db_log_files":       describe_db_log_files,
    "download_db_log_file_portion": download_db_log_file_portion,
    "describe_rds_events":         describe_rds_events,
    "describe_db_recommendations": describe_db_recommendations,
    "list_msk_clusters":           list_msk_clusters,
    "describe_ec2_instances":      describe_ec2_instances,
    "describe_pi_dimensions":      describe_pi_dimensions,
    "pi_create_analysis_report":   pi_create_analysis_report,
    "pi_get_analysis_report":      pi_get_analysis_report,
}


def _extract_tool_name(event: dict, body: dict, ctx) -> str | None:
    """Gateway 가 도구 이름을 어디로 넘기는지가 버전에 따라 다르다 — 모든 위치 탐색."""
    if isinstance(event, dict):
        for k in ("tool_name", "bedrockAgentCoreToolName", "__tool__", "toolName"):
            v = event.get(k)
            if v:
                return v
        # AgentCore Gateway 의 lambda invocation context
        rc = event.get("requestContext") or {}
        cust = rc.get("customAuthorizerContext") or rc.get("authorizer") or {}
        if isinstance(cust, dict):
            for k in ("bedrockAgentCoreToolName", "tool_name"):
                v = cust.get(k)
                if v:
                    return v
    for k in ("tool_name", "bedrockAgentCoreToolName", "__tool__"):
        v = body.get(k)
        if v:
            return v
    # client_context (Lambda invoke 의 별도 채널) 에 들어오는 케이스
    try:
        client_ctx = getattr(ctx, "client_context", None)
        if client_ctx and getattr(client_ctx, "custom", None):
            for k in ("bedrockAgentCoreToolName", "tool_name"):
                v = client_ctx.custom.get(k)
                if v:
                    return v
    except Exception:  # noqa: BLE001
        pass
    return None


def handler(event: dict, ctx) -> dict:
    body = _payload(event)
    # Gateway / 직접 invoke / inline payload 호환 — 도구 이름 다층 탐색
    tool_name = _extract_tool_name(event, body, ctx)
    # tool name prefix 가 'aws-api___describe_xxx' 처럼 들어올 수도 있음
    if isinstance(tool_name, str) and "___" in tool_name:
        tool_name = tool_name.rsplit("___", 1)[-1]
    args = body.get("arguments") if "arguments" in body else body

    if not tool_name:
        logger.warning("missing tool_name — event keys: %s, body keys: %s",
                       list(event.keys()) if isinstance(event, dict) else type(event),
                       list(body.keys()) if isinstance(body, dict) else type(body))
        return {"error": "missing tool_name", "available": list(_TOOLS.keys()),
                "debug": {"event_keys": list(event.keys()) if isinstance(event, dict) else None}}
    fn = _TOOLS.get(tool_name)
    if fn is None:
        return {"error": f"unknown tool '{tool_name}'", "available": list(_TOOLS.keys())}

    try:
        result = fn(args or {})
    except Exception as e:  # noqa: BLE001
        logger.exception("aws_api %s failed", tool_name)
        return {"error": f"{type(e).__name__}: {e}", "tool_name": tool_name}

    return _serialize(result)
