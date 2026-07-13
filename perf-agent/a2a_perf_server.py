"""
a2a_perf_server.py - Query Performance 에이전트를 A2A 프로토콜로 노출 (:9100).

DBAOps ops agent(파사드 :9101)나 다른 A2A 클라이언트가 이 서버로
SQL Server 쿼리 성능 질문을 보낼 수 있다.

Agent card: http://<host>:9100/.well-known/agent-card.json

Run:  python3.11 a2a_perf_server.py
"""
import os

from strands.multiagent.a2a import A2AServer

from query_agent import build_mcp_client, build_perf_agent

HOST = os.environ.get('A2A_PERF_HOST', '0.0.0.0')
PORT = int(os.environ.get('A2A_PERF_PORT', '9100'))
# agent card에 실릴 접근 URL (peer가 접근할 주소)
HTTP_URL = os.environ.get('A2A_PERF_URL', f"http://127.0.0.1:{PORT}")


def main():
    mcp_client = build_mcp_client()
    with mcp_client:
        # 양방향 A2A: 이 서버의 에이전트도 ops 파사드(:9101)에 물어볼 수 있다.
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
        )
        print(f"Query Performance A2A server on {HOST}:{PORT} (card url: {HTTP_URL})")
        server.serve()


if __name__ == "__main__":
    main()
