#!/usr/bin/env bash
# DBAOps-A2A-DBPerf — 통합 설치 (docker 없이 venv + systemd).
#
#   [1] DBAOps(vanilla) 4개 서비스 설치·기동  — dbaops/deploy/ec2-vanilla/install.sh 위임
#   [2] SQL Server Perf 3개 서비스 설치·기동   — 이 스크립트가 처리
#
# 대상 OS: Amazon Linux 2023 (dnf) 또는 Ubuntu 22.04+ (apt).
# 실행:  cd ~/DBAOps-A2A-DBPerf && bash deploy/install.sh
#
# 두 스택은 /etc/dbaops/dbaops.env 를 공유하고 전부 127.0.0.1 로 통신한다.
# 포트: dbaops mcp-router:9000 agent:8080 streamlit:8501 / perf a2a:9100 facade:9101 ui:8502
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DBAOPS_DIR="$REPO_ROOT/dbaops"
PERF_DIR="$REPO_ROOT/perf-agent"
INSTALL_DIR="${DBAOPS_INSTALL_DIR:-/opt/dbaops}"
VENV="$INSTALL_DIR/venv"
ENV_FILE="/etc/dbaops/dbaops.env"
UNIT_DIR="/etc/systemd/system"
RUN_USER="$(id -un)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  DBAOps-A2A-DBPerf 통합 설치 (venv + systemd, docker 없음) ║"
echo "╚══════════════════════════════════════════════════════════╝"

# ── [1] DBAOps vanilla 설치 (venv 생성 + 4개 유닛) ───────────────
echo ""
echo "==> [1/2] DBAOps(vanilla) 설치 위임 → dbaops/deploy/ec2-vanilla/install.sh"
bash "$DBAOPS_DIR/deploy/ec2-vanilla/install.sh"

# ── [2] Perf 에이전트 의존성 + systemd 유닛 ─────────────────────
echo ""
echo "==> [2/2] SQL Server Perf 에이전트 설치"
[ -d "$VENV" ] || { echo "!! venv 없음 — [1] 실패?"; exit 1; }

echo "    pip install (perf-agent/requirements.txt) — pymssql/strands/a2a/streamlit"
"$VENV/bin/pip" install -q -r "$PERF_DIR/requirements.txt"

echo "    pip install (DBAOps native A2A 서버용 a2a-sdk/uvicorn)"
"$VENV/bin/pip" install -q a2a-sdk uvicorn

echo "    perf env 기본값 추가 (없을 때만)"
if ! sudo grep -q '^DB_SECRET_ID=' "$ENV_FILE" 2>/dev/null; then
  sudo tee -a "$ENV_FILE" >/dev/null <<'PERFENV'

# ─── SQL Server Perf Agent ───
PERF_BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0
DB_SECRET_ID=dbops-sqlserver-secret
DB_NAME=master
# Slack 알림 기본 채널 (bot token 은 위 SLACK_BOT_TOKEN 재사용)
#SLACK_CHANNEL=#dbops-alerts
PERFENV
fi

echo "    systemd 유닛 설치 (dbaops-a2a / dbperf-a2a / dbperf-streamlit)"
# DBAOps native A2A (dbaops 리포 경로 + data + venv 치환)
DATA_DIR="$INSTALL_DIR/data"
sed -e "s|__DBAOPS__|$DBAOPS_DIR|g" \
    -e "s|__VENV__|$VENV|g" \
    -e "s|__DATA__|$DATA_DIR|g" \
    -e "s|__USER__|$RUN_USER|g" \
    "$REPO_ROOT/deploy/systemd/dbaops-a2a.service" | \
  sudo tee "$UNIT_DIR/dbaops-a2a.service" >/dev/null
# Perf 유닛
for unit in dbperf-a2a dbperf-streamlit; do
  sed -e "s|__PERF__|$PERF_DIR|g" \
      -e "s|__VENV__|$VENV|g" \
      -e "s|__USER__|$RUN_USER|g" \
      "$REPO_ROOT/deploy/systemd/$unit.service" | \
    sudo tee "$UNIT_DIR/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now dbaops-a2a dbperf-a2a dbperf-streamlit

echo ""
echo "완료. 확인:"
echo "  systemctl status dbaops-agent dbaops-a2a dbperf-a2a dbperf-streamlit --no-pager"
echo "  curl -s http://localhost:9100/.well-known/agent-card.json | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"name\"])'"
echo "  curl -s http://localhost:9102/.well-known/agent-card.json | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"name\"])'"
echo "  브라우저: http://<EC2-IP>:8501 (DBAOps)   http://<EC2-IP>:8502 (Perf)"
echo ""
echo "다음: /etc/dbaops/dbaops.env 에 DB 시크릿/Slack 채널 확인 후"
echo "      $VENV/bin/python $PERF_DIR/connections.py status"
