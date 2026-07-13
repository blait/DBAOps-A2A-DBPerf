#!/usr/bin/env bash
# Build guard: AgentCore 서울 리전 GA 확인.
# 0 exit = 진행 가능, 그 외 = abort 후 메시지 출력.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"

echo "==> checking bedrock-agentcore-control in ${REGION}"
if ! aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" >/dev/null 2>&1; then
  cat >&2 <<EOF
ERROR: AgentCore control plane not callable in ${REGION}.
- 서비스가 아직 GA 가 아니거나 권한이 부족합니다.
- 다른 리전(예: us-west-2) 으로 전환하거나 GA 확인 후 재시도 하세요.
EOF
  exit 1
fi
echo "  ok"
