#!/usr/bin/env bash
# Streamlit UI 띄우기 — 환경변수는 terraform output 에서 자동 추출.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
PORT="${PORT:-8502}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="${ROOT}/infra/envs/poc"
UI_DIR="${ROOT}/ui/streamlit"

# Runtime ARN — awscli text 출력은 paginator 때문에 두 번째 줄 'None' 이 붙는 경우가 있다.
# JSON 으로 받아 jq 없이 python 으로 파싱하는 게 안전.
RUNTIME_NAME="${RUNTIME_NAME:-dbaops_poc}"
RUNTIME_ARN="$(aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" --output json \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
arns = [r['agentRuntimeArn'] for r in d.get('agentRuntimes', []) if r.get('agentRuntimeName') == '${RUNTIME_NAME}']
print(arns[0] if arns else '')
")"
if [ -z "${RUNTIME_ARN}" ]; then
    echo "ERROR: agent runtime '${RUNTIME_NAME}' not found in ${REGION}" >&2
    exit 1
fi

# Terraform outputs
TF_OUT="$(cd "${TF_DIR}" && terraform output -json)"
SUBNETS="$(echo "${TF_OUT}" | python3 -c "import sys,json;print(','.join(json.load(sys.stdin)['private_subnet_ids']['value']))")"
SG="$(aws ec2 describe-security-groups --region "${REGION}" --output json \
    --filters "Name=group-name,Values=dbaops-poc-gen-*" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['SecurityGroups'][0]['GroupId'] if d.get('SecurityGroups') else '')
")"

cd "${UI_DIR}"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> Streamlit launching"
echo "    runtime: ${RUNTIME_ARN}"
echo "    subnets: ${SUBNETS}"
echo "    sg:      ${SG}"
echo "    http://localhost:${PORT}"

AGENTCORE_RUNTIME_ARN="${RUNTIME_ARN}" \
BEDROCK_REGION="${REGION}" \
AWS_REGION="${REGION}" \
ECS_CLUSTER="dbaops-poc" \
ECS_SUBNETS="${SUBNETS}" \
ECS_SECURITY_GROUPS="${SG}" \
streamlit run app.py --server.headless true --server.port "${PORT}"
