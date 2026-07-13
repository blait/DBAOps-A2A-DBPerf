"""
mcp_query_tools.py - stdio MCP server exposing 13 Query Performance tools.

Ported from autonomous-dbops/gateway/lambda_query_tools.py (AgentCore Gateway
Lambda version) to a plain stdio MCP server for EC2 deployment.

Uses pymssql for direct DB queries (Query Store + DMVs).
Credentials come from Secrets Manager at call time; nothing is cached on disk.

Run:  python3.11 mcp_query_tools.py   (spawned by an MCP client over stdio)
"""
import boto3
import pymssql
import json
import os
from typing import Dict, Any

from mcp.server.fastmcp import FastMCP

# Configuration from environment variables
AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-2')
DB_SECRET_ID = os.environ.get('DB_SECRET_ID', 'dbops-sqlserver-secret')
DB_INSTANCE_ID = os.environ.get('DB_INSTANCE_ID', 'sql-server-instance')
DB_NAME = os.environ.get('DB_NAME', 'master')

mcp = FastMCP("dbops-query-tools")


# ===== HELPER FUNCTIONS =====

def get_db_connection():
    """Get database connection using credentials from Secrets Manager"""
    try:
        secrets_client = boto3.client('secretsmanager', region_name=AWS_REGION)
        secret = secrets_client.get_secret_value(SecretId=DB_SECRET_ID)
        creds = json.loads(secret['SecretString'])

        conn = pymssql.connect(
            server=creds['host'],
            user=creds['username'],
            password=creds['password'],
            port=creds.get('port', 1433),
            database=DB_NAME
        )
        return conn
    except Exception as e:
        raise Exception(f"Error connecting to database: {str(e)}")


def run_query(query: str) -> list:
    """Execute a query and return rows as list of dicts."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query)
    columns = [desc[0] for desc in cursor.description]
    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return results


# ===== QUERY STORE TOOLS =====

@mcp.tool()
def check_query_store_enabled() -> Dict[str, Any]:
    """Check if Query Store is enabled and get configuration"""
    try:
        rows = run_query("""
            SELECT
                actual_state_desc,
                readonly_reason,
                desired_state_desc,
                current_storage_size_mb,
                max_storage_size_mb,
                query_capture_mode_desc
            FROM sys.database_query_store_options
        """)
        if rows:
            row = rows[0]
            return {
                'enabled': row['actual_state_desc'] in ('READ_WRITE', 'READ_ONLY'),
                'state': row['actual_state_desc'],
                'readonly_reason': row['readonly_reason'],
                'desired_state': row['desired_state_desc'],
                'storage_used_mb': row['current_storage_size_mb'],
                'storage_max_mb': row['max_storage_size_mb'],
                'capture_mode': row['query_capture_mode_desc'],
                'database': DB_NAME
            }
        return {'enabled': False, 'error': 'Query Store not configured', 'database': DB_NAME}
    except Exception as e:
        return {'enabled': False, 'error': str(e)}


@mcp.tool()
def get_query_store_top_queries(hours_back: int = 24, top_n: int = 10, metric: str = "cpu") -> Dict[str, Any]:
    """Get top resource-consuming queries from Query Store. Metric: cpu, duration, io, memory"""
    try:
        order_by = {
            "cpu": "qrs.avg_cpu_time DESC",
            "duration": "qrs.avg_duration DESC",
            "io": "qrs.avg_logical_io_reads DESC",
            "memory": "qrs.avg_query_max_used_memory DESC"
        }.get(metric, "qrs.avg_cpu_time DESC")

        results = run_query(f"""
        SELECT TOP {int(top_n)}
            qsq.query_id,
            SUBSTRING(CAST(qst.query_sql_text AS NVARCHAR(MAX)), 1, 500) as query_text,
            qrs.avg_cpu_time / 1000 as avg_cpu_ms,
            qrs.avg_duration / 1000 as avg_duration_ms,
            qrs.avg_logical_io_reads,
            qrs.avg_query_max_used_memory * 8 / 1024 as avg_memory_mb,
            qrs.count_executions,
            qrs.last_execution_time
        FROM sys.query_store_query qsq
        JOIN sys.query_store_query_text qst ON qsq.query_text_id = qst.query_text_id
        JOIN sys.query_store_plan qp ON qsq.query_id = qp.query_id
        JOIN sys.query_store_runtime_stats qrs ON qp.plan_id = qrs.plan_id
        JOIN sys.query_store_runtime_stats_interval qrsi ON qrs.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
        WHERE qrsi.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
        ORDER BY {order_by}
        """)
        return {'queries': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_query_store_regressed_queries(hours_back: int = 24) -> Dict[str, Any]:
    """Detect queries that regressed in performance (recent CPU > 1.5x historical)."""
    try:
        results = run_query(f"""
        WITH recent_stats AS (
            SELECT qp.query_id, AVG(qrs.avg_cpu_time) as recent_cpu, AVG(qrs.avg_duration) as recent_duration
            FROM sys.query_store_runtime_stats qrs
            JOIN sys.query_store_runtime_stats_interval qrsi ON qrs.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
            JOIN sys.query_store_plan qp ON qrs.plan_id = qp.plan_id
            WHERE qrsi.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
            GROUP BY qp.query_id
        ),
        historical_stats AS (
            SELECT qp.query_id, AVG(qrs.avg_cpu_time) as hist_cpu, AVG(qrs.avg_duration) as hist_duration
            FROM sys.query_store_runtime_stats qrs
            JOIN sys.query_store_runtime_stats_interval qrsi ON qrs.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
            JOIN sys.query_store_plan qp ON qrs.plan_id = qp.plan_id
            WHERE qrsi.start_time < DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
            GROUP BY qp.query_id
        )
        SELECT TOP 10
            q.query_id,
            SUBSTRING(CAST(qt.query_sql_text AS NVARCHAR(MAX)), 1, 500) as query_text,
            rs.recent_cpu / 1000 as recent_cpu_ms,
            hs.hist_cpu / 1000 as historical_cpu_ms,
            CAST((rs.recent_cpu - hs.hist_cpu) / hs.hist_cpu * 100 AS DECIMAL(10,2)) as cpu_regression_pct,
            rs.recent_duration / 1000 as recent_duration_ms,
            hs.hist_duration / 1000 as historical_duration_ms
        FROM recent_stats rs
        JOIN historical_stats hs ON rs.query_id = hs.query_id
        JOIN sys.query_store_query q ON rs.query_id = q.query_id
        JOIN sys.query_store_query_text qt ON q.query_text_id = qt.query_text_id
        WHERE rs.recent_cpu > hs.hist_cpu * 1.5
        ORDER BY cpu_regression_pct DESC
        """)
        return {'regressed_queries': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_query_store_wait_stats(query_id: int = None, hours_back: int = 24) -> Dict[str, Any]:
    """Get wait statistics from Query Store, optionally filtered to one query_id."""
    try:
        where_clause = f"AND qp.query_id = {int(query_id)}" if query_id else ""
        results = run_query(f"""
        SELECT TOP 20
            qp.query_id,
            qsws.wait_category_desc,
            qsws.avg_query_wait_time_ms,
            qsws.total_query_wait_time_ms,
            qsws.execution_type_desc
        FROM sys.query_store_wait_stats qsws
        JOIN sys.query_store_plan qp ON qsws.plan_id = qp.plan_id
        JOIN sys.query_store_runtime_stats_interval qrsi ON qsws.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
        WHERE qrsi.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
        {where_clause}
        ORDER BY qsws.avg_query_wait_time_ms DESC
        """)
        return {'wait_stats': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_query_execution_history(query_id: int, hours_back: int = 168) -> Dict[str, Any]:
    """Get execution history timeline for a specific query."""
    try:
        results = run_query(f"""
        SELECT
            qrsi.start_time,
            qrsi.end_time,
            qrs.count_executions,
            qrs.avg_cpu_time / 1000 as avg_cpu_ms,
            qrs.avg_duration / 1000 as avg_duration_ms,
            qrs.avg_logical_io_reads,
            qrs.avg_query_max_used_memory * 8 / 1024 as avg_memory_mb
        FROM sys.query_store_runtime_stats qrs
        JOIN sys.query_store_runtime_stats_interval qrsi ON qrs.runtime_stats_interval_id = qrsi.runtime_stats_interval_id
        JOIN sys.query_store_plan qp ON qrs.plan_id = qp.plan_id
        WHERE qp.query_id = {int(query_id)}
        AND qrsi.start_time >= DATEADD(hour, -{int(hours_back)}, GETUTCDATE())
        ORDER BY qrsi.start_time
        """)
        return {'timeline': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_query_store_plan_summary(query_id: int) -> Dict[str, Any]:
    """Get execution plan summary for a specific query."""
    try:
        results = run_query(f"""
        SELECT
            qp.plan_id,
            qp.is_forced_plan,
            qrs.avg_cpu_time / 1000 as avg_cpu_ms,
            qrs.avg_duration / 1000 as avg_duration_ms,
            qrs.count_executions,
            qrs.first_execution_time,
            qrs.last_execution_time
        FROM sys.query_store_plan qp
        JOIN sys.query_store_runtime_stats qrs ON qp.plan_id = qrs.plan_id
        WHERE qp.query_id = {int(query_id)}
        ORDER BY qrs.avg_cpu_time DESC
        """)
        return {'plans': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


# ===== DMV TOOLS =====

@mcp.tool()
def get_slow_queries(threshold_seconds: int = 5) -> Dict[str, Any]:
    """Get currently running slow queries from sys.dm_exec_requests."""
    try:
        results = run_query(f"""
        SELECT TOP 10
            r.session_id,
            r.status,
            r.command,
            r.cpu_time,
            r.total_elapsed_time / 1000 as elapsed_seconds,
            r.logical_reads,
            r.writes,
            r.blocking_session_id,
            SUBSTRING(st.text, (r.statement_start_offset/2)+1,
                ((CASE r.statement_end_offset
                    WHEN -1 THEN DATALENGTH(st.text)
                    ELSE r.statement_end_offset
                END - r.statement_start_offset)/2) + 1) AS query_text
        FROM sys.dm_exec_requests r
        CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
        WHERE r.session_id > 50
        AND r.total_elapsed_time / 1000 > {int(threshold_seconds)}
        ORDER BY r.total_elapsed_time DESC
        """)
        return {'slow_queries': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_blocking_sessions() -> Dict[str, Any]:
    """Get blocking sessions and what they're blocking."""
    try:
        results = run_query("""
        SELECT
            blocking.session_id AS blocking_session_id,
            blocked.session_id AS blocked_session_id,
            blocking_text.text AS blocking_query,
            blocked_text.text AS blocked_query,
            blocked.wait_time / 1000 AS wait_seconds,
            blocked.wait_type
        FROM sys.dm_exec_requests blocked
        INNER JOIN sys.dm_exec_requests blocking
            ON blocked.blocking_session_id = blocking.session_id
        CROSS APPLY sys.dm_exec_sql_text(blocking.sql_handle) blocking_text
        CROSS APPLY sys.dm_exec_sql_text(blocked.sql_handle) blocked_text
        WHERE blocked.blocking_session_id > 0
        """)
        return {'blocking_sessions': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_query_plan_from_cache(query_fragment: str) -> Dict[str, Any]:
    """Get execution plan from plan cache for queries matching a text fragment."""
    try:
        safe_fragment = query_fragment.replace("'", "''")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"""
        SELECT TOP 5
            SUBSTRING(st.text, 1, 500) as query_text,
            qs.execution_count,
            qs.total_worker_time / 1000 as total_cpu_ms,
            qs.total_elapsed_time / 1000 as total_duration_ms,
            qs.total_logical_reads,
            qs.total_logical_writes,
            CAST(qp.query_plan AS NVARCHAR(MAX)) as execution_plan_xml
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp
        WHERE st.text LIKE '%{safe_fragment}%'
        ORDER BY qs.total_worker_time DESC
        """)
        columns = [desc[0] for desc in cursor.description]
        results = []
        for row in cursor.fetchall():
            result = dict(zip(columns, row))
            if result.get('execution_plan_xml'):
                result['execution_plan_xml'] = result['execution_plan_xml'][:1000] + '...(truncated)'
            results.append(result)
        cursor.close()
        conn.close()
        return {'plans': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_expensive_queries_from_cache(top_n: int = 10, metric: str = "cpu") -> Dict[str, Any]:
    """Get top expensive queries from plan cache since last restart. Metric: cpu, duration, reads, writes"""
    try:
        order_by = {
            "cpu": "qs.total_worker_time DESC",
            "duration": "qs.total_elapsed_time DESC",
            "reads": "qs.total_logical_reads DESC",
            "writes": "qs.total_logical_writes DESC"
        }.get(metric, "qs.total_worker_time DESC")

        results = run_query(f"""
        SELECT TOP {int(top_n)}
            SUBSTRING(st.text, 1, 500) as query_text,
            qs.execution_count,
            qs.total_worker_time / 1000 as total_cpu_ms,
            qs.total_elapsed_time / 1000 as total_duration_ms,
            qs.total_logical_reads,
            qs.total_logical_writes,
            qs.creation_time,
            qs.last_execution_time
        FROM sys.dm_exec_query_stats qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
        ORDER BY {order_by}
        """)
        return {'queries': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def suggest_indexes(table_name: str = None) -> Dict[str, Any]:
    """Get missing index recommendations from DMVs with CREATE INDEX statements."""
    try:
        safe_table = table_name.replace("'", "''") if table_name else None
        where_clause = f"AND OBJECT_NAME(d.object_id, d.database_id) = '{safe_table}'" if safe_table else ""
        results = run_query(f"""
        SELECT TOP 10
            OBJECT_NAME(d.object_id, d.database_id) AS table_name,
            d.equality_columns,
            d.inequality_columns,
            d.included_columns,
            s.avg_total_user_cost * s.avg_user_impact * (s.user_seeks + s.user_scans) AS improvement_measure,
            'CREATE INDEX IX_' + OBJECT_NAME(d.object_id, d.database_id) + '_' +
                REPLACE(REPLACE(REPLACE(ISNULL(d.equality_columns, ''), ', ', '_'), '[', ''), ']', '') +
                CASE WHEN d.inequality_columns IS NOT NULL THEN '_' +
                    REPLACE(REPLACE(REPLACE(d.inequality_columns, ', ', '_'), '[', ''), ']', '')
                ELSE '' END +
            ' ON ' + d.statement + ' (' +
                ISNULL(d.equality_columns, '') +
                CASE WHEN d.equality_columns IS NOT NULL AND d.inequality_columns IS NOT NULL THEN ', ' ELSE '' END +
                ISNULL(d.inequality_columns, '') + ')' +
                CASE WHEN d.included_columns IS NOT NULL THEN ' INCLUDE (' + d.included_columns + ')' ELSE '' END
            AS create_index_statement,
            s.user_seeks,
            s.user_scans,
            s.last_user_seek,
            s.last_user_scan
        FROM sys.dm_db_missing_index_details d
        INNER JOIN sys.dm_db_missing_index_groups g ON d.index_handle = g.index_handle
        INNER JOIN sys.dm_db_missing_index_group_stats s ON g.index_group_handle = s.group_handle
        WHERE d.database_id = DB_ID()
        {where_clause}
        ORDER BY improvement_measure DESC
        """)
        return {'missing_indexes': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


@mcp.tool()
def get_index_usage() -> Dict[str, Any]:
    """Get index usage statistics to identify unused or expensive indexes."""
    try:
        results = run_query("""
        SELECT TOP 20
            OBJECT_NAME(s.object_id) AS table_name,
            i.name AS index_name,
            s.user_seeks,
            s.user_scans,
            s.user_lookups,
            s.user_updates,
            CASE
                WHEN s.user_seeks + s.user_scans + s.user_lookups = 0 THEN 'UNUSED'
                WHEN s.user_updates > (s.user_seeks + s.user_scans + s.user_lookups) * 10 THEN 'EXPENSIVE'
                ELSE 'USED'
            END AS usage_status
        FROM sys.dm_db_index_usage_stats s
        INNER JOIN sys.indexes i ON s.object_id = i.object_id AND s.index_id = i.index_id
        WHERE s.database_id = DB_ID()
        AND OBJECTPROPERTY(s.object_id, 'IsUserTable') = 1
        ORDER BY s.user_updates DESC
        """)
        return {'index_usage': results, 'count': len(results)}
    except Exception as e:
        return {'error': str(e)}


# ===== NOTIFICATION TOOL =====

@mcp.tool()
def send_slack_notification(message: str, severity: str = "INFO", channel: str = "") -> Dict[str, Any]:
    """Send a notification to Slack via bot token (chat.postMessage).
    Severity: INFO, WARNING, CRITICAL. channel defaults to SLACK_CHANNEL env."""
    try:
        from connections import send_slack
        return send_slack(message, severity, channel=channel)
    except Exception as e:
        return {'status': 'error', 'error': str(e)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
