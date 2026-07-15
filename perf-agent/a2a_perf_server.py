"""
a2a_perf_server.py - LangGraph 기반 Query Performance 에이전트 서버 (:9100).

한 프로세스가 두 인터페이스를 서빙 (dbaops-agent와 동일 구조):
- **A2A** (a2a-sdk 표준) — peer 에이전트(dbaops)·외부 시스템용, Task+artifact 응답
- **POST /invocations** (NDJSON 스트리밍) — UI/Slack용 진행 이벤트 스트림
  (start/stage/message/validation/report/done — dbaops iter_single과 같은 모양)

- MCP stdio 세션(mcp_query_tools.py)은 서버 기동 시 열어 프로세스 수명 동안 상주
- A2A context_id / invocations session_id → LangGraph thread_id 매핑으로 대화 연속성 유지

Agent card: http://<host>:9100/.well-known/agent-card.json
Run:  python a2a_perf_server.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
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
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

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


# ───────────── POST /invocations — NDJSON 스트리밍 (UI/Slack용) ─────────────

async def _invocations(request: Request):
    """dbaops runtime_entry와 같은 규약: {"request":{...}} → NDJSON 이벤트 스트림.

    stream 미요청 시엔 최종 결과 JSON 하나 (dbaops handler와 동일한 동기 규약).
    """
    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        event = {}
    req = event.get("request") or {}
    user_text = req.get("free_text") or ""
    thread = f"inv:{req.get('session_id') or 'default'}"
    # mode 미지정/auto → 그래프의 route 노드(LLM)가 single/report 판정
    mode = (req.get("mode") or "").lower()
    if mode in ("auto", "pipeline"):
        mode = "" if mode == "auto" else "report"
    stream = (req.get("stream") is True
              or "ndjson" in (request.headers.get("accept") or "").lower())

    try:
        await asyncio.wait_for(_READY.wait(), timeout=30)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "perf graph not ready"}, status_code=503)

    if not stream:
        answer = await perf_graph.run_perf(_GRAPH, user_text, thread_id=thread, mode=mode)
        return JSONResponse({"result": answer, "request": req})

    async def gen():
        try:
            async for ev in perf_graph.iter_perf(_GRAPH, user_text, thread_id=thread, mode=mode):
                yield (json.dumps(ev, ensure_ascii=False, default=str) + "\n").encode()
        except Exception as e:  # noqa: BLE001
            logger.exception("invocations stream failed")
            yield (json.dumps({"type": "error", "error": str(e)}) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


async def _healthz(_request: Request):
    return JSONResponse({"status": "ok", "ready": _READY.is_set()})


def main():
    handler = DefaultRequestHandler(agent_executor=PerfExecutor(),
                                    task_store=InMemoryTaskStore())
    app = A2AStarletteApplication(agent_card=build_agent_card(),
                                  http_handler=handler).build(lifespan=_lifespan)
    # 같은 프로세스·같은 포트에 UI/Slack용 스트리밍 라우트 추가
    app.router.routes.append(Route("/invocations", _invocations, methods=["POST"]))
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))

    print(f"Query Performance server (A2A + /invocations) on {HOST}:{PORT} (card url: {PUBLIC_URL})")
    uvicorn.run(app, host=HOST, port=PORT,
                log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
