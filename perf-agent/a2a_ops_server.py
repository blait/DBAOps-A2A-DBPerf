"""
a2a_ops_server.py - DBAOps Agent(vanilla systemd)를 A2A 프로토콜로 노출하는 파사드 (:9101).

DBAOps-Agent DBAOps agent(vanilla systemd)는 127.0.0.1:8080/invocations HTTP만 말하고
A2A를 모른다. 이 파사드가 A2A 요청을 받아 HTTP(mode=single)로 변환해 주고,
응답의 최종 분석 텍스트를 돌려준다.

또한 파사드 에이전트는 perf A2A 서버(:9100) client 도구도 가져서, DBAOps 쪽 질문에
SQL Server 컨텍스트가 필요하면 반대 방향(ops → perf)으로도 물어볼 수 있다.

Agent card: http://<host>:9101/.well-known/agent-card.json

Run:  python3 a2a_ops_server.py
"""
import json
import os
import sys
import urllib.request
from typing import Any, Dict

from strands import Agent, tool
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer

from query_agent import model_kwargs

AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-2')

# DBAOps agent (같은 호스트면 http://127.0.0.1:8080/invocations)
DBAOPS_AGENT_URL = os.environ.get('DBAOPS_AGENT_URL', 'http://127.0.0.1:8080/invocations')
DBAOPS_TIMEOUT = int(os.environ.get('DBAOPS_TIMEOUT', '840'))

HOST = os.environ.get('A2A_OPS_HOST', '0.0.0.0')
PORT = int(os.environ.get('A2A_OPS_PORT', '9101'))
HTTP_URL = os.environ.get('A2A_OPS_URL', f"http://127.0.0.1:{PORT}")
PERF_A2A_URL = os.environ.get('PERF_A2A_URL', 'http://127.0.0.1:9100')


@tool
def ask_dbaops_agent(question: str, hours_back: int = 1) -> Dict[str, Any]:
    """Ask the DBAOps RCA analyst about OS/infra metrics,
    Aurora PostgreSQL, RDS MySQL, Kafka(MSK) or logs. Korean questions work best.
    hours_back sets the analysis time window (default: last 1 hour)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    request = {
        "mode": "single",
        "free_text": question,
        "time_range": {
            "start": (now - timedelta(hours=hours_back)).isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
        },
        "session_id": "a2a-facade",
    }
    try:
        req = urllib.request.Request(
            DBAOPS_AGENT_URL,
            data=json.dumps({"request": request}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=DBAOPS_TIMEOUT) as r:
            result = json.loads(r.read())

        if result.get("error"):
            return {"status": "error", "error": result["error"]}

        # single_graph 응답: {"swarm": {"messages": [...normalized...], ...}}
        messages = (result.get("swarm") or {}).get("messages") or []
        final_text = ""
        for m in reversed(messages):
            if m.get("role") == "ai" and not (m.get("tool_calls") or []) and (m.get("text") or "").strip():
                final_text = m["text"]
                break
        if not final_text:
            return {"status": "error", "error": "no final AI message in response",
                    "n_messages": len(messages)}
        return {"status": "success", "answer": final_text,
                "n_messages": len(messages),
                "time_range": request["time_range"]}
    except Exception as e:
        return {"status": "error", "error": str(e)[:500]}


SYSTEM_PROMPT = """You are the A2A facade for the **DBAOps ops agent** — an RCA analyst
covering OS/infra metrics (EC2/Prometheus), Aurora PostgreSQL, RDS MySQL, Kafka(MSK),
and log analysis.

- For any question in that scope, call ask_dbaops_agent(question, hours_back) and relay
  its answer faithfully. Do not invent findings the tool did not return. Extract a sensible
  hours_back from the question (e.g. "최근 6시간" → 6); default 1.
- If the question needs **SQL Server query performance** context (Query Store, blocking,
  index tuning on the SQL Server instance), use the a2a_send_message tool to ask the
  "SQL Server Query Performance Agent" and combine both answers.
- If ask_dbaops_agent returns an error, report the error honestly and suggest checking
  the DBAOps agent container status.
- Answer in the language the question was asked in (Korean in, Korean out)."""


def build_ops_facade_agent() -> Agent:
    tools: list = [ask_dbaops_agent]
    try:
        from strands_tools.a2a_client import A2AClientToolProvider
        provider = A2AClientToolProvider(
            known_agent_urls=[PERF_A2A_URL],
            timeout=int(os.environ.get('A2A_CLIENT_TIMEOUT', '600')),
        )
        tools += provider.tools
    except Exception as e:
        print(f"[warn] A2A client tools unavailable: {e}", file=sys.stderr)

    model = BedrockModel(**model_kwargs())
    return Agent(
        name="DBAOps Ops Agent (A2A facade)",
        description=("Facade for the DBAOps RCA analyst: OS/infra "
                     "metrics, Aurora PostgreSQL, RDS MySQL, Kafka(MSK), log analysis. "
                     "Can also consult the SQL Server Query Performance Agent over A2A."),
        system_prompt=SYSTEM_PROMPT,
        model=model,
        tools=tools,
    )


def main():
    agent = build_ops_facade_agent()
    server = A2AServer(
        agent=agent,
        host=HOST,
        port=PORT,
        http_url=HTTP_URL,
        serve_at_root=True,
        version="1.0.0",
    )
    print(f"DBAOps ops A2A facade on {HOST}:{PORT} (card url: {HTTP_URL}) → {DBAOPS_AGENT_URL}")
    server.serve()


if __name__ == "__main__":
    main()
