#!/usr/bin/env bash
# DBAOps-A2A-DBPerf — 코드 업데이트 반영 (venv + systemd).
#   cd ~/DBAOps-A2A-DBPerf && git pull && bash deploy/update.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DBAOPS_DIR="$REPO_ROOT/dbaops"
PERF_DIR="$REPO_ROOT/perf-agent"
INSTALL_DIR="${DBAOPS_INSTALL_DIR:-/opt/dbaops}"
VENV="$INSTALL_DIR/venv"
DATA_DIR="$INSTALL_DIR/data"
RUN_USER="$(id -un)"

[ -d "$VENV" ] || { echo "!! venv 없음 — deploy/install.sh 먼저"; exit 1; }

echo "==> [1/2] DBAOps 갱신 위임"
bash "$REPO_ROOT/dbaops/deploy/ec2-vanilla/update.sh"

echo "==> [2/2] Perf + DBAOps A2A 갱신"
"$VENV/bin/pip" install -q -r "$PERF_DIR/requirements.txt"
"$VENV/bin/pip" install -q a2a-sdk uvicorn

# DBAOps native A2A 유닛
sed -e "s|__DBAOPS__|$DBAOPS_DIR|g" -e "s|__VENV__|$VENV|g" \
    -e "s|__DATA__|$DATA_DIR|g" -e "s|__USER__|$RUN_USER|g" \
    "$REPO_ROOT/deploy/systemd/dbaops-a2a.service" | \
  sudo tee /etc/systemd/system/dbaops-a2a.service >/dev/null
# Perf 유닛
for unit in dbperf-a2a dbperf-streamlit dbperf-slack-bot; do
  sed -e "s|__PERF__|$PERF_DIR|g" -e "s|__VENV__|$VENV|g" -e "s|__USER__|$RUN_USER|g" \
      "$REPO_ROOT/deploy/systemd/$unit.service" | \
    sudo tee /etc/systemd/system/$unit.service >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart dbaops-a2a dbperf-a2a dbperf-streamlit
systemctl is-enabled dbperf-slack-bot >/dev/null 2>&1 && sudo systemctl restart dbperf-slack-bot || true
echo "완료. curl -s http://localhost:9102/.well-known/agent-card.json"
