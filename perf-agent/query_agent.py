"""
query_agent.py - Query Performance 에이전트 CLI (LangGraph 기반).

stdio MCP 서버(mcp_query_tools.py)를 자식 프로세스로 열고, perf_graph의
analyze→validate→(revise)→report 파이프라인으로 질문을 처리한다.

사용:
  python query_agent.py                       # 대화형
  python query_agent.py "질문 한 번에"          # 원샷

서버(A2A :9100)는 a2a_perf_server.py — 같은 perf_graph를 공유한다.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

import perf_graph


async def _amain() -> None:
    params = StdioServerParameters(command=sys.executable,
                                   args=[perf_graph.SERVER_SCRIPT],
                                   env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await perf_graph.load_perf_tools(session)
            graph = perf_graph.build_graph(tools)
            print(f"perf graph ready — tools: {[t.name for t in tools]}")
            print(f"pipeline: analyze → validate → (revise) → report "
                  f"(validation={'on' if perf_graph.ENABLE_VALIDATION else 'off'})\n")

            # 원샷
            if len(sys.argv) > 1:
                q = " ".join(sys.argv[1:])
                print(await perf_graph.run_perf(graph, q, thread_id="cli"))
                return

            # 대화형
            print("Query Performance Agent (LangGraph). 'quit'으로 종료.\n")
            while True:
                try:
                    q = input("You> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nBye!")
                    break
                if not q:
                    continue
                if q.lower() in ("quit", "exit", "q"):
                    print("Bye!")
                    break
                print(await perf_graph.run_perf(graph, q, thread_id="cli"))
                print()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
