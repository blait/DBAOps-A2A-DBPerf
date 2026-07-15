"""
perf_graph.py - Query Performance 에이전트, LangGraph 기반 코어.

LLM 라우터가 요청 성격을 보고 두 경로 중 하나를 태운다 (ops와 동일한 정책):

    START → route ─┬→ single (ReAct 1방 — 대화·조회·가벼운 진단, 검증 없음, 빠름)
                   └→ analyze → validate ─┬→ report → END   (보고서 파이프라인)
                                          └→ revise → report → END

- route   : 별도 LLM 1호출 — "보고서/감사가능한 산출물을 원하는가?"만 판정
- single  : ops single_graph 스타일의 자유로운 ReAct — 짧고 대화체로 답
- analyze~report : 기존 4노드 검증 파이프라인 (근거 검증 + 정형 리포트)

강제 오버라이드: PERF_MODE=single|report (env) 또는 요청별 mode 인자.
PERF_VALIDATION=0 이면 파이프라인에서 validate/revise 생략.

도구는 기존 stdio MCP 서버(mcp_query_tools.py)를 langchain-mcp-adapters로 로드 +
ask_dbaops_agent(A2A peer). MCP 세션은 호출자(서버/CLI)가 열어 tools를 주입한다.
"""
from __future__ import annotations

import logging
import os
import re
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
# perf 전용 모델(PERF_BEDROCK_MODEL_ID) 우선 — 미설정 시 BEDROCK_MODEL_ID → 기본 Opus 4.8.
BEDROCK_MODEL_ID = os.environ.get("PERF_BEDROCK_MODEL_ID") or os.environ.get(
    "BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-8")
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

# ── single 모드: ops single_graph 스타일 — 자유로운 대화형 ──
SINGLE_PROMPT = """You are **Perf** — a senior DBA colleague who helps with query performance
over chat. You answer in Korean, naturally, the way a sharp teammate would in Slack.
You cover **SQL Server, PostgreSQL, MySQL** with one tool set (pass `target` to pick the DB;
call list_db_targets() when unsure which one the user means).

<how_you_work>
Talk to the user, don't file reports at them. 답은 짧고 핵심만 — 간단한 질문엔 소제목·섹션 없이
바로 답한다. 요청한 깊이에 맞춰라, 그 이상도 이하도 아니게.

- 가벼운 질문(개념·방법·"이거 뭐야"·잡담)이면 그냥 대화로 답한다. 도구도 형식도 필요 없다.
- 데이터를 묻는 질문("상위 쿼리 보여줘", "블로킹 있어?")이면 맞는 도구로 확인하고 핵심을
  짧게 전한다. 표·차트는 도움 될 때만.
- 도구를 쓰기 전에 한 문장으로 뭘 확인할지 말해라("mysql-poc 상위 쿼리 먼저 볼게요").
  진행 중 발견·방향전환·막힘이 생기면 한 줄씩 알린다.
- 답에 데이터가 들어가면 근거를 가볍게: 어떤 도구로 어떤 수치를 봤는지.
- "이상 없음"은 실제로 확인했을 때만. 확실한 것과 추측은 말투로 구분한다.
- 도구가 {"unsupported": ...}를 주면 그 제약을 정직하게 전달한다 — 지어내지 않는다.
- 끝맺음은 한두 문장: 뭘 알아냈고 다음은 뭔지. 더 파볼 여지가 있으면 자연스럽게 권한다.
</how_you_work>

<peer_agent>
동료: **DBAOps RCA agent** (ask_dbaops_agent, A2A). OS/호스트 메트릭, Kafka(MSK), 로그,
RDS 이벤트 등 쿼리 튜닝 밖 인프라 질문은 그쪽에 위임하고 답을 출처와 함께 인용한다.
쿼리 성능(mssql/pg/mysql)은 절대 위임하지 않는다 — 그건 네 일이다.
</peer_agent>

<charts>
수치 비교가 핵심이고 사용자가 시각화를 원하면, 답 안에 이 형식의 코드블록을 넣는다
(UI/Slack이 도구 결과 데이터와 매칭해 차트 이미지로 렌더):
```json-chart
{ "chart_type": "bar|line|scatter|histogram|table", "title": "<짧은 한글 제목>",
  "source_tool_call_id": "<실제 호출한 도구 call id>", "x_field": "<dotted path>", "y_field": "<dotted path>" }
```
예) get_top_queries → x="queries[*].query_text", y="queries[*].calls".
</charts>

ONLY send Slack alerts when explicitly requested by the user."""

# ── 라우터: single vs report 파이프라인 판정 (LLM 1호출) ──
ROUTER_PROMPT = """You are a request router for a database performance agent.
Decide if the user wants a FORMAL REPORT (audited, structured deliverable) or a normal chat answer.

Choose REPORT only when the user explicitly asks for a report/summary document, an audit,
a comprehensive analysis to share/keep ("보고서", "리포트로", "정리해서 문서로", "종합 분석해서
보고", "감사용", "경영진에게"), or asks for validated findings.
Everything else — questions, live checks, tuning advice, chart requests, casual chat — is SINGLE.

Reply with EXACTLY one word: SINGLE or REPORT"""

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
CRITICAL: if the answer contains ```json-chart ...``` fenced blocks, copy them into the
report VERBATIM — same fence tag `json-chart`, same JSON, do not rename to `json`,
do not drop them (the UI renders these into chart images).
Keep it concise. If the analyst's answer is already short (a greeting or one-liner),
just return it unchanged."""

# report 재작성에서 json-chart 펜스가 유실/변형될 때 원본에서 복구하기 위한 패턴
_CHART_FENCE = re.compile(r"```json-chart\s*\n.*?\n```", re.DOTALL)


# ───────────────────── State & 노드 ─────────────────────

class PerfState(TypedDict):
    messages: Annotated[list, add_messages]   # ReAct 대화 (도구 호출 포함)
    user_input: str
    mode: str                # "" (라우터가 결정) | "single" | "report" (강제)
    route: str               # 라우터 판정 결과: single | report
    analysis: str            # analyze/single 최종 텍스트
    verdict: str             # PASS | FAIL | SKIP
    issues: str
    revised: bool
    final: str               # 최종 답변


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
    react_single = create_react_agent(model=llm, tools=tools,
                                      prompt=SystemMessage(content=SINGLE_PROMPT))

    async def route(state: PerfState) -> dict:
        forced = (state.get("mode") or os.environ.get("PERF_MODE", "")).lower()
        if forced in ("single", "report"):
            return {"route": forced}
        resp = await llm.ainvoke([SystemMessage(content=ROUTER_PROMPT),
                                  HumanMessage(content=state["user_input"][:2000])])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        decision = "report" if "REPORT" in text.upper() else "single"
        logger.info("router: %s", decision)
        return {"route": decision}

    async def single(state: PerfState) -> dict:
        result = await react_single.ainvoke(
            {"messages": state["messages"] + [HumanMessage(content=state["user_input"])]},
            config={"recursion_limit": RECURSION_LIMIT},
        )
        msgs = result["messages"]
        text = _last_ai_text(msgs) or "(응답 없음)"
        return {"messages": msgs, "analysis": text, "final": text,
                "verdict": "SKIP", "issues": ""}

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
        text = text or analysis
        # 재작성 과정에서 json-chart 펜스가 빠졌으면 원본 것을 뒤에 복구
        orig_charts = _CHART_FENCE.findall(analysis)
        if orig_charts and not _CHART_FENCE.search(text):
            text = text + "\n\n" + "\n\n".join(orig_charts)
        return {"final": text}

    def pick_path(state: PerfState) -> Literal["single", "analyze"]:
        return "single" if state["route"] == "single" else "analyze"

    def after_validate(state: PerfState) -> Literal["revise", "report"]:
        if state["verdict"] == "FAIL" and not state.get("revised"):
            return "revise"
        return "report"

    g = StateGraph(PerfState)
    g.add_node("route", route)
    g.add_node("single", single)
    g.add_node("analyze", analyze)
    g.add_node("validate", validate)
    g.add_node("revise", revise)
    g.add_node("report", report)
    g.add_edge(START, "route")
    g.add_conditional_edges("route", pick_path, {"single": "single", "analyze": "analyze"})
    g.add_edge("single", END)
    g.add_edge("analyze", "validate")
    g.add_conditional_edges("validate", after_validate, {"revise": "revise", "report": "report"})
    g.add_edge("revise", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=InMemorySaver())


def _initial_state(user_input: str, mode: str = "") -> dict:
    return {"messages": [], "user_input": user_input, "mode": mode, "route": "",
            "analysis": "", "verdict": "", "issues": "", "revised": False, "final": ""}


async def run_perf(graph, user_input: str, thread_id: str = "default", mode: str = "") -> str:
    """그래프 1회 실행 → 최종 텍스트. mode=""(라우터 판단)|single|report."""
    out = await graph.ainvoke(
        _initial_state(user_input, mode),
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


async def iter_perf(graph, user_input: str, thread_id: str = "default", mode: str = ""):
    """그래프 실행을 스트리밍 — dbaops iter_single과 같은 이벤트 dict를 yield.

    이벤트: start / stage(route·single·analyze·validate·revise·report) / message /
            validation / report / done / error
    Slack·Streamlit이 dbaops와 동일한 렌더러로 진행상황·차트를 그릴 수 있다.
    """
    yield {"type": "start", "entry": "perf_agent",
           "reasoning": "쿼리 성능 분석가가 도구를 직접 골라 호출합니다."}

    config = {"configurable": {"thread_id": thread_id},
              "recursion_limit": RECURSION_LIMIT + 20}
    state = _initial_state(user_input, mode)

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
