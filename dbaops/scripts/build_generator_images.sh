#!/usr/bin/env bash
# generators/data_generator + generators/log_generator → ECR push (linux/arm64).
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TAG="${TAG:-latest}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR}"

ROOT="$(dirname "$0")/.."
cd "${ROOT}/generators"

echo "==> data-generator"
docker buildx build --platform linux/arm64 \
  -f data_generator/Dockerfile \
  -t "${ECR}/dbaops-data-generator:${TAG}" \
  --push .

echo "==> log-generator"
docker buildx build --platform linux/arm64 \
  -f log_generator/Dockerfile \
  -t "${ECR}/dbaops-log-generator:${TAG}" \
  --push .

echo "pushed dbaops-data-generator:${TAG} and dbaops-log-generator:${TAG}"
