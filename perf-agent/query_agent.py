"""
query_agent.py - Query Performance agent (single-box EC2 deployment).

Spawns mcp_query_tools.py as a child process and talks to it over stdio MCP —
no AgentCore Gateway, no Lambda. Everything runs in this container/box.

build_mcp_client() / build_perf_agent() are reused by:
  - a2a_perf_server.py  (A2A server on :9100)
  - CLI interactive mode (python3.11 query_agent.py)

If ENABLE_A2A=1 (default), the agent also gets A2A client tools so it can
consult the DBAOps RCA agent at OPS_A2A_URL (:9102, native A2A).
"""
import os
import sys

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp import StdioServerParameters, stdio_client

AWS_REGION = os.environ.get('AWS_REGION', 'ap-northeast-2')
# perf 전용 모델(PERF_BEDROCK_MODEL_ID) 우선 — DBAOps의 BEDROCK_MODEL_ID(opus)와 분리.
BEDROCK_MODEL_ID = os.environ.get('PERF_BEDROCK_MODEL_ID') or os.environ.get(
    'BEDROCK_MODEL_ID', 'global.anthropic.claude-sonnet-4-5-20250929-v1:0')
OPS_A2A_URL = os.environ.get('OPS_A2A_URL', 'http://127.0.0.1:9102')
ENABLE_A2A = os.environ.get('ENABLE_A2A', '1') == '1'


def model_kwargs() -> dict:
    """BedrockModel 인자. temperature는 일부 신모델(opus-4-7 등)이 거부하므로
    BEDROCK_TEMPERATURE env가 있을 때만 넣는다(기본 미지정)."""
    kw = {"model_id": BEDROCK_MODEL_ID, "region_name": AWS_REGION}
    t = os.environ.get("BEDROCK_TEMPERATURE")
    if t:
        kw["temperature"] = float(t)
    return kw

SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_query_tools.py")

SYSTEM_PROMPT = """You are an RDS SQL Server query performance optimization specialist.

**CRITICAL: Check Query Store availability first**
- Always call check_query_store_enabled() at the start
- If enabled: Use Query Store tools for historical analysis
- If disabled: Use DMV tools for real-time analysis only

**Investigation workflow:**

1. **Check Query Store**: Call check_query_store_enabled() first
2. **If Query Store enabled**:
   - Use get_query_store_top_queries for historical top queries
   - Use get_query_store_regressed_queries to find performance degradation
   - Use get_query_execution_history for timeline analysis
   - Use get_query_store_wait_stats for wait analysis
3. **If Query Store disabled**:
   - Use get_expensive_queries_from_cache for queries since restart
   - Use get_slow_queries for currently running slow queries
   - Use get_blocking_sessions for lock contention
   - Explain limitation: "Query Store not enabled, showing real-time data only"
4. **For optimization**:
   - Use suggest_indexes for missing indexes
   - Use get_index_usage to find unused indexes
   - Use get_query_plan_from_cache for execution plans
5. **ONLY send Slack alerts when explicitly requested in the user's prompt**

**Collaborating with the DBAOps ops agent (via A2A):**
- You may have an ask_dbaops_agent(question) tool. The DBAOps ops agent analyzes
  OS/infra metrics, Aurora PostgreSQL, RDS MySQL, Kafka(MSK) and logs — systems
  OUTSIDE your SQL Server scope. The tool returns DBAOps' answer as plain text.
- When the user's question involves those systems, or asks to cross-check with the
  ops agent, call ask_dbaops_agent with a clear Korean question and quote its
  returned text in your report (cite it as coming from the DBAOps agent).
- Never forward SQL Server questions to it — that is your own job.

**Response format:**

## Query Store Status
- Enabled: [YES/NO]
- State: [READ_WRITE/READ_ONLY/OFF]

## Analysis Period
- Historical: [X hours/days] (Query Store)
- OR Real-time: Since last restart (DMVs)

## Top Resource-Consuming Queries
1. Query ID / Text: [...]
2. Metric: [CPU/Duration/IO]
3. Impact: [...]

## Performance Issues Detected
1. **Issue**: [Regression/Slow query/Blocking]
2. **Query**: [...]
3. **Impact**: [...]

## Optimization Recommendations
1. **Immediate**: [Specific action]
2. **Index recommendations**: [CREATE INDEX statements]
3. **Query rewrite**: [Suggestions]

## Action Items
1. **Critical**: [...]
2. **High**: [...]
3. **Medium**: [...]"""


def build_mcp_client() -> MCPClient:
    """stdio MCP 서버(mcp_query_tools.py)를 자식 프로세스로 스폰하는 클라이언트."""
    return MCPClient(lambda: stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=[SERVER_SCRIPT],
            env={**os.environ},
        )
    ))


def _flatten_a2a_text(result: dict) -> str:
    """strands a2a_send_message 결과(중첩 dict)에서 실제 답변 텍스트만 추출."""
    resp = result.get("response") or {}
    # (a) Task 형태: response.task.artifacts[].parts[].text
    task = resp.get("task") or {}
    texts: list[str] = []
    for art in (task.get("artifacts") or []):
        for part in (art.get("parts") or []):
            t = part.get("text") or (part.get("root") or {}).get("text")
            if t:
                texts.append(t)
    # (b) Message 형태: response.parts[].text
    if not texts:
        for part in (resp.get("parts") or []):
            t = part.get("text") or (part.get("root") or {}).get("text")
            if t:
                texts.append(t)
    return "\n".join(texts).strip() or "(DBAOps 응답에서 텍스트를 찾지 못함)"


def build_perf_agent(mcp_client: MCPClient, with_a2a: bool = ENABLE_A2A) -> Agent:
    """Query Performance 에이전트 생성. mcp_client는 열린 상태(context 진입)여야 함."""
    tools = list(mcp_client.list_tools_sync())

    if with_a2a:
        try:
            import asyncio
            from strands import tool
            from strands_tools.a2a_client import A2AClientToolProvider
            provider = A2AClientToolProvider(
                known_agent_urls=[OPS_A2A_URL],
                timeout=int(os.environ.get('A2A_CLIENT_TIMEOUT', '600')),
            )

            @tool
            def ask_dbaops_agent(question: str) -> str:
                """Ask the DBAOps RCA agent (OS/infra metrics, Aurora PostgreSQL, RDS MySQL,
                Kafka/MSK, logs) a question over A2A and return its answer as plain text.
                Use for anything OUTSIDE SQL Server query performance. Korean question works best."""
                result = asyncio.run(provider._send_message(question, OPS_A2A_URL))
                if result.get("status") != "success":
                    return f"[DBAOps A2A error] {result.get('error', result)}"
                return _flatten_a2a_text(result)

            tools.append(ask_dbaops_agent)
        except Exception as e:
            print(f"[warn] A2A client tool unavailable: {e}", file=sys.stderr)

    model = BedrockModel(**model_kwargs())
    return Agent(
        name="SQL Server Query Performance Agent",
        description=("RDS SQL Server query performance specialist: Query Store analysis, "
                     "regression detection, blocking sessions, execution plans, index tuning."),
        system_prompt=SYSTEM_PROMPT,
        model=model,
        tools=tools,
    )


def main():
    print("Starting Query Performance Agent (stdio MCP)...")
    mcp_client = build_mcp_client()

    with mcp_client:
        agent = build_perf_agent(mcp_client)
        print(f"Connected. tools: {len(agent.tool_names)}")
        for t in agent.tool_names:
            print(f"  - {t}")
        print()

        # One-shot mode: python3.11 query_agent.py "your question"
        if len(sys.argv) > 1:
            agent(" ".join(sys.argv[1:]))
            return

        # Interactive mode
        print("Query Performance Agent ready. Type 'quit' to exit.\n")
        while True:
            try:
                user_input = input("You> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break
            agent(user_input)
            print()


if __name__ == "__main__":
    main()
