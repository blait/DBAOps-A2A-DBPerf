"""
a2a_server.py - DBAOps RCA 에이전트를 A2A 프로토콜로 직접 노출 (native, :9102).

기존 파사드(perf-agent/a2a_ops_server.py) 없이, DBAOps 그래프(invoke_single)를
a2a-sdk 서버로 직접 감싼다. 이 파일은 dbaops_agent 패키지를 import 만 하고
원본 코드는 수정하지 않는다.

**상호(A2A 양방향)**: 이 프로세스 안에서 DBAOps 그래프에 `ask_perf_agent`
LangChain 도구를 주입(monkeypatch)해, DBAOps도 perf-agent(:9100)에 A2A로
질문할 수 있다. perf ⇄ dbaops 양쪽 모두 A2A 직결.

의존: mcp-router(:9000, GATEWAY_ENDPOINT) + Bedrock. dbaops-agent 서비스와 동일 요구.

Agent card: http://<host>:9102/.well-known/agent-card.json
Run:  python -m a2a_server   (또는 python a2a_server.py)
"""
from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import uvicorn
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentSkill, Message,
                       Part, Role, TextPart)
from langchain_core.tools import StructuredTool

from dbaops_agent import single_graph
from dbaops_agent.single_graph import invoke_single

HOST = os.environ.get("DBAOPS_A2A_HOST", "0.0.0.0")
PORT = int(os.environ.get("DBAOPS_A2A_PORT", "9102"))
PUBLIC_URL = os.environ.get("DBAOPS_A2A_URL", f"http://127.0.0.1:{PORT}/")
PERF_A2A_URL = os.environ.get("PERF_A2A_URL", "http://127.0.0.1:9100")
RECURSION_LIMIT = int(os.environ.get("SINGLE_RECURSION_LIMIT", "80"))
DEFAULT_WINDOW_HOURS = int(os.environ.get("A2A_DEFAULT_WINDOW_HOURS", "1"))
PERF_TIMEOUT = int(os.environ.get("A2A_CLIENT_TIMEOUT", "600"))


# ─────────────── dbaops → perf 방향: A2A client 도구 주입 ───────────────

async def _a2a_ask_perf(question: str) -> str:
    """perf A2A 서버(:9100)에 질문을 보내고 응답 텍스트를 평문으로 수집."""
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
        # 이 스레드에 이미 이벤트 루프가 돌고 있으면(asyncio.run 불가)
        # 새 스레드에서 실행. 보통은 워커 스레드라 asyncio.run 바로 가능.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_a2a_ask_perf(question))
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, _a2a_ask_perf(question)).result(timeout=PERF_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return f"[perf A2A error] {e}"


_PERF_TOOL = StructuredTool.from_function(
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

_PROMPT_APPENDIX = """

<peer_agent>
동료 에이전트: **SQL Server Query Performance Agent** (ask_perf_agent 도구, A2A).
- RDS **SQL Server** 쿼리 성능(Query Store, 느린 쿼리, 블로킹, 실행계획, 인덱스 추천)이
  필요하면 ask_perf_agent 로 한국어 질문을 보내고, 받은 답을 출처 표기와 함께 인용한다.
- SQL Server 이외(Aurora PG / RDS MySQL / MSK / OS 메트릭 / 로그)는 절대 위임하지
  않는다 — 그것은 너의 일이다.
</peer_agent>"""


def _install_perf_tool() -> None:
    """원본 파일 무수정으로 single_graph에 ask_perf_agent를 주입.
    _build_graph()가 모듈 전역에서 _all_tools/_build_system_prompt를 조회하므로
    (lazy, 첫 invoke 때), 여기서 모듈 속성만 바꿔치면 된다."""
    orig_tools = single_graph._all_tools
    orig_prompt = single_graph._build_system_prompt

    def all_tools_with_perf():
        tools = orig_tools()
        if all(getattr(t, "name", "") != "ask_perf_agent" for t in tools):
            tools.append(_PERF_TOOL)
        return tools

    def prompt_with_perf():
        return orig_prompt() + _PROMPT_APPENDIX

    single_graph._all_tools = all_tools_with_perf
    single_graph._build_system_prompt = prompt_with_perf


_install_perf_tool()


def _final_text(result: dict) -> str:
    """invoke_single 결과에서 마지막 AI 텍스트(도구호출 아닌) 추출."""
    if result.get("error"):
        return f"[DBAOps error] {result['error']}"
    messages = result.get("messages") or []
    for m in reversed(messages):
        if m.get("role") == "ai" and not (m.get("tool_calls") or []) and (m.get("text") or "").strip():
            return m["text"]
    # fallback: 마지막 텍스트 있는 메시지
    for m in reversed(messages):
        if (m.get("text") or "").strip():
            return m["text"]
    return "(DBAOps 응답 없음)"


def _build_request(user_text: str) -> dict:
    """A2A 사용자 발화 → DBAOps single 모드 request. 시간창은 최근 N시간 기본.
    Date.now류를 스크립트가 아닌 런타임에서 쓰므로 여기선 datetime 사용 가능."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    return {
        "mode": "single",
        "free_text": user_text,
        "time_range": {
            "start": (now - timedelta(hours=DEFAULT_WINDOW_HOURS)).isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
        },
        "session_id": "a2a",
    }


class DBAOpsExecutor(AgentExecutor):
    """A2A 요청을 DBAOps invoke_single 으로 처리."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        import asyncio
        user_text = context.get_user_input() or ""
        request = _build_request(user_text)

        # Task + artifact 로 응답 — strands A2A client 등 표준 클라이언트가 파싱 가능.
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()
        # invoke_single 은 동기(LLM/도구 blocking) → 스레드로 오프로드
        result = await asyncio.to_thread(invoke_single, request, recursion_limit=RECURSION_LIMIT)
        answer = _final_text(result)
        await updater.add_artifact([Part(root=TextPart(text=answer))], name="dbaops_rca_result")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 단발 동기 실행이라 취소 개념 없음 — no-op
        return


def build_agent_card() -> AgentCard:
    skill = AgentSkill(
        id="dbaops_rca",
        name="DBAOps RCA analysis",
        description=("OS/infra metrics (EC2/Prometheus), Aurora PostgreSQL, RDS MySQL, "
                     "Kafka(MSK), and log analysis. Root-cause investigation across "
                     "metrics and logs with cited findings."),
        tags=["database", "infrastructure", "rca", "metrics", "logs", "aurora", "mysql", "kafka"],
        examples=[
            "EC2 최근 1시간 CPU peak 시점과 baseline 대비 격차 분석",
            "Aurora PG 최근 1시간 deadlock / FATAL 빈도",
            "MySQL slow_log 최근 30분 TOP 5",
        ],
    )
    return AgentCard(
        name="DBAOps RCA Agent",
        description=("DB/infra RCA analyst: OS metrics, Aurora PostgreSQL, RDS MySQL, "
                     "Kafka(MSK), logs. Native A2A."),
        url=PUBLIC_URL,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[skill],
    )


def build_app() -> A2AStarletteApplication:
    handler = DefaultRequestHandler(
        agent_executor=DBAOpsExecutor(),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(agent_card=build_agent_card(), http_handler=handler)


def main():
    app = build_app().build()
    print(f"DBAOps native A2A server on {HOST}:{PORT} (card url: {PUBLIC_URL})")
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
