"""Pipeline graph — 3 도메인 단일 에이전트 + 검증 + 리포트.

흐름:
  user → domain_agent → validation → (passed) report → END
                          │
                          (failed, revise<1) → revise → report → END
                          (failed, revise≥1) → report (with warning) → END

설계:
- domain_agent 는 create_react_agent (LLM + 모든 MCP 도구). 도메인 prompt 가 책임 영역 안내.
- validation 은 LLM-only. JSON 결과 파싱.
- revise 는 domain_agent 를 issues 와 함께 한 번 더 호출.
- report 는 LLM-only. tool history 와 최종 응답을 받아 markdown + chart spec 생성.

이벤트 형태 — UI 호환:
  {"type": "start", "entry": "domain", "domain": <key>, "reasoning": ...}
  {"type": "stage", "stage": "domain"|"validation"|"revise"|"report", "status": "running"|"completed"}
  {"type": "handoff", "agent": <stage 노드명 — UI 의 chat_message 그룹핑용>}
  {"type": "message", "message": <normalized>}
  {"type": "validation", "passed": bool, "issues": [...]}
  {"type": "report", "markdown": str, "charts": [...]}
  {"type": "done", ...}
  {"type": "error", "error": str}
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

from .llm import get_llm
from .tools.mcp_auto import build_mcp_tools
from .tools.mcp_tools import infra_context

logger = logging.getLogger(__name__)


# ─────────────────────── prompts 로드 ───────────────────────

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _read(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


def _domain_system_prompt(domain_key: str) -> str:
    common = _read("_common.md").format(**infra_context())
    domain_tpl = _read(f"domain_{domain_key}.md")
    return domain_tpl.format(common=common)


_VALIDATION_SYSTEM = _read("validation.md")
_REVISE_TEMPLATE = _read("revise.md")
_REPORT_SYSTEM = _read("report.md")


# ─────────────────────── 도메인 레지스트리 ───────────────────────

_DOMAINS = {
    "os_metric": "🖥️ OS·인프라 메트릭",
    "db_metric": "🗄️ DB 성능 메트릭",
    "log":       "📜 로그 분석",
}


def domain_keys() -> list[str]:
    return list(_DOMAINS.keys())


def domain_label(key: str) -> str:
    return _DOMAINS.get(key, key)


# ─────────────────────── State ───────────────────────


class PipelineState(TypedDict, total=False):
    domain:           str                  # os_metric / db_metric / log
    user_text:        str                  # 사용자 원본 요청 (revise 시 재사용)
    domain_messages:  list[BaseMessage]    # domain react agent 의 turn 메시지 누적
    domain_response:  str                  # 최신 domain 응답 텍스트
    validation:       dict                 # {"passed": bool, "issues": [...]}
    revise_count:     int
    report_markdown:  str
    report_charts:    list[dict]


# ─────────────────────── 노드: domain ───────────────────────


_DOMAIN_AGENTS_CACHE: dict[str, Any] = {}


def _get_domain_agent(domain_key: str):
    if domain_key in _DOMAIN_AGENTS_CACHE:
        return _DOMAIN_AGENTS_CACHE[domain_key]
    tools = build_mcp_tools(max_response_chars=12000)
    sys_prompt = _domain_system_prompt(domain_key)
    agent = create_react_agent(
        model=get_llm(),
        tools=tools,
        prompt=SystemMessage(content=sys_prompt),
        name=f"{domain_key}_agent",
    )
    _DOMAIN_AGENTS_CACHE[domain_key] = agent
    logger.info("pipeline_graph: built %s_agent with %d tools", domain_key, len(tools))
    return agent


def _last_text(messages: list[BaseMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
            content = m.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                joined = "\n".join(p for p in parts if p)
                if joined.strip():
                    return joined
    return ""


def _domain_node(state: PipelineState) -> dict:
    domain = state["domain"]
    agent = _get_domain_agent(domain)
    initial = {"messages": [HumanMessage(content=state["user_text"])]}
    result = agent.invoke(initial, config={"recursion_limit": 60})
    msgs = result.get("messages", []) or []
    return {
        "domain_messages": msgs,
        "domain_response": _last_text(msgs),
    }


# ─────────────────────── 노드: validation ───────────────────────


def _validation_node(state: PipelineState) -> dict:
    history_text = _format_history_for_validation(state.get("domain_messages") or [])
    user_msg = (
        f"<original_question>\n{state['user_text']}\n</original_question>\n\n"
        f"<conversation>\n{history_text}\n</conversation>\n\n"
        f"<analyst_final_response>\n{state.get('domain_response','')}\n</analyst_final_response>\n\n"
        "Inspect the analyst_final_response for the three failure modes and output the JSON object."
    )
    msgs = [SystemMessage(content=_VALIDATION_SYSTEM), HumanMessage(content=user_msg)]
    resp = get_llm().invoke(msgs)
    text = resp.content if isinstance(resp.content, str) else _flatten_content(resp.content)
    parsed = _parse_validation_json(text)
    return {"validation": parsed}


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
        return "\n".join(parts)
    return str(content or "")


def _parse_validation_json(text: str) -> dict:
    """LLM 응답에서 첫 JSON object 추출."""
    if not text:
        return {"passed": True, "issues": []}
    # 시도 1: 직접 parse
    stripped = text.strip()
    try:
        if stripped.startswith("{"):
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                return _normalize_validation(obj)
    except json.JSONDecodeError:
        pass
    # 시도 2: 첫 { ... } 블록 추출
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return _normalize_validation(obj)
        except json.JSONDecodeError:
            pass
    logger.warning("validation parse failed: %s", text[:200])
    return {"passed": True, "issues": []}  # parse 실패 시 통과 처리 (over-strict 방지)


def _normalize_validation(obj: dict) -> dict:
    passed = bool(obj.get("passed", True))
    issues = obj.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    norm_issues = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        norm_issues.append({
            "kind":   str(it.get("kind") or "unknown"),
            "detail": str(it.get("detail") or "")[:500],
        })
    return {"passed": passed and not norm_issues, "issues": norm_issues}


def _format_history_for_validation(messages: list[BaseMessage], *, max_chars: int = 8000) -> str:
    """domain agent 의 message history 를 validation prompt 에 들어갈 텍스트로 압축."""
    lines: list[str] = []
    for m in messages:
        role = getattr(m, "type", None) or m.__class__.__name__
        content = getattr(m, "content", "")
        text = content if isinstance(content, str) else _flatten_content(content)
        tcs = getattr(m, "tool_calls", None) or []
        if tcs:
            for tc in tcs:
                lines.append(f"[{role} tool_call] {tc.get('name')} args={json.dumps(tc.get('args') or {}, ensure_ascii=False, default=str)[:200]}")
        if text:
            lines.append(f"[{role}] {text[:1500]}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n...(truncated)"
    return out


# ─────────────────────── 노드: revise ───────────────────────


def _revise_node(state: PipelineState) -> dict:
    domain = state["domain"]
    agent = _get_domain_agent(domain)
    issues = state.get("validation", {}).get("issues") or []
    issues_text = "\n".join(f"- ({i['kind']}) {i['detail']}" for i in issues) or "(no specific issues)"
    revise_msg = _REVISE_TEMPLATE.format(issues=issues_text)

    # 기존 domain_messages 뒤에 user 의 revise 지시를 붙여 재실행
    prev_msgs = state.get("domain_messages") or []
    new_input = list(prev_msgs) + [HumanMessage(content=revise_msg)]
    result = agent.invoke({"messages": new_input}, config={"recursion_limit": 40})
    msgs = result.get("messages", []) or []
    return {
        "domain_messages": msgs,
        "domain_response": _last_text(msgs),
        "revise_count":    state.get("revise_count", 0) + 1,
    }


# ─────────────────────── 노드: report ───────────────────────


def _condensed_tool_history(messages: list[BaseMessage], *, limit: int = 30) -> str:
    """report agent 한테 줄 tool 호출 요약 — id, name, args, 결과 sample."""
    lines: list[str] = []
    by_id: dict[str, dict] = {}
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            tcid = tc.get("id") or ""
            by_id[tcid] = {
                "id":   tcid,
                "name": tc.get("name"),
                "args": tc.get("args") or {},
            }
    for m in messages:
        if getattr(m, "type", None) == "tool":
            tcid = getattr(m, "tool_call_id", "") or ""
            if tcid in by_id:
                content = getattr(m, "content", "")
                text = content if isinstance(content, str) else _flatten_content(content)
                by_id[tcid]["sample"] = text[:600]
    for i, info in enumerate(list(by_id.values())[:limit]):
        args_s = json.dumps(info.get("args") or {}, ensure_ascii=False, default=str)[:200]
        sample = info.get("sample", "")[:400]
        lines.append(f"[{i+1}] id={info['id']}  tool={info['name']}\n    args={args_s}\n    sample={sample}")
    return "\n".join(lines) if lines else "(no tool calls)"


def _report_node(state: PipelineState) -> dict:
    msgs_history = state.get("domain_messages") or []
    tool_summary = _condensed_tool_history(msgs_history)
    validation = state.get("validation") or {"passed": True, "issues": []}
    revise_used = state.get("revise_count", 0)

    user_msg = (
        f"<user_question>\n{state['user_text']}\n</user_question>\n\n"
        f"<analyst_final_response>\n{state.get('domain_response','')}\n</analyst_final_response>\n\n"
        f"<tool_history>\n{tool_summary}\n</tool_history>\n\n"
        f"<validation>\npassed={validation.get('passed')} issues={len(validation.get('issues') or [])} "
        f"revise_used={revise_used}\n</validation>\n\n"
        "Write the markdown report following report_structure and chart_spec."
    )
    resp = get_llm().invoke([SystemMessage(content=_REPORT_SYSTEM), HumanMessage(content=user_msg)])
    md = resp.content if isinstance(resp.content, str) else _flatten_content(resp.content)
    charts = _extract_chart_specs(md)
    return {"report_markdown": md or "", "report_charts": charts}


_CHART_FENCE_RE = re.compile(r"```json-chart\s*\n([\s\S]*?)\n```", re.MULTILINE)


def _extract_chart_specs(markdown: str) -> list[dict]:
    out: list[dict] = []
    if not markdown:
        return out
    for m in _CHART_FENCE_RE.finditer(markdown):
        body = m.group(1).strip()
        try:
            spec = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("chart spec parse failed: %s", body[:200])
            continue
        if isinstance(spec, dict):
            out.append(spec)
    return out


# ─────────────────────── 분기 (validation → ...) ───────────────────────


def _route_after_validation(state: PipelineState) -> Literal["revise", "report"]:
    v = state.get("validation") or {}
    if v.get("passed"):
        return "report"
    if state.get("revise_count", 0) < 1:
        return "revise"
    return "report"


# ─────────────────────── 그래프 빌드 ───────────────────────


_GRAPH = None


def _build_graph():
    g = StateGraph(PipelineState)
    g.add_node("domain",     _domain_node)
    g.add_node("validation", _validation_node)
    g.add_node("revise",     _revise_node)
    g.add_node("report",     _report_node)

    g.add_edge(START,        "domain")
    g.add_edge("domain",     "validation")
    g.add_conditional_edges("validation", _route_after_validation, {"revise": "revise", "report": "report"})
    g.add_edge("revise",     "report")
    g.add_edge("report",     END)

    return g.compile(checkpointer=InMemorySaver())


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


# ─────────────────────── 외부 API ───────────────────────


def _format_fast_context(fast: dict[str, Any]) -> str:
    """직전 turn fast/swarm 응답 요약을 사용자 메시지에 prepend 할 텍스트로.
    swarm_graph 시절 호환을 위해 single_graph 가 import 함."""
    if not fast:
        return ""
    lines: list[str] = ["[1차 fast 분석 결과 — 이미 확보된 정보]"]
    findings = fast.get("findings") or []
    hypotheses = fast.get("hypotheses") or []
    next_actions = fast.get("next_actions") or []
    if findings:
        lines.append(f"\n## findings ({len(findings)}건)")
        for f in findings[:30]:
            sev = (f.get("severity") or "info").upper()
            dom = f.get("domain") or "?"
            fid = f.get("id") or "?"
            title = f.get("title") or ""
            lines.append(f"- [{sev}][{dom}] ({fid}) {title}")
    if hypotheses:
        lines.append(f"\n## hypotheses ({len(hypotheses)}건)")
        for h in hypotheses[:10]:
            conf = h.get("confidence", 0.0) or 0.0
            refs = ", ".join(h.get("supporting_finding_ids") or [])
            lines.append(f"- (conf {conf:.2f}, refs={refs}) {h.get('statement','')}")
    if next_actions:
        lines.append("\n## next_actions")
        for a in next_actions[:10]:
            lines.append(f"- {a}")
    return "\n".join(lines)


def normalize_message(m: Any) -> dict:
    """LangChain BaseMessage → UI 가 쓰는 dict 형태. (swarm_graph 와 호환)"""
    role = getattr(m, "type", None) or "ai"
    name = getattr(m, "name", None)
    content = getattr(m, "content", None)
    text = content if isinstance(content, str) else _flatten_content(content)
    tcs = []
    for tc in (getattr(m, "tool_calls", None) or []):
        tcs.append({"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("args")})
    # 13000: tool 응답은 mcp_auto 가 12000자 내 '유효한 JSON'으로 이미 줄여놓음.
    # 여기서 그보다 작게 자르면 JSON 이 깨져 UI/Slack 차트 렌더가 실패한다.
    out = {
        "role":       role,
        "name":       name,
        "text":       (text or "")[:13000],
        "tool_calls": tcs,
    }
    tcid = getattr(m, "tool_call_id", None)
    if tcid:
        out["tool_call_id"] = tcid
    return out


def _user_text(request: dict[str, Any]) -> str:
    tr = request.get("time_range") or {}
    head = (
        f"분석 요청: {request.get('free_text','(없음)')}\n"
        f"time_range: {tr.get('start','?')} → {tr.get('end','?')}"
    )
    fc = request.get("fast_context") or {}
    if fc:
        # 직전 turn 요약을 hint 로
        head += "\n\n[직전 turn 컨텍스트 — 참고용]"
        for h in (fc.get("hypotheses") or [])[:3]:
            head += f"\n- {h.get('statement','')[:300]}"
    return head


def iter_pipeline(request: dict[str, Any]) -> Iterator[dict]:
    """Pipeline 실행을 stream — 단계별 이벤트 yield.

    request:
      domain:     "os_metric"|"db_metric"|"log" (필수)
      free_text:  사용자 질문
      time_range: {start, end}
      session_id: thread_id 결정용
      fast_context: 직전 turn (옵션)
    """
    domain = request.get("domain")
    if domain not in _DOMAINS:
        yield {"type": "error", "error": f"unknown domain: {domain}. valid={domain_keys()}"}
        return

    yield {
        "type":      "start",
        "entry":     "domain",
        "domain":    domain,
        "reasoning": f"[{domain_label(domain)}] domain agent 가 요청을 분석합니다.",
    }

    initial = {
        "domain":       domain,
        "user_text":    _user_text(request),
        "revise_count": 0,
    }
    config = {
        "configurable":   {"thread_id": f"pipeline:{domain}:{request.get('session_id') or 'default'}"},
        "recursion_limit": 40,
    }

    # stage 진행 + message 이벤트를 동시에 emit 하기 위해 stream_mode="values" 사용.
    # 각 노드 종료마다 state 가 한 번씩 emit 됨.
    seen_msg_ids: set[str] = set()
    last_node: str | None = None
    last_revise_count = 0
    validation_emitted = False
    report_emitted = False

    try:
        for chunk in _get_graph().stream(initial, config=config, stream_mode="values"):
            # 어느 노드까지 진행됐는지 추정
            current_node = _current_node_from_state(chunk)
            if current_node and current_node != last_node:
                yield {"type": "stage", "stage": current_node, "status": "completed"}
                yield {"type": "handoff", "agent": _stage_to_agent(current_node, domain)}
                last_node = current_node

            # domain_messages 새 메시지 emit
            for m in (chunk.get("domain_messages") or []):
                mid = getattr(m, "id", None) or id(m)
                key = str(mid)
                if key in seen_msg_ids:
                    continue
                seen_msg_ids.add(key)
                yield {"type": "message", "message": normalize_message(m)}

            # validation 결과 한 번만
            if not validation_emitted and chunk.get("validation") is not None:
                v = chunk["validation"]
                yield {"type": "validation", "passed": v.get("passed", True), "issues": v.get("issues") or []}
                validation_emitted = True

            # revise 진행 여부
            if chunk.get("revise_count", 0) > last_revise_count:
                last_revise_count = chunk["revise_count"]

            # report 한 번만
            if not report_emitted and chunk.get("report_markdown"):
                yield {
                    "type":     "report",
                    "markdown": chunk["report_markdown"],
                    "charts":   chunk.get("report_charts") or [],
                }
                report_emitted = True
    except Exception as e:  # noqa: BLE001
        logger.exception("pipeline stream failed")
        yield {"type": "error", "error": str(e)}
        return

    yield {
        "type":               "done",
        "final_active_agent": "report_agent",
        "handoffs":           ["domain_agent", "validation_agent", *(["revise_agent"] if last_revise_count else []), "report_agent"],
        "n_messages":         len(seen_msg_ids),
    }


def _current_node_from_state(chunk: dict) -> str | None:
    """state 의 어느 키가 채워졌는지로 직전 완료 노드 추정."""
    if chunk.get("report_markdown") is not None and chunk.get("report_markdown") != "":
        return "report"
    if chunk.get("revise_count", 0) > 0 and chunk.get("validation") is not None:
        # revise 후엔 domain_messages 가 갱신됨 — 노드명은 'revise'
        return "revise"
    if chunk.get("validation") is not None:
        return "validation"
    if chunk.get("domain_messages"):
        return "domain"
    return None


def _stage_to_agent(stage: str, domain: str) -> str:
    """stage 명을 UI avatar 매핑 키로."""
    if stage == "domain" or stage == "revise":
        return f"{domain}_agent"
    if stage == "validation":
        return "validation_agent"
    if stage == "report":
        return "report_agent"
    return stage


def invoke_pipeline(request: dict[str, Any]) -> dict[str, Any]:
    """동기 호출 — 모든 이벤트를 모아 dict 반환."""
    messages: list[dict] = []
    handoffs: list[str] = []
    final_active: str | None = None
    err: str | None = None
    validation: dict | None = None
    report: dict | None = None

    for ev in iter_pipeline(request):
        t = ev.get("type")
        if t == "message":
            messages.append(ev["message"])
        elif t == "handoff":
            handoffs.append(ev["agent"])
        elif t == "validation":
            validation = {"passed": ev.get("passed"), "issues": ev.get("issues")}
        elif t == "report":
            report = {"markdown": ev.get("markdown"), "charts": ev.get("charts")}
        elif t == "done":
            final_active = ev.get("final_active_agent")
        elif t == "error":
            err = ev.get("error")

    if err:
        return {"error": err, "messages": messages}
    return {
        "messages":           messages,
        "handoffs":           handoffs,
        "final_active_agent": final_active,
        "validation":         validation,
        "report":             report,
    }
