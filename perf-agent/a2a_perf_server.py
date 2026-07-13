"""
a2a_perf_server.py - Query Performance 에이전트를 A2A 프로토콜로 노출 (:9100).

DBAOps RCA agent(native A2A :9102)나 다른 A2A 클라이언트가 이 서버로
SQL Server 쿼리 성능 질문을 보낼 수 있다.

Agent card: http://<host>:9100/.well-known/agent-card.json

Run:  python3.11 a2a_perf_server.py
"""
import os

from a2a.types import AgentSkill
from strands.multiagent.a2a import A2AServer

from query_agent import build_mcp_client, build_perf_agent

HOST = os.environ.get('A2A_PERF_HOST', '0.0.0.0')
PORT = int(os.environ.get('A2A_PERF_PORT', '9100'))
# agent card에 실릴 접근 URL (peer가 접근할 주소)
HTTP_URL = os.environ.get('A2A_PERF_URL', f"http://127.0.0.1:{PORT}")

# 카드에 광고할 대표 스킬 — 내부 도구 14개를 그대로 노출하지 않고 큐레이션.
# (특히 ask_dbaops_agent 같은 peer 위임 도구는 외부에 광고하면 경유/루프 오해 소지)
CARD_SKILLS = [
    AgentSkill(
        id="sqlserver_query_performance",
        name="SQL Server query performance analysis",
        description=(
            "RDS SQL Server query performance diagnosis and tuning: Query Store "
            "historical analysis, regression detection, currently running slow "
            "queries, blocking sessions, execution plans, and missing/unused "
            "index recommendations with CREATE INDEX statements."
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


def main():
    mcp_client = build_mcp_client()
    with mcp_client:
        # 양방향 A2A: 이 서버의 에이전트도 DBAOps native A2A(:9102)에 물어볼 수 있다.
        # 무한 위임 루프는 시스템 프롬프트의 역할 경계로 방지 —
        # perf는 SQL Server 질문을 절대 위임하지 않고, ops는 SQL Server 컨텍스트만 위임한다.
        agent = build_perf_agent(mcp_client)
        server = A2AServer(
            agent=agent,
            host=HOST,
            port=PORT,
            http_url=HTTP_URL,
            serve_at_root=True,
            version="1.0.0",
            skills=CARD_SKILLS,
            # 스트리밍 응답을 A2A 스펙에 맞게 (Strands 기본값은 비준수 — 자체 경고 로그 있음)
            enable_a2a_compliant_streaming=True,
        )
        print(f"Query Performance A2A server on {HOST}:{PORT} (card url: {HTTP_URL})")
        server.serve()


if __name__ == "__main__":
    main()
