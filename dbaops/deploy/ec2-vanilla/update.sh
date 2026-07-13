#!/usr/bin/env bash
# DBAOps-Agent 생 EC2 배포 — 코드 업데이트 반영.
#   cd ~/dbaops && git pull && bash deploy/ec2-vanilla/update.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_DIR="${DBAOPS_INSTALL_DIR:-/opt/dbaops}"
VENV="$INSTALL_DIR/venv"

[ -d "$VENV" ] || { echo "!! venv 없음 — install.sh 먼저 실행"; exit 1; }

echo "==> pip 의존성 갱신"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/mcp_router/requirements.txt"
"$VENV/bin/pip" install -q -e "$REPO_ROOT/agent"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/ui/streamlit/requirements.txt"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/slack_bot/requirements.txt"

echo "==> npm 의존성 갱신 (mysql MCP)"
cp "$REPO_ROOT/mcp_tools/community_mysql/package.json" "$INSTALL_DIR/mysql-mcp/"
( cd "$INSTALL_DIR/mysql-mcp" && npm install --omit=dev --silent )

echo "==> systemd 유닛 갱신 + 재시작"
RUN_USER="$(stat -c '%U' "$VENV" 2>/dev/null || id -un)"
for unit in dbaops-mcp-router dbaops-agent dbaops-streamlit dbaops-slack-bot; do
  sed -e "s|__REPO__|$REPO_ROOT|g" \
      -e "s|__VENV__|$VENV|g" \
      -e "s|__DATA__|$INSTALL_DIR/data|g" \
      -e "s|__MYSQL_MCP__|$INSTALL_DIR/mysql-mcp|g" \
      -e "s|__USER__|$RUN_USER|g" \
      "$REPO_ROOT/deploy/ec2-vanilla/systemd/$unit.service" | \
    sudo tee /etc/systemd/system/$unit.service >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart dbaops-mcp-router dbaops-agent dbaops-streamlit
systemctl is-enabled dbaops-slack-bot >/dev/null 2>&1 && \
  sudo systemctl restart dbaops-slack-bot || true

echo "완료. 확인: curl -s http://localhost:9000/healthz"
