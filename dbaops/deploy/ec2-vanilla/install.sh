#!/usr/bin/env bash
# DBAOps-Agent — 생(vanilla) EC2 설치 스크립트. docker 없이 venv + systemd 로 구동.
#
# 대상 OS: Amazon Linux 2023 (dnf). Ubuntu 22.04+ 는 apt 분기로 지원.
# 실행:   sudo 필요 없음 — 내부에서 sudo 를 호출한다 (패키지 설치/유닛 등록).
#   cd ~/dbaops/deploy/ec2-vanilla && bash install.sh
#
# 하는 일:
#   1. python3.12 / nodejs20 / libpq / 한글 폰트 설치 (dnf 또는 apt)
#   2. /opt/dbaops/venv 생성 + 4개 서비스 의존성 설치
#   3. mysql MCP 서버(node) npm install
#   4. /etc/dbaops/dbaops.env 생성 (없을 때만 — .env 값 프롬프트 없이 example 복사)
#   5. systemd 유닛 4개 설치·활성화: dbaops-mcp-router / dbaops-agent /
#      dbaops-streamlit / dbaops-slack-bot
#
# 재실행해도 안전(멱등). 코드 업데이트 후에는 update.sh 를 쓰면 된다.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_DIR="${DBAOPS_INSTALL_DIR:-/opt/dbaops}"
VENV="$INSTALL_DIR/venv"
DATA_DIR="$INSTALL_DIR/data"
ENV_FILE="/etc/dbaops/dbaops.env"
UNIT_DIR="/etc/systemd/system"
PYTHON_BIN=python3.12

echo "==> DBAOps vanilla install — repo: $REPO_ROOT → $INSTALL_DIR"

# ── 1. OS 패키지 ─────────────────────────────────────────────
if command -v dnf >/dev/null 2>&1; then
  echo "==> dnf: python3.12 nodejs20 libpq fontconfig noto-cjk"
  sudo dnf install -y -q python3.12 python3.12-pip nodejs20 libpq \
      fontconfig google-noto-sans-cjk-ttc-fonts git
  # nodejs20 은 /usr/bin/node-20 으로 설치될 수 있음 — node 심볼릭 보장
  if ! command -v node >/dev/null 2>&1 && [ -x /usr/bin/node-20 ]; then
    sudo alternatives --install /usr/bin/node node /usr/bin/node-20 20 || \
      sudo ln -sf /usr/bin/node-20 /usr/local/bin/node
  fi
  if ! command -v npm >/dev/null 2>&1 && [ -x /usr/bin/npm-20 ]; then
    sudo ln -sf /usr/bin/npm-20 /usr/local/bin/npm
  fi
elif command -v apt-get >/dev/null 2>&1; then
  echo "==> apt: python3.12 nodejs libpq fonts-noto-cjk"
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.12 python3.12-venv python3-pip \
      libpq5 fontconfig fonts-noto-cjk git curl
  if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
    sudo apt-get install -y -qq nodejs
  fi
else
  echo "!! 지원하지 않는 OS (dnf/apt 없음)"; exit 1
fi
command -v "$PYTHON_BIN" >/dev/null || { echo "!! $PYTHON_BIN 설치 실패"; exit 1; }
command -v node >/dev/null || { echo "!! node 설치 실패"; exit 1; }
echo "    python: $($PYTHON_BIN --version) / node: $(node --version)"

# ── 2. venv + python 의존성 ──────────────────────────────────
echo "==> venv: $VENV"
sudo mkdir -p "$INSTALL_DIR" "$DATA_DIR"
sudo chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
[ -d "$VENV" ] || "$PYTHON_BIN" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
echo "==> pip install (mcp-router + agent + streamlit + slack-bot) — 수 분 소요"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/mcp_router/requirements.txt"
"$VENV/bin/pip" install -q -e "$REPO_ROOT/agent"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/ui/streamlit/requirements.txt"
"$VENV/bin/pip" install -q -r "$REPO_ROOT/slack_bot/requirements.txt"

# ── 3. mysql MCP (node) ─────────────────────────────────────
echo "==> npm install (@benborla29/mcp-server-mysql)"
mkdir -p "$INSTALL_DIR/mysql-mcp"
cp "$REPO_ROOT/mcp_tools/community_mysql/package.json" "$INSTALL_DIR/mysql-mcp/"
( cd "$INSTALL_DIR/mysql-mcp" && npm install --omit=dev --silent )

# ── 4. env 파일 ──────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  echo "==> $ENV_FILE 생성 (dbaops.env.example 기반 — 값 확인 필요)"
  sudo mkdir -p /etc/dbaops
  sudo cp "$REPO_ROOT/deploy/ec2-vanilla/dbaops.env.example" "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
else
  echo "==> $ENV_FILE 이미 존재 — 유지"
fi

# connections.json 초기화 (없을 때만)
[ -f "$DATA_DIR/connections.json" ] || \
  cp "$REPO_ROOT/deploy/ec2-allinone/connections.example.json" "$DATA_DIR/connections.json"

# ── 5. systemd 유닛 ──────────────────────────────────────────
echo "==> systemd 유닛 설치"
RUN_USER="$(id -un)"
for unit in dbaops-mcp-router dbaops-agent dbaops-streamlit dbaops-slack-bot; do
  sed -e "s|__REPO__|$REPO_ROOT|g" \
      -e "s|__VENV__|$VENV|g" \
      -e "s|__DATA__|$DATA_DIR|g" \
      -e "s|__MYSQL_MCP__|$INSTALL_DIR/mysql-mcp|g" \
      -e "s|__USER__|$RUN_USER|g" \
      "$REPO_ROOT/deploy/ec2-vanilla/systemd/$unit.service" | \
    sudo tee "$UNIT_DIR/$unit.service" >/dev/null
done
sudo systemctl daemon-reload

echo "==> 기동: mcp-router → agent → streamlit"
sudo systemctl enable --now dbaops-mcp-router dbaops-agent dbaops-streamlit

# slack 토큰이 env 에 채워져 있으면 봇도 기동
if sudo grep -qE '^SLACK_BOT_TOKEN=xoxb-' "$ENV_FILE" 2>/dev/null; then
  sudo systemctl enable --now dbaops-slack-bot
  echo "    slack-bot 포함 4개 기동"
else
  echo "    slack-bot 은 미기동 — $ENV_FILE 에 토큰 채운 뒤: sudo systemctl enable --now dbaops-slack-bot"
fi

echo ""
echo "완료. 상태 확인:"
echo "  systemctl status dbaops-mcp-router dbaops-agent dbaops-streamlit --no-pager"
echo "  curl -s http://localhost:9000/healthz"
echo "  브라우저: http://<EC2-IP>:8501  (SG 에서 8501 인바운드 허용 필요)"
