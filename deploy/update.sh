#!/usr/bin/env bash
# DBAOps-A2A-DBPerf — 코드 업데이트 반영 (venv + systemd).
#   cd ~/DBAOps-A2A-DBPerf && git pull && bash deploy/update.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PERF_DIR="$REPO_ROOT/perf-agent"
INSTALL_DIR="${DBAOPS_INSTALL_DIR:-/opt/dbaops}"
VENV="$INSTALL_DIR/venv"
RUN_USER="$(id -un)"

[ -d "$VENV" ] || { echo "!! venv 없음 — deploy/install.sh 먼저"; exit 1; }

echo "==> [1/2] DBAOps 갱신 위임"
bash "$REPO_ROOT/dbaops/deploy/ec2-vanilla/update.sh"

echo "==> [2/2] Perf 갱신"
"$VENV/bin/pip" install -q -r "$PERF_DIR/requirements.txt"
for unit in dbperf-a2a dbperf-ops-facade dbperf-streamlit; do
  sed -e "s|__PERF__|$PERF_DIR|g" -e "s|__VENV__|$VENV|g" -e "s|__USER__|$RUN_USER|g" \
      "$REPO_ROOT/deploy/systemd/$unit.service" | \
    sudo tee /etc/systemd/system/$unit.service >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart dbperf-a2a dbperf-ops-facade dbperf-streamlit
echo "완료. curl -s http://localhost:9100/.well-known/agent-card.json"
