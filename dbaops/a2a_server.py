"""
a2a_server.py - DBAOps RCA 에이전트를 A2A 프로토콜로 직접 노출 (native, :9102).

기존 파사드(perf-agent/a2a_ops_server.py) 없이, DBAOps 그래프(invoke_single)를
a2a-sdk 서버로 직접 감싼다. 이 파일은 dbaops_agent 패키지를 import 만 하고
원본 코드는 수정하지 않는다.

perf-agent(:9100)와 이 서버(:9102)가 A2A로 직접 통신한다.

의존: mcp-router(:9000, GATEWAY_ENDPOINT) + Bedrock. dbaops-agent 서비스와 동일 요구.

Agent card: http://<host>:9102/.well-known/agent-card.json
Run:  python -m a2a_server   (또는 python a2a_server.py)
"""
from __future__ import annotations

import os

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message

from dbaops_agent.single_graph import invoke_single

HOST = os.environ.get("DBAOPS_A2A_HOST", "0.0.0.0")
PORT = int(os.environ.get("DBAOPS_A2A_PORT", "9102"))
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
        # invoke_single 은 동기(LLM/도구 blocking) → 스레드로 오프로드
        result = await asyncio.to_thread(invoke_single, request, recursion_limit=RECURSION_LIMIT)
        answer = _final_text(result)
        await event_queue.enqueue_event(new_agent_text_message(answer))

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
