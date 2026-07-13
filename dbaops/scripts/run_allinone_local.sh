#!/usr/bin/env bash
# 올인원 로컬/대안 실행 — docker 없이 4 프로세스를 venv 로 직접 띄운다.
# (docker 가 안 되는 EC2 / 로컬 개발용. 운영 권장은 deploy/ec2-allinone/docker-compose.yml)
#
# 사전: python3.12, node20, postgres client(libpq) 설치 필요.
# 사용: BEDROCK_MODEL_ID=... AWS_REGION=ap-northeast-2 bash scripts/run_allinone_local.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${DBAOPS_VENV:-/tmp/dbaops_allinone_venv}"
export DBAOPS_DATA_DIR="${DBAOPS_DATA_DIR:-$ROOT/.allinone_data}"
export AWS_REGION="${AWS_REGION:-ap-northeast-2}"
export BEDROCK_REGION="$AWS_REGION"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-global.anthropic.claude-opus-4-7}"
export MCP_ROUTER_PORT="${MCP_ROUTER_PORT:-9000}"
export GATEWAY_ENDPOINT="http://127.0.0.1:${MCP_ROUTER_PORT}/mcp"
export AGENT_HTTP_URL="http://127.0.0.1:8080/invocations"
export MCP_ROUTER_HEALTH_URL="http://127.0.0.1:${MCP_ROUTER_PORT}/healthz"
export PYTHONPATH="$ROOT:$ROOT/agent/src:$ROOT/ui/streamlit"

mkdir -p "$DBAOPS_DATA_DIR"
[ -f "$DBAOPS_DATA_DIR/connections.json" ] || \
  cp "$ROOT/deploy/ec2-allinone/connections.example.json" "$DBAOPS_DATA_DIR/connections.json"

echo "==> venv at $VENV"
[ -d "$VENV" ] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -q --upgrade pip
echo "==> installing deps (mcp-router + agent + ui + slack)"
pip install -q -r "$ROOT/mcp_router/requirements.txt"
pip install -q -e "$ROOT/agent"
pip install -q -r "$ROOT/ui/streamlit/requirements.txt"
pip install -q -r "$ROOT/slack_bot/requirements.txt"

PIDS=()
cleanup() { echo "stopping…"; for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

echo "==> mcp-router :$MCP_ROUTER_PORT"
( cd "$ROOT" && python -m mcp_router.server ) & PIDS+=($!)
sleep 2

echo "==> agent :8080"
( cd "$ROOT/agent" && python -m dbaops_agent.runtime_entry ) & PIDS+=($!)
sleep 2

if [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
  echo "==> slack-bot"
  ( cd "$ROOT/slack_bot" && PYTHONPATH="$ROOT/ui/streamlit:$PYTHONPATH" python bot.py ) & PIDS+=($!)
fi

echo "==> streamlit :8501  (http://localhost:8501)"
( cd "$ROOT/ui/streamlit" && streamlit run app.py --server.port=8501 --server.address=0.0.0.0 ) & PIDS+=($!)

echo "==> all up. Ctrl-C to stop."
wait
