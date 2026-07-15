"""DBAOps-Agent Streamlit chat UI — 3 supervisor 탭 + 시나리오 라이브 모니터."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import streamlit as st

from agentcore_client import invoke_stream as agentcore_invoke_stream
from components import view_connections, view_generators, view_swarm

# 올인원 EC2: agent HTTP 직접 호출 모드 / generator 탭 노출 여부
AGENT_HTTP_URL = os.environ.get("AGENT_HTTP_URL", "")
SHOW_GENERATORS = os.environ.get("SHOW_GENERATORS", "false").lower() in ("1", "true", "yes")

st.set_page_config(page_title="DBAOps Agent", layout="wide")
st.title("DBAOps Agent")
st.caption("LangGraph + MCP — DB/인프라 RCA 분석 에이전트")

# ───────────────────────── 세션 상태 ─────────────────────────
SUPERVISORS: list[dict] = [
    {
        "key":   "single",
        "label": "🤖 DBAOps Agent",
        "tab":   "🤖 DBAOps Agent",
        "responsibility": "한 명의 RCA 분석가가 모든 도구(DB·메트릭·로그·PI·Prometheus)를 직접 사용해 종합 분석.",
        "input_hint":     "주요 입력: 시간 범위 + 자연어 질문",
        "deliverable":    "분류 + 발견 사실(인용 포함) + 가설(hedging) + 권고",
        "example":        "예: 'Aurora 최근 1시간 어디서 병목이 났는지 메트릭/로그/PI 종합해서 분석'",
        "mode":           "auto",   # LLM 라우터 — 보고서 요청이면 pipeline, 아니면 single
        "domain":         None,
    },
]

for s in SUPERVISORS:
    h_key = f"history__{s['key']}"
    sid_key = f"session_id__{s['key']}"
    if h_key not in st.session_state:
        st.session_state[h_key] = []
    if sid_key not in st.session_state:
        st.session_state[sid_key] = str(uuid.uuid4())[:8]
if "tracked_tasks" not in st.session_state:
    st.session_state["tracked_tasks"] = []


# ───────────────────────── Sidebar ─────────────────────────
with st.sidebar:
    st.markdown("### 분석 옵션")
    st.caption("⏱ 시간 범위 (UTC) — default 최근 1시간")
    now = datetime.now(timezone.utc)
    default_start = now - timedelta(hours=1)
    start = st.text_input("Start (UTC)", default_start.isoformat(timespec="seconds"))
    end = st.text_input("End (UTC)", now.isoformat(timespec="seconds"))

    if st.button("⏱ 최근 1시간으로 초기화", use_container_width=True):
        n = datetime.now(timezone.utc)
        st.session_state["__start_reset"] = (n - timedelta(hours=1)).isoformat(timespec="seconds")
        st.session_state["__end_reset"]   = n.isoformat(timespec="seconds")
        st.rerun()
    if "__start_reset" in st.session_state:
        start = st.session_state.pop("__start_reset")
    if "__end_reset" in st.session_state:
        end = st.session_state.pop("__end_reset")

    st.divider()
    use_prev_context = st.toggle(
        "이전 답변을 다음 요청에 컨텍스트로 사용",
        value=True,
        help="직전 turn 의 supervisor 응답을 다음 요청 hint 로 자동 주입.",
    )

    st.divider()
    if st.button("🗑 대화 초기화", use_container_width=True):
        for s in SUPERVISORS:
            st.session_state[f"history__{s['key']}"] = []
            st.session_state[f"session_id__{s['key']}"] = str(uuid.uuid4())[:8]
        st.rerun()

    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
    if AGENT_HTTP_URL:
        st.caption(f"backend: `agent HTTP` → {AGENT_HTTP_URL}")
    else:
        st.caption(f"runtime: `{runtime_arn.rsplit('/',1)[-1] or '(unset)'}`")
    for s in SUPERVISORS:
        st.caption(f"session: `{st.session_state[f'session_id__{s['key']}']}`")


# ───────────────────────── 직전 컨텍스트 헬퍼 ─────────────────────────
def _build_prev_context(sup_key: str) -> dict:
    history = st.session_state.get(f"history__{sup_key}") or []
    if not history:
        return {}
    last = history[-1]
    sw = last.get("swarm") or {}
    if not sw.get("messages"):
        return {}
    for m in reversed(sw["messages"]):
        if m.get("role") == "ai" and not (m.get("tool_calls") or []) and (m.get("text") or "").strip():
            return {"hypotheses": [{
                "confidence": 0.5,
                "statement":  (m.get("text") or "")[:1500],
                "supporting_finding_ids": [],
            }]}
    return {}


def _summarize_turn(turn: dict) -> str:
    sw = turn.get("swarm") or {}
    bits: list[str] = []
    if sw and sw.get("messages"):
        bits.append(f"msg {len(sw['messages'])} · handoff {max(0, len(sw.get('handoffs') or []) - 1)}")
    elapsed = turn.get("elapsed")
    if elapsed:
        bits.append(f"⏱ {elapsed:.1f}s")
    return " · ".join(bits)


# ───────────────────────── 메인 탭 ─────────────────────────
chat_tab_labels = [s["tab"] for s in SUPERVISORS] + ["🔌 MCP 연결설정"]
if SHOW_GENERATORS:
    chat_tab_labels.append("🧪 시나리오 라이브 모니터")
chat_tabs = st.tabs(chat_tab_labels)


def _render_supervisor_tab(s: dict) -> None:
    sup_key = s["key"]
    h_key = f"history__{sup_key}"
    sid_key = f"session_id__{sup_key}"

    with st.container(border=True):
        st.markdown(f"### {s['label']}")
        st.markdown(f"**핵심 책임**: {s['responsibility']}")
        st.caption(s["input_hint"])
        st.caption(s["deliverable"])
        st.caption(s["example"])

    history = st.session_state.get(h_key) or []
    for turn in history:
        with st.chat_message("user", avatar="🙋"):
            st.markdown(turn.get("free_text") or "_(empty)_")
            st.caption(
                f"window {turn.get('start','?')[:19]} → {turn.get('end','?')[:19]}"
            )

        with st.chat_message("assistant", avatar="🤖"):
            st.caption(_summarize_turn(turn))
            sw = turn.get("swarm") or {}
            if sw:
                with st.expander("🐝 Supervisor 대화 / 최종 정리", expanded=True):
                    view_swarm.render(sw, request={
                        "supervisor": sup_key,
                        "free_text":  turn.get("free_text"),
                        "time_range": {"start": turn.get("start"), "end": turn.get("end")},
                    })

    prefill = st.session_state.get("chat_prefill")
    if prefill and prefill.get("supervisor") == sup_key:
        st.session_state.pop("chat_prefill", None)
        st.info(
            f"📋 시나리오 추천 prompt 가 준비됐습니다.  \n"
            f"`{prefill.get('free_text','')}`  \n"
            f"채팅 입력창에 붙여 넣고 Enter 만 치면 분석이 시작됩니다."
        )
        st.session_state[f"chat_prefill_pending__{sup_key}"] = prefill

    prompt = st.chat_input(
        f"분석 요청 입력 — {s['tab']}",
        key=f"chat_input__{sup_key}",
    )

    if not prompt:
        return

    if not runtime_arn and not AGENT_HTTP_URL:
        st.warning("AGENT_HTTP_URL 또는 AGENTCORE_RUNTIME_ARN 이 설정되지 않아 호출할 수 없습니다.")
        st.stop()

    st.session_state.pop(f"chat_prefill_pending__{sup_key}", None)

    request_mode = s.get("mode") or "pipeline"
    base_request: dict = {
        "mode":        request_mode,
        "time_range":  {"start": start, "end": end},
        "free_text":   prompt,
        "session_id":  st.session_state[sid_key],
    }
    if request_mode == "pipeline":
        base_request["domain"] = s.get("domain") or sup_key

    with st.chat_message("user", avatar="🙋"):
        st.markdown(prompt)
        st.caption(
            f"mode=`{request_mode}` · "
            + (f"domain=`{base_request.get('domain','?')}` · " if request_mode == "pipeline" else "")
            + f"window {start[:19]} → {end[:19]}"
        )

    turn: dict = {
        "free_text":   prompt,
        "supervisor":  sup_key,
        "start":       start,
        "end":         end,
        "swarm":       None,
        "elapsed":     None,
    }

    live = st.container(border=True)
    with live:
        st.markdown(f"**🤖 [{s['label']}] 분석 진행 중…**")
        t0 = datetime.now(timezone.utc)

        if use_prev_context:
            ctx = _build_prev_context(sup_key)
            if ctx:
                base_request["fast_context"] = ctx

        sw_final = view_swarm.render_stream(agentcore_invoke_stream(base_request), request=base_request)
        turn["swarm"] = sw_final

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        turn["elapsed"] = elapsed
        st.caption(f"⏱ 총 {elapsed:.1f}s")

    st.session_state[h_key].append(turn)
    st.rerun()


# 처음 N개 = supervisor 탭
for tab, s in zip(chat_tabs[:len(SUPERVISORS)], SUPERVISORS):
    with tab:
        _render_supervisor_tab(s)

# 그 다음 = MCP 연결설정
with chat_tabs[len(SUPERVISORS)]:
    view_connections.render()

# (옵션) 마지막 = 시나리오 라이브 모니터 (testbed PoC 전용)
if SHOW_GENERATORS:
    with chat_tabs[-1]:
        view_generators.render(autorefresh_sec=int(os.environ.get("GEN_REFRESH_SEC", "5")))
