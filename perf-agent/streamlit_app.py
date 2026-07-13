"""
streamlit_app.py - Query Performance Agent Streamlit UI (DBAOps-Agent 스타일).

탭 3개:
  ⚡ Query Performance — 우리 perf 에이전트와 채팅 (A2A :9100 경유)
  🧭 DBAOps Ops Agent  — DBAOps RCA 에이전트와 채팅 (native A2A :9102)
  🔌 연동 관리          — DB/Slack/A2A/Runtime 상태 확인, Slack 등록·테스트

두 채팅 탭 모두 A2A 서버를 통해 호출하므로, 에이전트 간(A2A) 경로와
사람이 쓰는 경로가 완전히 동일하다.

Run:  streamlit run streamlit_app.py --server.port 8502
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import streamlit as st

from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart

import connections

st.set_page_config(page_title="SQL Server DBOps", layout="wide")
st.title("SQL Server DBOps — Query Performance × DBAOps")
st.caption("stdio MCP 도구 13개 + native A2A 연동 (perf :9100 ↔ DBAOps :9102)")

AGENTS = [
    {
        "key": "perf",
        "tab": "⚡ Query Performance",
        "url": connections.PERF_A2A_URL,
        "desc": "RDS SQL Server 쿼리 성능 — Query Store / DMV / 인덱스 튜닝. "
                "OS·타DB 질문이 섞이면 스스로 ops 에이전트에 A2A로 물어봄.",
        "example": "예: '지난 24시간 CPU 상위 쿼리 5개와 개선 방법'",
    },
    {
        "key": "ops",
        "tab": "🧭 DBAOps Ops Agent",
        "url": connections.OPS_A2A_URL,
        "desc": "OS·인프라 메트릭 / Aurora PG / RDS MySQL / Kafka / 로그 RCA — "
                "DBAOps RCA 에이전트(native A2A)와 직접 통신.",
        "example": "예: 'EC2 최근 1시간 CPU peak 시점과 baseline 대비 격차'",
    },
]

for a in AGENTS:
    st.session_state.setdefault(f"history__{a['key']}", [])
    st.session_state.setdefault(f"ctx__{a['key']}", str(uuid.uuid4()))


# ───────────────────────── A2A 호출 ─────────────────────────

async def _a2a_ask(base_url: str, text: str, context_id: str) -> str:
    async with httpx.AsyncClient(timeout=900) as hc:
        card = await A2ACardResolver(httpx_client=hc, base_url=base_url).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=hc, streaming=False)).create(card)
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text=text))],
            message_id=uuid.uuid4().hex,
            context_id=context_id,
        )
        def _texts_from_parts(parts):
            out = []
            for part in (parts or []):
                root = getattr(part, "root", part)
                if getattr(root, "text", None):
                    out.append(root.text)
            return out

        chunks: list[str] = []
        async for event in client.send_message(msg):
            # 비스트리밍 응답은 서버 구현에 따라 세 형태로 옴:
            #  (a) Message 객체 (DBAOps native: new_agent_text_message)
            #  (b) (Task, update) 튜플 — artifacts 또는 status.message 에 텍스트
            if isinstance(event, tuple):
                task = event[0]
                for artifact in (getattr(task, "artifacts", None) or []):
                    chunks += _texts_from_parts(getattr(artifact, "parts", None))
                status = getattr(task, "status", None)
                if status and getattr(status, "message", None):
                    chunks += _texts_from_parts(getattr(status.message, "parts", None))
            else:
                chunks += _texts_from_parts(getattr(event, "parts", None))
        return "\n".join(chunks) or "(빈 응답)"


def a2a_ask(base_url: str, text: str, context_id: str) -> str:
    return asyncio.run(_a2a_ask(base_url, text, context_id))


# ───────────────────────── 채팅 탭 ─────────────────────────

def render_chat_tab(a: dict) -> None:
    h_key = f"history__{a['key']}"

    with st.container(border=True):
        st.markdown(f"### {a['tab']}")
        st.markdown(a["desc"])
        st.caption(a["example"])
        st.caption(f"A2A endpoint: `{a['url']}`")

    for turn in st.session_state[h_key]:
        with st.chat_message("user", avatar="🙋"):
            st.markdown(turn["q"])
        with st.chat_message("assistant", avatar="🤖"):
            st.markdown(turn["a"])
            if turn.get("elapsed"):
                st.caption(f"⏱ {turn['elapsed']:.1f}s")

    prompt = st.chat_input(f"질문 입력 — {a['tab']}", key=f"input__{a['key']}")
    if not prompt:
        return

    with st.chat_message("user", avatar="🙋"):
        st.markdown(prompt)
    with st.chat_message("assistant", avatar="🤖"):
        import time
        t0 = time.time()
        with st.spinner("분석 중…(도구 호출 포함 수 분 걸릴 수 있음)"):
            try:
                answer = a2a_ask(a["url"], prompt, st.session_state[f"ctx__{a['key']}"])
            except Exception as e:
                answer = f"❌ A2A 호출 실패: {e}\n\n서버 기동 여부를 연동 관리 탭에서 확인하세요."
        elapsed = time.time() - t0
        st.markdown(answer)
        st.caption(f"⏱ {elapsed:.1f}s")

    st.session_state[h_key].append({"q": prompt, "a": answer, "elapsed": elapsed})


# ───────────────────────── 연동 관리 탭 ─────────────────────────

def render_connections_tab() -> None:
    st.markdown("### 🔌 연동 서비스 상태")
    st.caption("이 스택이 연결되는 모든 외부 서비스의 연결 상태를 한눈에 확인합니다.")

    if st.button("🔄 전체 상태 새로고침", use_container_width=False):
        st.session_state.pop("conn_status", None)

    if "conn_status" not in st.session_state:
        with st.spinner("연동 상태 확인 중…"):
            st.session_state["conn_status"] = connections.check_all()

    labels = {
        "db_sqlserver": ("🗄️ RDS SQL Server", "Secrets Manager 자격증명으로 직접 접속"),
        "slack": ("💬 Slack", "Bot Token (chat.postMessage)"),
        "dbaops_agent": ("🧭 DBAOps Agent", "DBAOps agent (127.0.0.1:8080)"),
        "a2a_performance_agent": ("🔗 A2A — Perf Agent :9100", "우리 쿼리 성능 에이전트"),
        "a2a_dbaops_facade": ("🔗 A2A — DBAOps :9102", "DBAOps native A2A 서버"),
    }
    for key, status in st.session_state["conn_status"].items():
        label, hint = labels.get(key, (key, ""))
        with st.container(border=True):
            col1, col2 = st.columns([1, 4])
            col1.markdown("✅ 연결됨" if status.get("ok") else "❌ 안 됨")
            col2.markdown(f"**{label}** — {hint}")
            col2.caption(str(status.get("detail", "")))
            if status.get("skills"):
                col2.caption("skills: " + ", ".join(status["skills"]))

    st.divider()
    st.markdown("### 💬 Slack 연동")
    st.caption("Bot Token(xoxb-…) 방식 — DBAOps slack-bot과 같은 토큰을 재사용합니다. "
               "`.env`의 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` 로 설정하세요.")
    test_channel = st.text_input("테스트 채널 (비우면 SLACK_CHANNEL 사용)",
                                 placeholder="#dbops-alerts 또는 C0123ABCDEF")
    if st.button("테스트 메시지 발송"):
        result = connections.send_slack("Streamlit 연동 관리 탭에서 보낸 테스트 메시지입니다.",
                                        "INFO", channel=test_channel)
        (st.success if result.get("status") == "success" else st.error)(str(result))


# ───────────────────────── 레이아웃 ─────────────────────────

tabs = st.tabs([a["tab"] for a in AGENTS] + ["🔌 연동 관리"])
for tab, a in zip(tabs[:-1], AGENTS):
    with tab:
        render_chat_tab(a)
with tabs[-1]:
    render_connections_tab()

with st.sidebar:
    st.markdown("### 세션")
    for a in AGENTS:
        ctx_short = st.session_state[f"ctx__{a['key']}"][:8]
        st.caption(f"{a['tab']}: `{ctx_short}`")
    if st.button("🗑 대화 초기화", use_container_width=True):
        for a in AGENTS:
            st.session_state[f"history__{a['key']}"] = []
            st.session_state[f"ctx__{a['key']}"] = str(uuid.uuid4())
        st.rerun()
    st.divider()
    st.markdown("### 아키텍처")
    st.code(
        "Streamlit :8502\n"
        "  ├─ A2A → perf agent :9100\n"
        "  │         └─ stdio MCP (13 tools)\n"
        "  │         └─ A2A → DBAOps :9102\n"
        "  └─ A2A → DBAOps :9102 (native)\n"
        "            └─ single_graph → MCP router :9000\n"
        "            └─ A2A → perf agent :9100",
        language=None,
    )
