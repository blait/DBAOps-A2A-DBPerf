#!/usr/bin/env bash
# 시나리오 generator 끄기 — EventBridge schedule 모두 DISABLED.
#   --stop-tasks 옵션 시 실행 중인 generator task 도 stop.
#   --streamlit-down 옵션 시 Streamlit ECS service 도 desired_count=0.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ENV="${ENV:-poc}"
CLUSTER="dbaops-${ENV}"

STOP_TASKS=0
STREAMLIT_DOWN=0
for arg in "$@"; do
  case "$arg" in
    --stop-tasks)     STOP_TASKS=1 ;;
    --streamlit-down) STREAMLIT_DOWN=1 ;;
    *) echo "unknown arg: $arg"; exit 1 ;;
  esac
done

echo "==> disabling all EventBridge schedules"
schedules=$(aws scheduler list-schedules --region "$REGION" --no-cli-pager --output json \
  | python3 -c "import sys,json;[print(s['Name']) for s in json.load(sys.stdin).get('Schedules',[]) if s['Name'].startswith('dbaops-${ENV}-')]")
for name in $schedules; do
  echo "   - $name"
  aws scheduler update-schedule --name "$name" --region "$REGION" --no-cli-pager \
    --state DISABLED --output json >/dev/null
done

if [ "$STOP_TASKS" -eq 1 ]; then
  echo "==> stopping running generator tasks"
  task_arns=$(aws ecs list-tasks --cluster "$CLUSTER" --region "$REGION" --no-cli-pager --output text --query 'taskArns[]' \
    | tr '\t' '\n')
  for arn in $task_arns; do
    [ -z "$arn" ] && continue
    family=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$arn" --region "$REGION" --no-cli-pager --output text --query 'tasks[0].taskDefinitionArn' | awk -F/ '{print $NF}' | awk -F: '{print $1}')
    if [[ "$family" == dbaops-${ENV}-data-* || "$family" == dbaops-${ENV}-log-* ]]; then
      echo "   stop $arn ($family)"
      aws ecs stop-task --cluster "$CLUSTER" --task "$arn" --region "$REGION" --no-cli-pager --output json >/dev/null
    fi
  done
fi

if [ "$STREAMLIT_DOWN" -eq 1 ]; then
  echo "==> Streamlit service desired_count=0"
  aws ecs update-service --cluster "$CLUSTER" --service "dbaops-${ENV}-streamlit" \
    --desired-count 0 --region "$REGION" --no-cli-pager --output json >/dev/null || \
    echo "   (warning: service 가 없거나 이미 0)"
fi

echo "done."
