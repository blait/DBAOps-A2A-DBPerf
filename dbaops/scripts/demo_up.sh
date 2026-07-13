#!/usr/bin/env bash
# 시나리오 generator 켜기:
#   - 인자 없음:  모든 EventBridge schedule 을 ENABLED 로 → cron 자동 부하 시작
#   - 인자 있음:  지정한 시나리오를 ad-hoc 1회 RunTask
#
# 시나리오 키:
#   data-baseline | data-lock-contention | data-slow-query | data-connection-spike
#   data-kafka-isr-shrink | data-cpu-burn | data-disk-io-burst
#   log-postgres | log-mysql | log-kafka
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ENV="${ENV:-poc}"
CLUSTER="dbaops-${ENV}"

if [ "$#" -eq 0 ]; then
  echo "==> enabling all EventBridge schedules (cron auto-trigger)"
  schedules=$(aws scheduler list-schedules --region "$REGION" --no-cli-pager --output json \
    | python3 -c "import sys,json;[print(s['Name']) for s in json.load(sys.stdin).get('Schedules',[]) if s['Name'].startswith('dbaops-${ENV}-')]")
  for name in $schedules; do
    echo "   + $name"
    aws scheduler update-schedule --name "$name" --region "$REGION" --no-cli-pager \
      --state ENABLED --output json >/dev/null
  done
  echo "done. ${ENV} 의 모든 시나리오 schedule 이 ENABLED."
  exit 0
fi

# ad-hoc trigger
SCENARIO="$1"
TASK_DEF="dbaops-${ENV}-${SCENARIO}"

echo "==> ad-hoc RunTask: ${TASK_DEF}"

# 네트워크 정보 (terraform output 에서)
TF_DIR="$(dirname "$0")/../infra/envs/${ENV}"
SUBNET=$(cd "$TF_DIR" && terraform output -json private_subnet_ids 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)[0])")
SG=$(aws ec2 describe-security-groups --region "$REGION" --no-cli-pager --output json \
  --filters "Name=group-name,Values=dbaops-${ENV}-gen-*" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['SecurityGroups'][0]['GroupId'] if d['SecurityGroups'] else '')")

if [ -z "$SUBNET" ] || [ -z "$SG" ]; then
  echo "ERROR: subnet 또는 generator SG 를 찾을 수 없음. terraform apply 가 끝났는지 확인."
  exit 1
fi

CONTAINER_NAME="data-gen"
[[ "$SCENARIO" == log-* ]] && CONTAINER_NAME="log-gen"

aws ecs run-task \
  --region "$REGION" --no-cli-pager \
  --cluster "$CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$TASK_DEF" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --output json \
  --query 'tasks[0].taskArn' || {
    echo "ERROR: RunTask 실패"
    exit 1
  }

echo "started. UI 의 시나리오 라이브 모니터 탭에서 진행 상황 확인."
