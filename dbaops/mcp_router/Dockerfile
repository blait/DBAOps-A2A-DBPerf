# MCP Router — AgentCore Gateway 대체. python(MCP 서버 5개) + node(mysql MCP) 를 한 컨테이너에.
FROM python:3.12-slim

# node 20 (community-mysql MCP 서버용) + postgres client (postgres-mcp libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg libpq5 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python MCP 서버 + router 의존성
COPY mcp_router/requirements.txt /app/mcp_router/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp_router/requirements.txt

# Node MCP 서버 (benborla mysql)
COPY mcp_tools/community_mysql/package.json /app/package.json
RUN npm install --omit=dev

# router 코드 + 커스텀 핸들러(직접 import) + tool_io schema
COPY mcp_router/ /app/mcp_router/
COPY mcp_tools/  /app/mcp_tools/

ENV DBAOPS_MCP_TOOLS_DIR=/app/mcp_tools \
    DBAOPS_DATA_DIR=/data \
    MYSQL_MCP_ENTRY=/app/node_modules/@benborla29/mcp-server-mysql/dist/index.js \
    MCP_ROUTER_PORT=9000 \
    AWS_REGION=ap-northeast-2 \
    AWS_DEFAULT_REGION=ap-northeast-2 \
    PYTHONPATH=/app

EXPOSE 9000
CMD ["python", "-m", "mcp_router.server"]
