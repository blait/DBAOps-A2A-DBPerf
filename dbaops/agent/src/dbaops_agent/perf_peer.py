"""perf_peer.py — 동료 Perf 에이전트(A2A :9100) 호출 도구.

DBAOps 에이전트가 SQL Server 쿼리 성능 질문을 Perf 에이전트에 위임할 때 쓴다.
single_graph 가 도구 목록과 시스템 프롬프트에 직접 포함하므로, HTTP(:8080)
경로(UI/Slack)와 A2A(:9102) 경로 모두 동일하게 위임이 동작한다.

ENABLE_PERF_PEER=0 으로 끌 수 있다 (기본 켬).
"""

from __future__ import annotations

import asyncio
import os
import uuid

from langchain_core.tools import StructuredTool

PERF_A2A_URL = os.environ.get("PERF_A2A_URL", "http://127.0.0.1:9100")
PERF_TIMEOUT = int(os.environ.get("A2A_CLIENT_TIMEOUT", "600"))
ENABLED = os.environ.get("ENABLE_PERF_PEER", "1") == "1"


async def _a2a_ask_perf(question: str) -> str:
    """perf A2A 서버(:9100)에 질문을 보내고 응답 텍스트를 평문으로 수집."""
    import httpx
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.types import Message, Part, Role, TextPart

    async with httpx.AsyncClient(timeout=PERF_TIMEOUT) as hc:
        card = await A2ACardResolver(httpx_client=hc, base_url=PERF_A2A_URL).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=hc, streaming=False)).create(card)
        msg = Message(role=Role.user, parts=[Part(TextPart(text=question))],
                      message_id=uuid.uuid4().hex)

        def texts(parts):
            out = []
            for p in (parts or []):
                r = getattr(p, "root", p)
                if getattr(r, "text", None):
                    out.append(r.text)
            return out

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
        return "\n".join(chunks).strip() or "(perf 에이전트 응답 없음)"


def _ask_perf_agent(question: str) -> str:
    try:
        # 이 스레드에 이벤트 루프가 이미 돌고 있으면 asyncio.run 불가 → 새 스레드.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_a2a_ask_perf(question))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _a2a_ask_perf(question)).result(timeout=PERF_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return f"[perf A2A error] {e}"


PERF_TOOL = StructuredTool.from_function(
    func=_ask_perf_agent,
    name="ask_perf_agent",
    description=(
        "Ask the SQL Server Query Performance Agent (peer agent over A2A) about "
        "RDS SQL Server query performance: Query Store, slow queries, blocking "
        "sessions, execution plans, index recommendations. Input: a clear question "
        "in Korean. Returns the peer agent's answer as plain text. Use ONLY for "
        "SQL Server topics — never for Aurora/MySQL/Kafka/OS metrics (those are yours)."
    ),
)

PROMPT_APPENDIX = """

<peer_agent>
동료 에이전트: **SQL Server Query Performance Agent** (ask_perf_agent 도구, A2A).
- RDS **SQL Server** 쿼리 성능(Query Store, 느린 쿼리, 블로킹, 실행계획, 인덱스 추천)이
  필요하면 ask_perf_agent 로 한국어 질문을 보내고, 받은 답을 출처 표기와 함께 인용한다.
- SQL Server 이외(Aurora PG / RDS MySQL / MSK / OS 메트릭 / 로그)는 절대 위임하지
  않는다 — 그것은 너의 일이다.
</peer_agent>"""
