"""
perf_graph.py - SQL Server Query Performance 에이전트, LangGraph 기반 코어.

DBAOps(pipeline_graph)와 같은 설계 철학의 4노드 StateGraph:

    START → analyze → validate ─┬→ report → END
            (ReAct)   (검증)     └→ revise → report → END
                                  (검증 실패 시 1회 재분석)

- analyze : create_react_agent — Query Store/DMV 도구를 직접 골라 호출하는 RCA 루프
- validate: 별도 LLM 호출로 분석 결과 검증 (도구 인용 없는 주장/빈 답변/포맷 위반 탐지)
- revise  : 검증 지적사항을 주입해 1회 재분석 (루프 방지 위해 1회 한정)
- report  : 최종 마크다운 리포트 정리

도구는 기존 stdio MCP 서버(mcp_query_tools.py)를 langchain-mcp-adapters로 로드 +
ask_dbaops_agent(A2A peer). MCP 세션은 호출자(서버/CLI)가 열어 tools를 주입한다.

PERF_VALIDATION=0 이면 validate/revise 건너뛰고 analyze→report 직행 (지연 절감).
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Annotated, Any, Literal

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import create_react_agent
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
# perf 전용 모델(PERF_BEDROCK_MODEL_ID) 우선 — DBAOps의 BEDROCK_MODEL_ID(opus)와 분리.
BEDROCK_MODEL_ID = os.environ.get("PERF_BEDROCK_MODEL_ID") or os.environ.get(
    "BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")
OPS_A2A_URL = os.environ.get("OPS_A2A_URL", "http://127.0.0.1:8080")
ENABLE_A2A = os.environ.get("ENABLE_A2A", "1") == "1"
ENABLE_VALIDATION = os.environ.get("PERF_VALIDATION", "1") == "1"
RECURSION_LIMIT = int(os.environ.get("PERF_RECURSION_LIMIT", "60"))
A2A_CLIENT_TIMEOUT = int(os.environ.get("A2A_CLIENT_TIMEOUT", "600"))

SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_query_tools.py")


def get_llm() -> ChatBedrockConverse:
    kw: dict[str, Any] = {"model_id": BEDROCK_MODEL_ID, "region_name": AWS_REGION}
    t = os.environ.get("BEDROCK_TEMPERATURE")
    if t:
        kw["temperature"] = float(t)
    return ChatBedrockConverse(**kw)


# ───────────────────── A2A peer 도구 (dbaops) ─────────────────────

async def _a2a_ask_dbaops(question: str) -> str:
    """DBAOps A2A(:8080)에 질문을 보내고 응답 텍스트를 평문으로 수집."""
    async with httpx.AsyncClient(timeout=A2A_CLIENT_TIMEOUT) as hc:
        card = await A2ACardResolver(httpx_client=hc, base_url=OPS_A2A_URL).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=hc, streaming=False)).create(card)
        msg = Message(role=Role.user, parts=[Part(TextPart(text=question))],
                      message_id=uuid.uuid4().hex)

        def texts(parts):
            return [getattr(getattr(p, "root", p), "text", None)
                    for p in (parts or []) if getattr(getattr(p, "root", p), "text", None)]

        chunks: list[str] = []
        async for ev in client.send_message(msg):
            if isinstance(ev, tuple):
                task = ev[0]
                for art in (getattr(task, "artifacts", None) or []):
                    chunks += texts(getattr(art, "parts", None))
                status = getattr(task, "status", None)
                if status and getattr(status, "message", None):
                    chunks += texts(getattr(status.message, "parts", None))
            else:
                chunks += texts(getattr(ev, "parts", None))
        return "\n".join(chunks).strip() or "(DBAOps 응답 없음)"


def build_dbaops_tool() -> BaseTool:
    async def ask_dbaops_agent(question: str) -> str:
        try:
            return await _a2a_ask_dbaops(question)
        except Exception as e:  # noqa: BLE001
            return f"[DBAOps A2A error] {e}"

    return StructuredTool.from_function(
        coroutine=ask_dbaops_agent,
        name="ask_dbaops_agent",
        description=(
            "Ask the DBAOps RCA agent (peer over A2A) about OS/infra metrics, RDS MySQL, "
            "Kafka/MSK, or log analysis — infrastructure RCA outside query-level tuning. "
            "Input: a clear Korean question. Returns the peer's answer as text. "
            "Never use for SQL Server/PostgreSQL query tuning (your own job)."
        ),
    )


# ───────────────────── MCP 도구 로딩 ─────────────────────

async def load_perf_tools(session) -> list[BaseTool]:
    """열린 MCP ClientSession에서 도구 로드 + A2A peer 도구 추가."""
    from langchain_mcp_adapters.tools import load_mcp_tools
    tools = await load_mcp_tools(session)
    if ENABLE_A2A:
        tools.append(build_dbaops_tool())
    return list(tools)


# ───────────────────── 프롬프트 ─────────────────────

ANALYST_PROMPT = """You are a database query performance optimization specialist
covering **SQL Server, PostgreSQL, and MySQL** (multi-engine tools with a `target` parameter).

**Targets:** call list_db_targets() when unsure which database the user means.
If the user names one (e.g. "PG 쪽", "SQL Server"), pass the matching target.
If only one target exists, omit target (default applies).

**Investigation workflow:**
1. check_query_store_enabled(target) first — Query Store (mssql), pg_stat_statements
   (postgres), or performance_schema (mysql). Tools handle engine differences internally.
2. get_top_queries for expensive queries (windowed on mssql; cumulative on postgres —
   the tool's response says which)
3. Real-time: get_slow_queries / get_blocking_sessions / get_wait_stats /
   get_connection_stats
4. Optimization: suggest_indexes / get_index_usage / get_query_plan / get_table_health
5. If a tool returns {"unsupported": ...}, relay its hint honestly — never fake data
6. ONLY send Slack alerts when explicitly requested by the user

**Peer agent (A2A):** ask_dbaops_agent covers OS/infra metrics,
Kafka(MSK), and log analysis — infrastructure RCA outside query-level tuning.
Delegate those; quote its answer citing the DBAOps agent.
Never delegate SQL Server/PostgreSQL/MySQL query performance questions (your own job).

**Evidence rules (validation단계에서 검사됨):**
- Every concrete number/claim must come from a tool result you actually called
- If a tool returned an error or empty data, say so — never invent findings
- Answer in the user's language (Korean in → Korean out)

**Charts (선택):** 수치 비교/분포가 핵심일 때만, 답변 안에 아래 형식의 코드블록을 넣어라
(UI/Slack이 이 스펙과 네 도구 결과 데이터로 차트를 그린다):
```json-chart
{ "chart_type": "bar|line|scatter|histogram|table", "title": "<짧은 한글 제목>",
  "source_tool_call_id": "<네가 실제 호출한 도구 call id — 지어내지 말 것>",
  "x_field": "<dotted path>", "y_field": "<dotted path>" }
```
예) get_top_queries 결과 → x="queries[*].query_text", y="queries[*].total_cpu_ms".
맞는 call id가 없으면 차트는 생략. 표 데이터가 이미 텍스트로 충분하면 차트 불필요."""

VALIDATOR_PROMPT = """You are a strict reviewer of a database performance analysis (SQL Server/PostgreSQL).
Given the user request and the analyst's final answer (with the tools that were called),
check ONLY these failure modes:
1. Claims with concrete numbers/query names that no tool call supports (fabrication)
2. The answer is empty, cut off, or answers a different question
3. Tool errors were silently ignored (e.g. login failed but answer pretends data exists)

Reply in EXACTLY this format (no markdown):
VERDICT: PASS | FAIL
ISSUES: <한국어로 한 줄씩, 없으면 '없음'>"""

REPORTER_PROMPT = """You are a formatter. Rewrite the analyst's answer as a clean Korean
markdown report. Keep every fact/number exactly as-is (do NOT add or change findings).
Structure (omit sections that don't apply):
## 요약
## 발견 사항
## 권고 (인덱스/쿼리 개선 — CREATE INDEX 문 등 그대로 유지)
## 참고 (도구 제약, Query Store 상태, DBAOps 인용 출처 등)
Keep it concise. If the analyst's answer is already short (a greeting or one-liner),
just return it unchanged."""


# ───────────────────── State & 노드 ─────────────────────

class PerfState(TypedDict):
    messages: Annotated[list, add_messages]   # analyze ReAct 대화 (도구 호출 포함)
    user_input: str
    analysis: str            # analyze 최종 텍스트
    verdict: str             # PASS | FAIL | SKIP
    issues: str
    revised: bool
    final: str               # report 결과


def _last_ai_text(messages: list) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage) and not (getattr(m, "tool_calls", None) or []):
            c = m.content
            if isinstance(c, list):
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            if (c or "").strip():
                return c
    return ""


def _tool_activity_summary(messages: list) -> str:
    """validator에게 줄 '실제 호출된 도구' 요약."""
    lines = []
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            lines.append(f"- called {tc.get('name')}({tc.get('args')})")
    return "\n".join(lines[-30:]) or "(no tool calls)"


def build_graph(tools: list[BaseTool]):
    llm = get_llm()
    react = create_react_agent(model=llm, tools=tools,
                               prompt=SystemMessage(content=ANALYST_PROMPT))

    async def analyze(state: PerfState) -> dict:
        result = await react.ainvoke(
            {"messages": state["messages"] + [HumanMessage(content=state["user_input"])]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        msgs = result["messages"]
        return {"messages": msgs, "analysis": _last_ai_text(msgs)}

    async def validate(state: PerfState) -> dict:
        if not ENABLE_VALIDATION:
            return {"verdict": "SKIP", "issues": ""}
        prompt = (f"[사용자 요청]\n{state['user_input']}\n\n"
                  f"[실제 도구 호출]\n{_tool_activity_summary(state['messages'])}\n\n"
                  f"[분석 답변]\n{state['analysis'][:8000]}")
        resp = await llm.ainvoke([SystemMessage(content=VALIDATOR_PROMPT),
                                  HumanMessage(content=prompt)])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        verdict = "PASS" if "VERDICT: PASS" in text.upper() else "FAIL"
        issues = text.split("ISSUES:", 1)[-1].strip() if "ISSUES:" in text else ""
        return {"verdict": verdict, "issues": issues}

    async def revise(state: PerfState) -> dict:
        fix = (f"{state['user_input']}\n\n[검증 지적사항 — 반드시 고칠 것]\n{state['issues']}\n"
               f"지적된 부분만 도구를 다시 호출해 사실 기반으로 수정하라.")
        result = await react.ainvoke(
            {"messages": state["messages"] + [HumanMessage(content=fix)]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        msgs = result["messages"]
        return {"messages": msgs, "analysis": _last_ai_text(msgs), "revised": True}

    async def report(state: PerfState) -> dict:
        analysis = state["analysis"] or "(분석 결과 없음)"
        if len(analysis) < 200:      # 인사말 등 짧은 답은 포맷팅 생략
            return {"final": analysis}
        resp = await llm.ainvoke([SystemMessage(content=REPORTER_PROMPT),
                                  HumanMessage(content=analysis)])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        return {"final": text or analysis}

    def route(state: PerfState) -> Literal["revise", "report"]:
        if state["verdict"] == "FAIL" and not state.get("revised"):
            return "revise"
        return "report"

    g = StateGraph(PerfState)
    g.add_node("analyze", analyze)
    g.add_node("validate", validate)
    g.add_node("revise", revise)
    g.add_node("report", report)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "validate")
    g.add_conditional_edges("validate", route, {"revise": "revise", "report": "report"})
    g.add_edge("revise", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=InMemorySaver())


async def run_perf(graph, user_input: str, thread_id: str = "default") -> str:
    """그래프 1회 실행 → 최종 리포트 텍스트. thread_id로 대화 연속성 유지."""
    out = await graph.ainvoke(
        {"messages": [], "user_input": user_input, "analysis": "",
         "verdict": "", "issues": "", "revised": False, "final": ""},
        config={"configurable": {"thread_id": thread_id},
                "recursion_limit": RECURSION_LIMIT + 20},
    )
    return out.get("final") or out.get("analysis") or "(응답 없음)"


# ───────────────────── 스트리밍 실행 (DBAOps iter_single과 같은 이벤트 모양) ─────────────────────

def _normalize_message(m) -> dict:
    """LangChain BaseMessage → UI/Slack 렌더러가 쓰는 dict (dbaops normalize_message 호환)."""
    role = getattr(m, "type", None) or "ai"
    content = getattr(m, "content", None)
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    tcs = [{"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")}
           for tc in (getattr(m, "tool_calls", None) or [])]
    out = {"role": role, "name": getattr(m, "name", None),
           "text": (content or "")[:13000], "tool_calls": tcs}
    tcid = getattr(m, "tool_call_id", None)
    if tcid:
        out["tool_call_id"] = tcid
    return out


async def iter_perf(graph, user_input: str, thread_id: str = "default"):
    """그래프 실행을 스트리밍 — dbaops iter_single과 같은 이벤트 dict를 yield.

    이벤트: start / stage(analyze·validate·revise·report) / message /
            validation / report / done / error
    Slack·Streamlit이 dbaops와 동일한 렌더러로 진행상황·차트를 그릴 수 있다.
    """
    yield {"type": "start", "entry": "perf_agent",
           "reasoning": "쿼리 성능 분석가가 도구를 직접 골라 호출합니다."}

    config = {"configurable": {"thread_id": thread_id},
              "recursion_limit": RECURSION_LIMIT + 20}
    state = {"messages": [], "user_input": user_input, "analysis": "",
             "verdict": "", "issues": "", "revised": False, "final": ""}

    seen: set[str] = set()
    final_out: dict = {}
    try:
        async for chunk in graph.astream(state, config=config, stream_mode=["values", "updates"]):
            mode, payload = chunk if isinstance(chunk, tuple) else ("values", chunk)
            if mode == "updates":
                for node, upd in (payload or {}).items():
                    yield {"type": "stage", "stage": node, "status": "completed"}
                    if node == "validate" and isinstance(upd, dict) and upd.get("verdict"):
                        yield {"type": "validation",
                               "passed": upd["verdict"] != "FAIL",
                               "issues": ([{"kind": upd.get("issues", "")}]
                                          if upd.get("verdict") == "FAIL" else [])}
                continue
            # values 모드 — 새 메시지만 흘림
            if isinstance(payload, dict):
                final_out = payload
                for m in (payload.get("messages") or []):
                    key = str(getattr(m, "id", None) or id(m))
                    if key in seen:
                        continue
                    seen.add(key)
                    yield {"type": "message", "message": _normalize_message(m)}
    except Exception as e:  # noqa: BLE001
        logger.exception("perf stream failed")
        yield {"type": "error", "error": str(e)}
        return

    final = final_out.get("final") or final_out.get("analysis") or "(응답 없음)"
    yield {"type": "report", "markdown": final, "charts": []}
    yield {"type": "done", "final_active_agent": "perf_agent"}
