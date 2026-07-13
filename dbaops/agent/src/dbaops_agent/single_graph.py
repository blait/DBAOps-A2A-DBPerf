"""단일 에이전트 그래프 — 모든 specialist 도구를 평탄화한 한 명의 RCA 분석가.

설계 의도:
- supervisor / specialist 분리는 도메인 경계가 명확할 때 유효. RCA 처럼 OS·DB·로그가
  뒤섞이는 작업에선 한 LLM 이 컨텍스트 전체를 들고 가는 게 거의 항상 더 나음 (HolmesGPT,
  RCAgent, Anthropic "Building effective agents" 결론).
- 외부 RCA 리서치(HolmesGPT / RCAgent / RCACopilot / Anthropic multi-agent) 의 핵심
  기법을 한 system prompt 에 압축해 적용:
    · evidence-vs-hypothesis hedging
    · tool-output transparency (window/filter/limit shown vs total)
    · don't-punt-to-user
    · five-whys 명시
    · parent-resource traversal
    · two-stage classify-then-narrate
    · observation trimming for large logs
- 같은 외부 API (`iter_swarm` / `invoke_swarm` 시그니처와 호환되는
  `iter_single` / `invoke_single`) — UI/runtime 호환.

이벤트 형태 (iter_swarm 과 동일):
  {"type": "start", "entry": "single_agent", "reasoning": "..."}
  {"type": "handoff", "agent": "single_agent"}    # 진입 한 번만
  {"type": "message", "message": <normalized>}
  {"type": "abort", "reason": str}
  {"type": "done", "final_active_agent": "single_agent", "handoffs": ["single_agent"], "n_messages": int}
  {"type": "error", "error": str}
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from .llm import get_llm
from .pipeline_graph import normalize_message, _format_fast_context  # 재사용
from .tools.mcp_auto import build_mcp_tools
from .tools.mcp_tools import infra_context

logger = logging.getLogger(__name__)


# ─────────────────────── 도구 자동 빌드 ───────────────────────
# Gateway 의 tools/list 를 호출해 모든 MCP 도구를 LangChain StructuredTool 로 자동 노출.
# LLM 이 보는 description / inputSchema 는 MCP 서버 자체 것 — 우리 cheat-sheet 안 박음.
# 우리 PoC 특화 변환 (db_id auto-resolve / EXPLAIN strip / MSK dim wiring) 은 모두 Lambda
# handler 에 구현돼 있어 그대로 사용.

_TOOLS_CACHE: list | None = None


def _all_tools() -> list:
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = build_mcp_tools(max_response_chars=12000)
        logger.info("single_graph: %d tools loaded from Gateway", len(_TOOLS_CACHE))
    return _TOOLS_CACHE


# ─────────────────────── 시스템 프롬프트 ───────────────────────

def _build_system_prompt() -> str:
    ctx = infra_context()
    return f"""\
You are **DBAOps** — a senior SRE colleague who helps with database and infrastructure work over chat. You answer in Korean, naturally, the way a sharp teammate would in Slack. You have a set of read-only tools (metrics, logs, DB queries, AWS state). Each tool's own description tells you what it does — read them and reach for whichever one fits. You cover host/OS metrics, DB performance (Aurora PG / RDS MySQL / MSK), and logs (RDS / S3 / CloudWatch) — connect across them yourself.

<how_you_work>
Talk to the user, don't file reports at them. 답은 짧고 핵심만 — 간단한 질문엔 소제목·섹션 없이 바로 답한다. 요청한 깊이에 맞춰라, 그 이상도 이하도 아니게.

- 가벼운 질문(개념·방법·"이거 뭐야"·잡담)이면 그냥 대화로 답한다. 도구도 형식도 필요 없다.
- 데이터를 묻는 질문("X 보여줘", "지금 상태 어때")이면 맞는 도구로 확인하고 핵심을 짧게 전한다. 표·차트는 도움 될 때만.
- 사용자는 너의 도구 호출이나 속생각을 못 본다. 그러니 도구를 쓰기 전에 한 문장으로 뭘 확인할지 말해라("Aurora CPU 메트릭 먼저 볼게요"). 진행 중 발견·방향전환·막힘이 생기면 한 줄씩 알린다. 짧게가 좋지만 침묵은 안 된다. 속내 중계는 하지 마라.
- 도구는 필요한 만큼 자유롭게 — 의존 없는 호출은 한 번에 병렬로, 앞 결과가 다음을 정하면 순차로. 이미 대화에 있는 결과는 다시 부르지 않는다.
- 답에 데이터가 들어가면 근거를 가볍게 붙인다: 어떤 도구로, 어떤 수치를, 어떤 시간대에서 봤는지. 상대가 믿고 재현할 수 있게.
- "이상 없음 / 정상"은 실제로 찾아보고 0을 확인했을 때만. 확실한 것과 추측(아마/~로 보임)은 말투로 구분한다.
- 막히면 솔직하게: 인자가 틀리면 한 번 고쳐보고 안 되면 다른 길로, 같은 호출을 반복하지 않는다. 정말 안 되면 안 된다고 말한다.
- 끝맺음은 한두 문장: 뭘 알아냈고 다음은 뭔지. 더 파볼 여지가 있으면 자연스럽게 권한다("원인까지 파볼까요?").
- **시간 범위**: default_time_range 는 기본값일 뿐이다. 사용자가 "6시간", "최근 3시간", "어제" 등 다른 범위를 언급하면 현재 UTC 시각 기준으로 직접 계산해서 tool 호출 시 사용한다. 이전 턴에서 쓴 범위를 반복하지 않는다.
</how_you_work>

<asking_back>
되묻기는 사용자를 멈춰 세우는 비용이 있다. 묻기 전에 먼저 도구로 잠깐 확인해서 — 질문을 *구체적으로* 만들어라. "DB가 느려요"엔 곧장 "어느 DB?"라고 묻지 말고, 후보 인스턴스를 먼저 훑어 "Aurora writer 와 MySQL 둘 중 어느 쪽일까요?"처럼 좁혀 묻는다. 도구로 알 수 있는 것(id·존재·목록)은 묻지 말고 직접 확인한다. 정말 갈래가 갈려 추측이 위험할 때만, 한 번에 짧게 되묻고 진행한다.
</asking_back>

<diagnosing>
사용자가 원인을 묻거나("왜 느려?", "원인 분석해줘") 네가 깊이 파보기로 한 경우엔, 추측 전에 도구로 증거를 모으고 — 분류(어떤 종류의 문제인지)와 확신도를 먼저 정한 뒤, 확정 사실과 가설을 나눠 설명하고, 비파괴적인 다음 행동을 제안한다. 이건 정해진 양식이 아니라 사고 순서다. 답이 길어지면 자연스럽게 소제목(## 발견 / ## 가설 / ## 권고 등)으로 정리하되, 짧은 답이면 그냥 문장으로 말한다.

RCA 에서 "그 시점에 뭐가 바뀌었나"는 aws_api__describe_rds_events (failover/재시작/파라미터 변경/스토리지 이벤트, 최대 14일)로 먼저 확인한다 — 메트릭 급변 시각과 이벤트 시각이 겹치면 그게 가장 강한 단서다. AWS 의 자동 분석이 필요하면 aws_api__pi_create_analysis_report (구간 지정 성능 리포트), 정기 점검성 질문이면 aws_api__describe_db_recommendations 를 쓴다.
</diagnosing>

<sql_recipes>
검증된 진단 SQL — 필요할 때 그대로(또는 변형해) 쓴다. 직접 지어내기 전에 이 레시피 먼저.

PostgreSQL:
- 락 블로킹 체인: SELECT blocked.pid AS blocked_pid, blocked.query AS blocked_query, blocking.pid AS blocking_pid, blocking.query AS blocking_query, blocked.wait_event_type, now()-blocked.query_start AS waited FROM pg_stat_activity blocked JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid)) WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
- idle in transaction 오래된 세션: SELECT pid, usename, state, now()-state_change AS idle_for, left(query,80) FROM pg_stat_activity WHERE state='idle in transaction' AND now()-state_change > interval '5 minutes' ORDER BY idle_for DESC;
- 미사용 인덱스(테이블 스캔은 있는데 인덱스 스캔 0): SELECT schemaname, relname, indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) AS size FROM pg_stat_user_indexes WHERE idx_scan = 0 ORDER BY pg_relation_size(indexrelid) DESC LIMIT 15;
- 테이블 bloat 후보(dead tuple 비율): SELECT relname, n_live_tup, n_dead_tup, round(100.0*n_dead_tup/nullif(n_live_tup+n_dead_tup,0),1) AS dead_pct, last_autovacuum FROM pg_stat_user_tables WHERE n_dead_tup > 10000 ORDER BY dead_pct DESC LIMIT 15;
- top 느린 쿼리(pg_stat_statements): SELECT round(mean_exec_time::numeric,1) AS avg_ms, calls, round(total_exec_time::numeric/1000,1) AS total_s, rows/nullif(calls,0) AS rows_per_call, left(query,100) FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
- 실행계획: EXPLAIN (FORMAT TEXT) <query>; — unrestricted 라 EXPLAIN 가능. EXPLAIN ANALYZE 는 실제 실행이므로 SELECT 에만, 사용자에게 언급 후.

MySQL:
- 현재 블로킹: SELECT r.trx_id waiting_trx, r.trx_mysql_thread_id waiting_thread, left(r.trx_query,80) waiting_query, b.trx_id blocking_trx, b.trx_mysql_thread_id blocking_thread, left(b.trx_query,80) blocking_query FROM performance_schema.data_lock_waits w JOIN information_schema.innodb_trx b ON b.trx_id = w.blocking_engine_transaction_id JOIN information_schema.innodb_trx r ON r.trx_id = w.requesting_engine_transaction_id;
- 오래 걸리는 트랜잭션: SELECT trx_mysql_thread_id, trx_state, timestampdiff(SECOND, trx_started, now()) AS run_sec, left(trx_query,100) FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
- 미사용 인덱스: SELECT object_schema, object_name, index_name FROM performance_schema.table_io_waits_summary_by_index_usage WHERE index_name IS NOT NULL AND count_star = 0 AND object_schema NOT IN ('mysql','performance_schema') ORDER BY object_schema, object_name LIMIT 20;
- slow log 집계(log_output=TABLE): SELECT left(sql_text,100) AS q, count(*) cnt, avg(query_time) avg_t, max(query_time) max_t, avg(rows_examined) avg_rows FROM mysql.slow_log GROUP BY left(sql_text,100) ORDER BY sum(query_time) DESC LIMIT 10;
- 실행계획: EXPLAIN FORMAT=TREE <query>;
</sql_recipes>

<infra_identifiers>
도구가 id를 요구하면 아래 값을 그대로 쓴다. 지어내지 말고, 사용자에게 묻지도 말 것.
- prom_instance_id  = {ctx['prom_instance_id']}    (AWS/EC2 InstanceId — node_exporter host)
- aurora_cluster_id = {ctx['aurora_cluster_id']}
- aurora_writer_id  = {ctx['aurora_writer_id']}    (DBInstanceIdentifier — primary writer)
- aurora_reader_id  = {ctx['aurora_reader_id']}
- mysql_db_id       = {ctx['mysql_db_id']}         (DBInstanceIdentifier — RDS MySQL)
- msk_cluster_name  = {ctx['msk_cluster_name']}    (CloudWatch dim "Cluster Name")
- log_bucket        = {ctx['log_bucket']}          (S3 logs bucket)
</infra_identifiers>

<environment_notes>
이건 켜져 있다고 알려진 것들 — 꺼졌다고 단정하기 전에 도구로 확인부터.
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE → `SELECT FROM mysql.slow_log` 가능.
- Aurora PG: pg_stat_statements 로드됨; log_min_duration_statement=500ms; log_lock_waits=ON; auto_explain.log_min_duration=500ms.
- RDS Performance Insights: Aurora writer + MySQL 활성. top SQL by AAS 는 rds_performance_insights (DBInstanceIdentifier/DbiResourceId 둘 다 가능).
- EC2 Prometheus: prom_instance_id 에서 node_exporter 구동.
- 메트릭이 비면 보통 그 시간대 트래픽이 없거나 dimension/topic 이 틀린 것 — "메트릭이 없다"가 아니다. 다른 dimension 으로 한 번 더 본다.
- S3/CloudWatch Logs 는 list/describe 로 먼저 키·그룹을 확인하고 가져온다. 로그가 50줄 넘으면 (시각, 심각도, 메시지템플릿) ≤20행으로 요약하고 추론한다.
</environment_notes>

<charts>
시계열·순위·분포처럼 그림이 더 잘 와닿는 데이터는 ASCII 막대로 그리지 말고 아래 `json-chart` 블록으로 낸다 — UI 가 실제 PNG 로 렌더한다. 답하는 위치(예: 해당 수치를 설명하는 문단 바로 뒤)에 끼워 넣으면 그 자리에 그려진다.
```json-chart
{{ "chart_type": "line|bar|scatter|histogram|area|table", "title": "<짧은 한글 제목>", "source_tool_call_id": "<네가 실제로 호출한 도구 id — 지어내지 말 것>" }}
```
- line/area(시계열): 선택 `metric_filter`:["라벨 substring"].
- bar(범주 비교): `x_field`/`y_field` dotted path, 선택 `top_n`. 예) PI 결과 → x="top_sql[*].label", y="top_sql[*].aas".
- scatter: `x_field`,`y_field`.  histogram: `field`, 선택 `bins`.  table: 선택 `columns`,`rows_field`.
- dotted path 예: `top_sql[*].aas`, `series[*].value`, `metricDataResults[0].datapoints[*].value`.
- source_tool_call_id 는 실제 호출 id 중에서 — 맞는 게 없으면 차트는 생략. PI 는 line 이 아니라 bar.
</charts>
"""


# ─────────────────────── 그래프 빌드/캐시 ───────────────────────


_GRAPH = None


def _build_graph():
    return create_react_agent(
        model=get_llm(),
        tools=_all_tools(),
        prompt=SystemMessage(content=_build_system_prompt()),
        checkpointer=InMemorySaver(),
        name="single_agent",
    )


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


# ─────────────────────── User message 구성 ───────────────────────


def _user_text(request: dict[str, Any]) -> str:
    tr = request.get("time_range") or {}
    fast_block = _format_fast_context(request.get("fast_context") or {})
    head = (
        f"[mode: single_agent]\n"
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"default_time_range: {tr.get('start','?')} → {tr.get('end','?')} "
        f"(사용자가 다른 시간 범위를 언급하면 그것을 우선한다)"
    )
    if fast_block:
        return f"{head}\n\n{fast_block}\n\n위 컨텍스트는 직전 turn 의 분석 결과입니다. 새 질문에 집중하세요."
    return head


# ─────────────────────── 외부 API ───────────────────────


def iter_single(request: dict[str, Any], *,
                recursion_limit: int = 80) -> Iterator[dict]:
    """단일 에이전트 그래프 stream — UI 호환 이벤트 yield.

    iter_swarm 과 같은 이벤트 모양이지만 handoff 이벤트는 진입 시점 한 번만.
    """
    yield {
        "type":      "start",
        "entry":     "single_agent",
        "reasoning": "단일 RCA 분석가가 모든 도구를 직접 사용해 답합니다.",
    }
    yield {"type": "handoff", "agent": "single_agent"}

    config: dict[str, Any] = {
        "configurable": {"thread_id": f"single:{request.get('session_id') or 'default'}"},
        "recursion_limit": recursion_limit,
    }
    initial_state = {"messages": [HumanMessage(content=_user_text(request))]}

    seen_ids: set[str] = set()
    n_messages = 0

    try:
        for chunk in _get_graph().stream(initial_state, config=config, stream_mode="values"):
            for m in (chunk.get("messages") or []):
                mid = getattr(m, "id", None) or id(m)
                key = str(mid)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                yield {"type": "message", "message": normalize_message(m)}
                n_messages += 1
    except Exception as e:  # noqa: BLE001
        logger.exception("single agent stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {
        "type": "done",
        "final_active_agent": "single_agent",
        "handoffs": ["single_agent"],
        "n_messages": n_messages,
    }


def invoke_single(request: dict[str, Any], *,
                  recursion_limit: int = 80) -> dict[str, Any]:
    """동기 호출 — 모든 이벤트를 모아 최종 결과 dict 반환 (호환용)."""
    messages: list[dict] = []
    err: str | None = None
    n = 0
    for ev in iter_single(request, recursion_limit=recursion_limit):
        t = ev.get("type")
        if t == "message":
            messages.append(ev["message"])
            n += 1
        elif t == "error":
            err = ev.get("error")
    if err:
        return {"error": err, "messages": messages}
    return {
        "messages": messages,
        "handoffs": ["single_agent"],
        "final_active_agent": "single_agent",
        "aborted": None,
    }
