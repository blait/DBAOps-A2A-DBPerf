"""
a2a_perf_server.py - LangGraph 기반 Query Performance 에이전트를 A2A로 노출 (:9100).

Strands A2AServer 대신 a2a-sdk 직접 구현 (dbaops/a2a_server.py와 동일 패턴).
- MCP stdio 세션(mcp_query_tools.py)은 서버 기동 시 열어 프로세스 수명 동안 상주
- A2A context_id → LangGraph thread_id 매핑으로 대화 연속성 유지
- Task + artifact 형태 응답 (peer/strands/사내 클라이언트 모두 파싱 가능)

Agent card: http://<host>:9100/.well-known/agent-card.json
Run:  python a2a_perf_server.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentSkill, Part, TextPart
from mcp import ClientSession, StdioServerParameters, stdio_client

import perf_graph

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("perf-a2a")

HOST = os.environ.get("A2A_PERF_HOST", "0.0.0.0")
PORT = int(os.environ.get("A2A_PERF_PORT", "9100"))
PUBLIC_URL = os.environ.get("A2A_PERF_URL", f"http://127.0.0.1:{PORT}/")

_GRAPH = None          # 기동 시 build_graph 결과
_READY = asyncio.Event()


class PerfExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.submit()
        await updater.start_work()
        try:
            await asyncio.wait_for(_READY.wait(), timeout=30)
            user_text = context.get_user_input() or ""
            thread = context.context_id or "default"
            answer = await perf_graph.run_perf(_GRAPH, user_text, thread_id=thread)
        except Exception as e:  # noqa: BLE001
            logger.exception("perf graph failed")
            answer = f"[perf error] {e}"
        await updater.add_artifact([Part(root=TextPart(text=answer))], name="perf_result")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return


CARD_SKILLS = [
    AgentSkill(
        id="sqlserver_query_performance",
        name="SQL Server query performance analysis",
        description=(
            "RDS SQL Server query performance diagnosis and tuning: Query Store "
            "historical analysis, regression detection, currently running slow "
            "queries, blocking sessions, execution plans, and missing/unused "
            "index recommendations with CREATE INDEX statements. "
            "LangGraph pipeline: analyze → validate → (revise) → report."
        ),
        tags=["sqlserver", "rds", "query-store", "dmv", "performance", "index-tuning", "blocking"],
        examples=[
            "지난 24시간 CPU 상위 쿼리 5개와 개선 방법",
            "블로킹 세션 확인해줘",
            "Orders 테이블에 필요한 인덱스 추천해줘",
            "어제부터 느려진 쿼리(회귀) 찾아줘",
        ],
    ),
]


def build_agent_card() -> AgentCard:
    return AgentCard(
        name="SQL Server Query Performance Agent",
        description=("RDS SQL Server query performance specialist: Query Store analysis, "
                     "regression detection, blocking sessions, execution plans, index "
                     "tuning. LangGraph-based (analyze→validate→report)."),
        url=PUBLIC_URL,
        version="2.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=CARD_SKILLS,
    )


async def _mcp_session_keeper() -> None:
    """MCP stdio 세션을 한 태스크 안에서 열고 유지 — anyio cancel scope는
    같은 태스크에서 enter/exit 해야 하므로, 이 keeper 태스크가 수명을 관리한다."""
    global _GRAPH
    params = StdioServerParameters(command=sys.executable,
                                   args=[perf_graph.SERVER_SCRIPT],
                                   env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await perf_graph.load_perf_tools(session)
            _GRAPH = perf_graph.build_graph(tools)
            _READY.set()
            logger.info("perf graph ready — %d tools (validation=%s)",
                        len(tools), perf_graph.ENABLE_VALIDATION)
            # 서버 수명 동안 세션 유지
            await asyncio.Event().wait()


@contextlib.asynccontextmanager
async def _lifespan(app):  # noqa: ANN001
    keeper = asyncio.get_running_loop().create_task(_mcp_session_keeper())
    yield
    keeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await keeper


def main():
    handler = DefaultRequestHandler(agent_executor=PerfExecutor(),
                                    task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=build_agent_card(),
                                  http_handler=handler).build(lifespan=_lifespan)

    print(f"Query Performance A2A server (LangGraph) on {HOST}:{PORT} (card url: {PUBLIC_URL})")
    uvicorn.run(app, host=HOST, port=PORT,
                log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
