"""
a2a_server.py - DBAOps RCA 에이전트 통합 서버 (:8080, 포트 하나).

perf 서버(a2a_perf_server.py)와 동일 구조 — Starlette 앱 하나가 한 포트에서:
- **A2A** (a2a-sdk 표준) — peer 에이전트(perf)·외부 시스템용, Task+artifact 응답
- **POST /invocations** — UI/Slack용. NDJSON 스트리밍(진행 이벤트) + 동기 JSON 둘 다
  기존 runtime_entry 규약 그대로 (pipeline/single 모드, /ping·/healthz 포함)

Agent card: http://<host>:8080/.well-known/agent-card.json
Run:  python a2a_server.py   (systemd dbaops-agent 유닛의 ExecStart)
"""
from __future__ import annotations

import json
import logging
import os

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentSkill,
                       Part, TextPart)
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from dbaops_agent.single_graph import invoke_single

logger = logging.getLogger(__name__)

HOST = os.environ.get("DBAOPS_A2A_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("DBAOPS_A2A_PORT", "8080")))
PUBLIC_URL = os.environ.get("DBAOPS_A2A_URL", f"http://127.0.0.1:{PORT}/")
RECURSION_LIMIT = int(os.environ.get("SINGLE_RECURSION_LIMIT", "80"))
DEFAULT_WINDOW_HOURS = int(os.environ.get("A2A_DEFAULT_WINDOW_HOURS", "1"))


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


# ───────────── /invocations — UI/Slack용 (runtime_entry 규약 그대로) ─────────────

async def _invocations(request: Request):
    """기존 runtime_entry의 POST /invocations 와 동일 규약:
    {"request": {...}} → stream=true/Accept:ndjson 이면 NDJSON 이벤트 스트림,
    아니면 동기 JSON 하나. pipeline/single 모드 모두 지원."""
    import anyio
    from dbaops_agent.runtime_entry import handler as sync_handler

    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        event = {}
    req = event.get("request") or {}
    stream = (req.get("stream") is True
              or "ndjson" in (request.headers.get("accept") or "").lower())

    # mode=auto(또는 미지정): LLM 라우터가 보고서 요청인지 판정해 single/pipeline 선택
    mode = (req.get("mode") or "auto").lower()
    if mode == "auto":
        from dbaops_agent.router import decide
        mode, domain = await anyio.to_thread.run_sync(
            lambda: decide(req.get("free_text") or ""))
        req = {**req, "mode": mode}
        if domain and not req.get("domain"):
            req["domain"] = domain
        event = {**event, "request": req}

    if not stream:
        result = await anyio.to_thread.run_sync(lambda: sync_handler(event))
        return JSONResponse(result)

    def _sync_gen():
        if mode == "swarm":
            yield {"type": "error", "error": "mode=swarm has been removed; use mode=pipeline."}
            return
        if mode == "single":
            from dbaops_agent.single_graph import iter_single
            yield from iter_single(req, recursion_limit=RECURSION_LIMIT)
        else:
            from dbaops_agent.pipeline_graph import iter_pipeline
            yield from iter_pipeline(req)

    async def gen():
        # 동기 제너레이터(LLM/도구 blocking)를 스레드에서 한 스텝씩 당겨온다
        it = _sync_gen()
        while True:
            try:
                ev = await anyio.to_thread.run_sync(lambda: next(it, None))
            except Exception as e:  # noqa: BLE001
                logger.exception("invocations stream failed")
                yield (json.dumps({"type": "error", "error": str(e)}) + "\n").encode()
                return
            if ev is None:
                return
            yield (json.dumps(ev, ensure_ascii=False, default=str) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson")


async def _healthz(_request: Request):
    return JSONResponse({"status": "ok"})


def main():
    app = build_app().build()
    # UI/Slack용 라우트를 같은 앱·같은 포트에 추가
    app.router.routes.append(Route("/invocations", _invocations, methods=["POST"]))
    app.router.routes.append(Route("/invoke", _invocations, methods=["POST"]))
    app.router.routes.append(Route("/ping", _healthz, methods=["GET"]))
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))
    print(f"DBAOps server (A2A + /invocations) on {HOST}:{PORT} (card url: {PUBLIC_URL})")
    uvicorn.run(app, host=HOST, port=PORT, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
