# Schemas

JSON Schema 정의. Agent ↔ UI ↔ MCP 간 계약.

- `analysis_request.json` — Streamlit → agent 컨테이너(:8080/invocations) 입력
- `analysis_report.json`  — agent 컨테이너(:8080/invocations) → Streamlit 출력

각 MCP 도구의 I/O 스키마는 `mcp_tools/*/tool_io.json` 참조.
