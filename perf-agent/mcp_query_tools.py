"""
mcp_query_tools.py - stdio MCP 서버: 쿼리 성능 진단 도구 (멀티엔진).

논리 도구는 하나의 세트, 내부 구현만 DB 엔진별(dialect)로 분기한다:
  - mssql    : Query Store + DMV (sys.query_store_*, sys.dm_exec_*)
  - postgres : pg_stat_statements + pg_stat_activity/pg_locks/EXPLAIN
  - mysql    : performance_schema (events_statements_summary, sys 스키마)

모든 진단 도구는 target 파라미터(기본: 첫 번째 타깃)로 대상 DB를 고른다.
타깃 정의는 db_targets.py(DB_TARGETS env) 참고. list_db_targets로 조회.

엔진별 미지원 기능(예: PG의 시간구간 이력)은 에러 대신
{"unsupported": ..., "hint": ...}로 안내를 반환한다 — LLM이 사용자에게 설명 가능.
"""
import json
import os
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

import db_targets
from db_targets import engine_of, run_query

AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-2')

mcp = FastMCP("dbperf-query-tools")


def _t(target: str) -> str:
    return target or db_targets.default_target()


def _unsupported(feature: str, hint: str) -> Dict[str, Any]:
    return {"unsupported": feature, "hint": hint}


# ═══════════════════════ 타깃 조회 ═══════════════════════

@mcp.tool()
def list_db_targets() -> Dict[str, Any]:
    """List registered database targets (name, engine, host, database).
    Call this first when unsure which 'target' value to pass to other tools."""
    return {"targets": db_targets.describe_targets(),
            "default": db_targets.default_target()}


# ═══════════════════════ 이력/저장소 상태 ═══════════════════════

@mcp.tool()
def check_query_store_enabled(target: str = "") -> Dict[str, Any]:
    """Check if the query history store is enabled — Query Store (mssql) or
    pg_stat_statements extension (postgres) — and return its configuration."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, """
                SELECT actual_state_desc, readonly_reason, desired_state_desc,
                       current_storage_size_mb, max_storage_size_mb, query_capture_mode_desc
                FROM sys.database_query_store_options""")
            if rows:
                r = rows[0]
                return {"engine": eng, "enabled": r["actual_state_desc"] in ("READ_WRITE", "READ_ONLY"),
                        "state": r["actual_state_desc"], "capture_mode": r["query_capture_mode_desc"],
                        "storage_used_mb": r["current_storage_size_mb"],
                        "storage_max_mb": r["max_storage_size_mb"]}
            return {"engine": eng, "enabled": False, "error": "Query Store not configured"}
        if eng == "mysql":
            rows = run_query(target, "SHOW GLOBAL VARIABLES LIKE 'performance_schema'")
            on = rows and rows[0].get("Value") == "ON"
            digest = run_query(target, "SELECT count(*) c FROM performance_schema.events_statements_summary_by_digest") if on else []
            return {"engine": eng, "enabled": bool(on), "source": "performance_schema",
                    "digest_rows": digest[0]["c"] if digest else 0}
        # postgres
        rows = run_query(target, "SELECT extname, extversion FROM pg_extension WHERE extname='pg_stat_statements'")
        if rows:
            cfg = run_query(target, "SHOW pg_stat_statements.max")
            return {"engine": eng, "enabled": True, "extension": "pg_stat_statements",
                    "version": rows[0]["extversion"], "max_statements": cfg[0].get("pg_stat_statements.max") if cfg else None}
        avail = run_query(target, "SELECT name FROM pg_available_extensions WHERE name='pg_stat_statements'")
        return {"engine": eng, "enabled": False,
                "hint": "CREATE EXTENSION pg_stat_statements; (shared_preload_libraries 필요)" if avail
                        else "pg_stat_statements 확장이 설치돼 있지 않음"}
    except Exception as e:
        return {"enabled": False, "error": str(e)[:400]}


# ═══════════════════════ 상위/비싼 쿼리 ═══════════════════════

_MSSQL_TOP_STORE = """
SELECT TOP {n}
    qsq.query_id,
    SUBSTRING(CAST(qst.query_sql_text AS NVARCHAR(MAX)), 1, 500) as query_text,
    qrs.avg_cpu_time / 1000 as avg_cpu_ms,
    qrs.avg_duration / 1000 as avg_duration_ms,
    qrs.avg_logical_io_reads as avg_reads,
    qrs.count_executions as calls,
    qrs.last_execution_time
FROM sys.query_store_query qsq
JOIN sys.query_store_query_text qst ON qsq.query_text_id = qst.query_text_id
JOIN sys.query_store_plan qp ON qsq.query_id = qp.query_id
JOIN sys.query_store_runtime_stats qrs ON qp.plan_id = qrs.plan_id
JOIN sys.query_store_runtime_stats_interval qrsi ON qrs.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
WHERE qrsi.start_time >= DATEADD(hour, -{hours}, GETUTCDATE())
ORDER BY {order}
"""

_MSSQL_TOP_CACHE = """
SELECT TOP {n}
    SUBSTRING(st.text, 1, 500) as query_text,
    qs.execution_count as calls,
    qs.total_worker_time / 1000 as total_cpu_ms,
    qs.total_elapsed_time / 1000 as total_duration_ms,
    qs.total_logical_reads as total_reads,
    qs.last_execution_time
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY {order}
"""

_PG_TOP = """
SELECT queryid::text as query_id,
       LEFT(query, 500) as query_text,
       ROUND((total_exec_time / NULLIF(calls,0))::numeric, 2) as avg_cpu_ms,
       ROUND((total_exec_time / NULLIF(calls,0))::numeric, 2) as avg_duration_ms,
       ROUND((shared_blks_hit + shared_blks_read) / NULLIF(calls,0)::numeric, 1) as avg_reads,
       calls,
       ROUND(total_exec_time::numeric, 1) as total_duration_ms
FROM pg_stat_statements
WHERE query NOT ILIKE '%%pg_stat_statements%%'
ORDER BY {order}
LIMIT {n}
"""


@mcp.tool()
def get_top_queries(target: str = "", hours_back: int = 24, top_n: int = 10,
                    metric: str = "cpu") -> Dict[str, Any]:
    """Get top resource-consuming queries. Metric: cpu, duration, io.
    mssql: Query Store windowed by hours_back (falls back to plan cache if QS off).
    postgres: pg_stat_statements cumulative stats (hours_back not applicable)."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            order = {"cpu": "qrs.avg_cpu_time DESC", "duration": "qrs.avg_duration DESC",
                     "io": "qrs.avg_logical_io_reads DESC"}.get(metric, "qrs.avg_cpu_time DESC")
            rows = run_query(target, _MSSQL_TOP_STORE.format(n=int(top_n), hours=int(hours_back), order=order))
            if rows:
                return {"engine": eng, "source": "query_store", "queries": rows, "count": len(rows)}
            order2 = {"cpu": "qs.total_worker_time DESC", "duration": "qs.total_elapsed_time DESC",
                      "io": "qs.total_logical_reads DESC"}.get(metric, "qs.total_worker_time DESC")
            rows = run_query(target, _MSSQL_TOP_CACHE.format(n=int(top_n), order=order2))
            return {"engine": eng, "source": "plan_cache(since restart)", "queries": rows, "count": len(rows)}
        if eng == "mysql":
            order_my = {"cpu": "SUM_TIMER_WAIT DESC", "duration": "AVG_TIMER_WAIT DESC",
                        "io": "SUM_ROWS_EXAMINED DESC"}.get(metric, "SUM_TIMER_WAIT DESC")
            rows = run_query(target, f"""
                SELECT LEFT(DIGEST_TEXT, 500) AS query_text,
                       COUNT_STAR AS calls,
                       ROUND(SUM_TIMER_WAIT/1e9, 1) AS total_duration_ms,
                       ROUND(AVG_TIMER_WAIT/1e9, 2) AS avg_duration_ms,
                       ROUND(SUM_ROWS_EXAMINED/NULLIF(COUNT_STAR,0), 1) AS avg_rows_examined,
                       LAST_SEEN AS last_execution_time
                FROM performance_schema.events_statements_summary_by_digest
                WHERE SCHEMA_NAME IS NOT NULL AND SCHEMA_NAME NOT IN ('performance_schema','mysql','sys')
                ORDER BY {order_my} LIMIT {int(top_n)}""")
            return {"engine": eng, "source": "performance_schema(cumulative)",
                    "note": "누적 통계 기준 — 시간구간(hours_back) 필터는 MySQL에서 미지원",
                    "queries": rows, "count": len(rows)}
        # postgres
        order = {"cpu": "total_exec_time DESC", "duration": "mean_exec_time DESC",
                 "io": "(shared_blks_hit + shared_blks_read) DESC"}.get(metric, "total_exec_time DESC")
        rows = run_query(target, _PG_TOP.format(n=int(top_n), order=order))
        return {"engine": eng, "source": "pg_stat_statements(cumulative)",
                "note": "누적 통계 기준 — 시간구간(hours_back) 필터는 PG에서 미지원",
                "queries": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


@mcp.tool()
def get_regressed_queries(target: str = "", hours_back: int = 24) -> Dict[str, Any]:
    """Detect queries that regressed (recent CPU > 1.5x historical).
    mssql only (needs Query Store time buckets); postgres returns guidance."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "postgres":
            return _unsupported(
                "regression detection (time-bucketed history)",
                "pg_stat_statements는 누적 통계만 제공. get_top_queries(metric='duration')로 "
                "평균 실행시간이 긴 쿼리를 보거나, pg_stat_statements_reset() 후 재수집으로 비교 가능.")
        if eng == "mysql":
            return _unsupported(
                "regression detection (time-bucketed history)",
                "performance_schema는 누적 통계만 제공. get_top_queries(metric='duration')로 "
                "평균 실행시간 상위를 보거나, TRUNCATE events_statements_summary_by_digest 후 재수집으로 비교 가능.")
        rows = run_query(target, f"""
        WITH recent AS (
            SELECT qp.query_id, AVG(qrs.avg_cpu_time) cpu
            FROM sys.query_store_runtime_stats qrs
            JOIN sys.query_store_runtime_stats_interval i ON qrs.runtime_stats_interval_id=i.runtime_stats_interval_id
            JOIN sys.query_store_plan qp ON qrs.plan_id=qp.plan_id
            WHERE i.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE()) GROUP BY qp.query_id),
        hist AS (
            SELECT qp.query_id, AVG(qrs.avg_cpu_time) cpu
            FROM sys.query_store_runtime_stats qrs
            JOIN sys.query_store_runtime_stats_interval i ON qrs.runtime_stats_interval_id=i.runtime_stats_interval_id
            JOIN sys.query_store_plan qp ON qrs.plan_id=qp.plan_id
            WHERE i.start_time < DATEADD(hour, -{int(hours_back)}, GETUTCDATE()) GROUP BY qp.query_id)
        SELECT TOP 10 q.query_id,
               SUBSTRING(CAST(qt.query_sql_text AS NVARCHAR(MAX)),1,500) query_text,
               r.cpu/1000 recent_cpu_ms, h.cpu/1000 historical_cpu_ms,
               CAST((r.cpu-h.cpu)/h.cpu*100 AS DECIMAL(10,2)) regression_pct
        FROM recent r JOIN hist h ON r.query_id=h.query_id
        JOIN sys.query_store_query q ON r.query_id=q.query_id
        JOIN sys.query_store_query_text qt ON q.query_text_id=qt.query_text_id
        WHERE r.cpu > h.cpu*1.5 ORDER BY regression_pct DESC""")
        return {"engine": eng, "regressed_queries": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


# ═══════════════════════ 실시간 ═══════════════════════

@mcp.tool()
def get_slow_queries(target: str = "", threshold_seconds: int = 5) -> Dict[str, Any]:
    """Get currently running queries slower than threshold_seconds."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, f"""
            SELECT TOP 10 r.session_id, r.status, r.command,
                   r.total_elapsed_time/1000 elapsed_seconds, r.cpu_time, r.logical_reads,
                   r.blocking_session_id,
                   SUBSTRING(st.text,(r.statement_start_offset/2)+1,
                     ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(st.text)
                       ELSE r.statement_end_offset END - r.statement_start_offset)/2)+1) query_text
            FROM sys.dm_exec_requests r
            CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
            WHERE r.session_id > 50 AND r.total_elapsed_time/1000 > {int(threshold_seconds)}
            ORDER BY r.total_elapsed_time DESC""")
        elif eng == "mysql":
            rows = run_query(target, f"""
            SELECT ID AS session_id, STATE AS status, TIME AS elapsed_seconds,
                   USER AS usename, DB AS dbname, LEFT(INFO, 500) AS query_text
            FROM information_schema.PROCESSLIST
            WHERE COMMAND NOT IN ('Sleep','Daemon','Binlog Dump') AND INFO IS NOT NULL
              AND TIME > {int(threshold_seconds)} AND ID <> CONNECTION_ID()
            ORDER BY TIME DESC LIMIT 10""")
        else:
            rows = run_query(target, f"""
            SELECT pid as session_id, state as status,
                   ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1) as elapsed_seconds,
                   wait_event_type, wait_event, usename, application_name,
                   LEFT(query, 500) as query_text
            FROM pg_stat_activity
            WHERE state <> 'idle' AND pid <> pg_backend_pid()
              AND query_start < now() - interval '{int(threshold_seconds)} seconds'
            ORDER BY query_start LIMIT 10""")
        return {"engine": eng, "slow_queries": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


@mcp.tool()
def get_blocking_sessions(target: str = "") -> Dict[str, Any]:
    """Get blocking chains: who blocks whom, with both queries and wait time."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, """
            SELECT blocking.session_id blocking_session_id, blocked.session_id blocked_session_id,
                   bt.text blocking_query, kt.text blocked_query,
                   blocked.wait_time/1000 wait_seconds, blocked.wait_type
            FROM sys.dm_exec_requests blocked
            JOIN sys.dm_exec_requests blocking ON blocked.blocking_session_id = blocking.session_id
            CROSS APPLY sys.dm_exec_sql_text(blocking.sql_handle) bt
            CROSS APPLY sys.dm_exec_sql_text(blocked.sql_handle) kt
            WHERE blocked.blocking_session_id > 0""")
        elif eng == "mysql":
            rows = run_query(target, """
            SELECT r.trx_mysql_thread_id AS blocked_session_id,
                   b.trx_mysql_thread_id AS blocking_session_id,
                   LEFT(b.trx_query, 300) AS blocking_query,
                   LEFT(r.trx_query, 300) AS blocked_query,
                   TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds,
                   'innodb_lock' AS wait_type
            FROM performance_schema.data_lock_waits w
            JOIN information_schema.innodb_trx r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
            JOIN information_schema.innodb_trx b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID""")
        else:
            rows = run_query(target, """
            SELECT blocked.pid as blocked_session_id,
                   blocker.pid as blocking_session_id,
                   LEFT(blocker.query, 300) as blocking_query,
                   LEFT(blocked.query, 300) as blocked_query,
                   ROUND(EXTRACT(EPOCH FROM (now() - blocked.query_start))::numeric,1) as wait_seconds,
                   blocked.wait_event_type || ':' || blocked.wait_event as wait_type
            FROM pg_stat_activity blocked
            JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS bp(pid) ON true
            JOIN pg_stat_activity blocker ON blocker.pid = bp.pid
            WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0""")
        return {"engine": eng, "blocking_sessions": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


@mcp.tool()
def get_query_plan(target: str = "", query_fragment: str = "") -> Dict[str, Any]:
    """Get execution plan for queries matching a text fragment.
    mssql: plan cache XML. postgres: EXPLAIN of the matching statement text."""
    target = _t(target)
    if not query_fragment:
        return {"error": "query_fragment is required"}
    safe = query_fragment.replace("'", "''")
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, f"""
            SELECT TOP 3 SUBSTRING(st.text,1,300) query_text, qs.execution_count calls,
                   qs.total_worker_time/1000 total_cpu_ms,
                   CAST(qp.query_plan AS NVARCHAR(MAX)) plan_xml
            FROM sys.dm_exec_query_stats qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
            CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp
            WHERE st.text LIKE '%{safe}%' ORDER BY qs.total_worker_time DESC""")
            for r in rows:
                if r.get("plan_xml"):
                    r["plan_xml"] = r["plan_xml"][:1500] + "...(truncated)"
            return {"engine": eng, "plans": rows, "count": len(rows)}
        if eng == "mysql":
            stmts = run_query(target, f"""
                SELECT DIGEST_TEXT FROM performance_schema.events_statements_summary_by_digest
                WHERE DIGEST_TEXT LIKE '%{safe}%' AND SCHEMA_NAME NOT IN ('performance_schema','mysql','sys')
                ORDER BY SUM_TIMER_WAIT DESC LIMIT 1""")
            if not stmts:
                return {"engine": eng, "plans": [], "count": 0,
                        "note": f"'{query_fragment}' 매칭 쿼리가 performance_schema에 없음"}
            q = stmts[0]["DIGEST_TEXT"]
            if "?" in q:
                return {"engine": eng, "matched_query": q[:500], "plans": [],
                        "note": "다이제스트(? 파라미터) 쿼리는 자동 EXPLAIN 불가 — 원문으로 수동 EXPLAIN 권장"}
            plan = run_query(target, f"EXPLAIN FORMAT=JSON {q}")
            key = list(plan[0].keys())[0] if plan else None
            return {"engine": eng, "matched_query": q[:500],
                    "plan": str(plan[0][key])[:2000] if plan else None}
        # postgres: pg_stat_statements에서 문장 찾아 EXPLAIN (파라미터 쿼리는 EXPLAIN 불가할 수 있음)
        stmts = run_query(target, f"""
            SELECT query FROM pg_stat_statements
            WHERE query ILIKE '%{safe}%' AND query NOT ILIKE '%pg_stat_statements%'
              AND query NOT ILIKE 'EXPLAIN%' AND lower(query) LIKE 'select%'
            ORDER BY total_exec_time DESC LIMIT 1""")
        if not stmts:
            return {"engine": eng, "plans": [], "count": 0,
                    "note": f"'{query_fragment}' 매칭 SELECT 쿼리가 pg_stat_statements에 없음"}
        q = stmts[0]["query"]
        if "$1" in q:
            return {"engine": eng, "matched_query": q[:500], "plans": [],
                    "note": "파라미터화된 쿼리($1..)는 자동 EXPLAIN 불가 — 쿼리 원문으로 수동 EXPLAIN 권장"}
        plan = run_query(target, f"EXPLAIN (FORMAT JSON) {q}")
        key = list(plan[0].keys())[0] if plan else None
        return {"engine": eng, "matched_query": q[:500],
                "plan": json.dumps(plan[0][key])[:2000] if plan else None}
    except Exception as e:
        return {"error": str(e)[:400]}


# ═══════════════════════ 대기 통계 ═══════════════════════

@mcp.tool()
def get_wait_stats(target: str = "", hours_back: int = 24) -> Dict[str, Any]:
    """Get wait statistics. mssql: per-query waits from Query Store (windowed).
    postgres: current wait_event snapshot from pg_stat_activity."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, f"""
            SELECT TOP 20 qp.query_id, w.wait_category_desc,
                   w.avg_query_wait_time_ms, w.total_query_wait_time_ms
            FROM sys.query_store_wait_stats w
            JOIN sys.query_store_plan qp ON w.plan_id = qp.plan_id
            JOIN sys.query_store_runtime_stats_interval i ON w.runtime_stats_interval_id=i.runtime_stats_interval_id
            WHERE i.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
            ORDER BY w.avg_query_wait_time_ms DESC""")
            return {"engine": eng, "source": "query_store(windowed)", "wait_stats": rows, "count": len(rows)}
        if eng == "mysql":
            rows = run_query(target, """
                SELECT EVENT_NAME AS wait_event, COUNT_STAR AS waits,
                       ROUND(SUM_TIMER_WAIT/1e9, 1) AS total_wait_ms
                FROM performance_schema.events_waits_summary_global_by_event_name
                WHERE COUNT_STAR > 0 AND EVENT_NAME NOT LIKE 'idle%%'
                ORDER BY SUM_TIMER_WAIT DESC LIMIT 20""")
            return {"engine": eng, "source": "performance_schema(cumulative)",
                    "wait_stats": rows, "count": len(rows)}
        rows = run_query(target, """
            SELECT wait_event_type, wait_event, count(*) as sessions,
                   array_agg(DISTINCT state) as states
            FROM pg_stat_activity
            WHERE wait_event IS NOT NULL AND pid <> pg_backend_pid()
            GROUP BY 1,2 ORDER BY sessions DESC LIMIT 20""")
        return {"engine": eng, "source": "pg_stat_activity(snapshot)",
                "note": "PG는 누적 대기통계가 기본 미제공 — 현재 스냅샷 기준",
                "wait_stats": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


# ═══════════════════════ 인덱스 ═══════════════════════

@mcp.tool()
def suggest_indexes(target: str = "", table_name: str = "") -> Dict[str, Any]:
    """Index recommendations. mssql: missing-index DMVs with ready CREATE INDEX DDL.
    postgres: heuristic — tables with heavy sequential scans (index candidates)."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            safe = table_name.replace("'", "''") if table_name else ""
            where = f"AND OBJECT_NAME(d.object_id, d.database_id) = '{safe}'" if safe else ""
            rows = run_query(target, f"""
            SELECT TOP 10 OBJECT_NAME(d.object_id, d.database_id) table_name,
                   d.equality_columns, d.inequality_columns, d.included_columns,
                   s.avg_total_user_cost * s.avg_user_impact * (s.user_seeks + s.user_scans) improvement,
                   'CREATE INDEX IX_' + OBJECT_NAME(d.object_id, d.database_id) + '_auto ON ' + d.statement +
                   ' (' + ISNULL(d.equality_columns,'') +
                   CASE WHEN d.equality_columns IS NOT NULL AND d.inequality_columns IS NOT NULL THEN ', ' ELSE '' END +
                   ISNULL(d.inequality_columns,'') + ')' +
                   CASE WHEN d.included_columns IS NOT NULL THEN ' INCLUDE (' + d.included_columns + ')' ELSE '' END ddl
            FROM sys.dm_db_missing_index_details d
            JOIN sys.dm_db_missing_index_groups g ON d.index_handle = g.index_handle
            JOIN sys.dm_db_missing_index_group_stats s ON g.index_group_handle = s.group_handle
            WHERE d.database_id = DB_ID() {where} ORDER BY improvement DESC""")
            return {"engine": eng, "missing_indexes": rows, "count": len(rows)}
        if eng == "mysql":
            rows = run_query(target, """
                SELECT object_schema AS db, object_name AS table_name,
                       rows_full_scanned, latency
                FROM sys.schema_tables_with_full_table_scans
                ORDER BY rows_full_scanned DESC LIMIT 10""")
            return {"engine": eng, "source": "sys.schema_tables_with_full_table_scans",
                    "note": "풀스캔 많은 테이블 — WHERE 절 컬럼 분석 후 인덱스 후보. EXPLAIN으로 검증 권장",
                    "index_candidates": rows, "count": len(rows)}
        safe = table_name.replace("'", "''") if table_name else ""
        where = f"AND relname = '{safe}'" if safe else ""
        rows = run_query(target, f"""
            SELECT relname as table_name, seq_scan, seq_tup_read, idx_scan,
                   n_live_tup as approx_rows,
                   CASE WHEN seq_scan > 0 THEN ROUND(seq_tup_read::numeric / seq_scan, 0) ELSE 0 END as avg_rows_per_seqscan
            FROM pg_stat_user_tables
            WHERE seq_scan > COALESCE(idx_scan, 0) AND n_live_tup > 1000 {where}
            ORDER BY seq_tup_read DESC LIMIT 10""")
        return {"engine": eng, "source": "seq_scan heuristic",
                "note": "풀스캔이 인덱스스캔보다 많은 테이블 — WHERE 절 컬럼 분석 후 인덱스 후보. "
                        "정확한 검증은 EXPLAIN + (가능하면 HypoPG)",
                "index_candidates": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


@mcp.tool()
def get_index_usage(target: str = "") -> Dict[str, Any]:
    """Find unused or rarely-used indexes (drop candidates) and usage stats."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "mssql":
            rows = run_query(target, """
            SELECT TOP 20 OBJECT_NAME(s.object_id) table_name, i.name index_name,
                   s.user_seeks, s.user_scans, s.user_lookups, s.user_updates,
                   CASE WHEN s.user_seeks + s.user_scans + s.user_lookups = 0 THEN 'UNUSED'
                        WHEN s.user_updates > (s.user_seeks+s.user_scans+s.user_lookups)*10 THEN 'EXPENSIVE'
                        ELSE 'USED' END usage_status
            FROM sys.dm_db_index_usage_stats s
            JOIN sys.indexes i ON s.object_id = i.object_id AND s.index_id = i.index_id
            WHERE s.database_id = DB_ID() AND OBJECTPROPERTY(s.object_id,'IsUserTable')=1
            ORDER BY s.user_updates DESC""")
        elif eng == "mysql":
            rows = run_query(target, """
            SELECT object_schema AS db, object_name AS table_name, index_name
            FROM sys.schema_unused_indexes LIMIT 20""")
            return {"engine": eng, "source": "sys.schema_unused_indexes",
                    "unused_indexes": rows, "count": len(rows)}
        else:
            rows = run_query(target, """
            SELECT s.relname as table_name, s.indexrelname as index_name,
                   s.idx_scan as scans,
                   pg_size_pretty(pg_relation_size(s.indexrelid)) as index_size,
                   CASE WHEN s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary THEN 'UNUSED'
                        WHEN s.idx_scan < 50 THEN 'RARELY_USED' ELSE 'USED' END as usage_status
            FROM pg_stat_user_indexes s
            JOIN pg_index i ON s.indexrelid = i.indexrelid
            ORDER BY s.idx_scan ASC, pg_relation_size(s.indexrelid) DESC LIMIT 20""")
        return {"engine": eng, "index_usage": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


# ═══════════════════════ PG 고유 건강 지표 (mssql은 안내) ═══════════════════════

@mcp.tool()
def get_table_health(target: str = "") -> Dict[str, Any]:
    """Table health: postgres — dead tuples/bloat & last (auto)vacuum·analyze.
    mssql — index fragmentation summary."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "postgres":
            rows = run_query(target, """
            SELECT relname as table_name, n_live_tup, n_dead_tup,
                   CASE WHEN n_live_tup > 0 THEN ROUND(n_dead_tup*100.0/n_live_tup, 1) ELSE 0 END as dead_pct,
                   last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
            FROM pg_stat_user_tables
            WHERE n_live_tup + n_dead_tup > 0
            ORDER BY n_dead_tup DESC LIMIT 15""")
            return {"engine": eng, "table_health": rows, "count": len(rows),
                    "note": "dead_pct 높으면 VACUUM 필요 — autovacuum 동작 여부 확인"}
        if eng == "mysql":
            rows = run_query(target, """
                SELECT TABLE_SCHEMA AS db, TABLE_NAME AS table_name, TABLE_ROWS AS approx_rows,
                       ROUND(DATA_LENGTH/1024/1024, 1) AS data_mb,
                       ROUND(DATA_FREE/1024/1024, 1) AS free_mb,
                       CASE WHEN DATA_LENGTH > 0 THEN ROUND(DATA_FREE*100.0/(DATA_LENGTH+DATA_FREE), 1) ELSE 0 END AS frag_pct
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')
                ORDER BY DATA_FREE DESC LIMIT 15""")
            return {"engine": eng, "table_health": rows, "count": len(rows),
                    "note": "frag_pct 높으면 OPTIMIZE TABLE 검토"}
        rows = run_query(target, """
        SELECT TOP 15 OBJECT_NAME(ips.object_id) table_name, i.name index_name,
               CAST(ips.avg_fragmentation_in_percent AS DECIMAL(5,1)) frag_pct, ips.page_count
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
        JOIN sys.indexes i ON ips.object_id = i.object_id AND ips.index_id = i.index_id
        WHERE ips.page_count > 100 AND ips.avg_fragmentation_in_percent > 10
        ORDER BY ips.avg_fragmentation_in_percent DESC""")
        return {"engine": eng, "index_fragmentation": rows, "count": len(rows)}
    except Exception as e:
        return {"error": str(e)[:400]}


@mcp.tool()
def get_connection_stats(target: str = "") -> Dict[str, Any]:
    """Connection/session breakdown: total, active, idle, idle-in-transaction."""
    target = _t(target)
    try:
        eng = engine_of(target)
        if eng == "postgres":
            rows = run_query(target, """
            SELECT COALESCE(state,'(none)') as state, count(*) as sessions,
                   ROUND(EXTRACT(EPOCH FROM max(now()-state_change))::numeric,0) as max_state_seconds
            FROM pg_stat_activity WHERE pid <> pg_backend_pid()
            GROUP BY state ORDER BY sessions DESC""")
            mx = run_query(target, "SHOW max_connections")
            return {"engine": eng, "by_state": rows,
                    "max_connections": mx[0].get("max_connections") if mx else None,
                    "note": "'idle in transaction'이 오래 남으면 락/베큠 지연 원인"}
        if eng == "mysql":
            rows = run_query(target, """
                SELECT COMMAND AS state, COUNT(*) AS sessions, MAX(TIME) AS max_state_seconds
                FROM information_schema.PROCESSLIST GROUP BY COMMAND ORDER BY sessions DESC""")
            mx = run_query(target, "SHOW GLOBAL VARIABLES LIKE 'max_connections'")
            return {"engine": eng, "by_state": rows,
                    "max_connections": mx[0].get("Value") if mx else None}
        rows = run_query(target, """
        SELECT s.status, count(*) sessions
        FROM sys.dm_exec_sessions s WHERE s.is_user_process = 1
        GROUP BY s.status""")
        return {"engine": eng, "by_state": rows}
    except Exception as e:
        return {"error": str(e)[:400]}


# ═══════════════════════ 알림 ═══════════════════════

@mcp.tool()
def send_slack_notification(message: str, severity: str = "INFO", channel: str = "") -> Dict[str, Any]:
    """Send a notification to Slack via bot token (chat.postMessage).
    Severity: INFO, WARNING, CRITICAL. channel defaults to SLACK_CHANNEL env."""
    try:
        from connections import send_slack
        return send_slack(message, severity, channel=channel)
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
