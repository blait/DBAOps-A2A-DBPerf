#!/usr/bin/env bash
# MCP Lambda 컨테이너 이미지 빌드/push (linux/arm64).
#
# 우리 PoC 특화 (4개) + 기성 MCP wrap (6개) = 총 10개.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TAG="${TAG:-latest}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR}"

ROOT="$(dirname "$0")/.."
cd "${ROOT}/mcp_tools"

# (dir name, ECR repo suffix)
TOOLS=(
  rds_pi               rds-pi
  msk_metrics          msk-metrics
  s3_log_fetch         s3-log-fetch
  aws_api              aws-api
  awslabs_cloudwatch   awslabs-cloudwatch
  awslabs_aws_doc      awslabs-aws-doc
  awslabs_aws_api      awslabs-aws-api
  community_prometheus community-prometheus
  community_postgres   community-postgres
  community_mysql      community-mysql
)

n=${#TOOLS[@]}
for ((i=0; i<n; i+=2)); do
  tool="${TOOLS[$i]}"
  name="${TOOLS[$((i+1))]}"
  echo "==> ${name} (from ${tool}/Dockerfile)"
  docker buildx build --platform linux/arm64 \
    -f "${tool}/Dockerfile" \
    -t "${ECR}/dbaops-mcp-${name}:${TAG}" \
    --provenance false \
    --push "${tool}"
done

echo "pushed $((n/2)) mcp images"
