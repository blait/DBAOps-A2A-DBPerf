#!/usr/bin/env bash
# agent/ 컨테이너 → ECR push (linux/arm64).
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="dbaops-agent"
TAG="${TAG:-latest}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" >/dev/null

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR}"

cd "$(dirname "$0")/../agent"
# AgentCore Runtime 은 ARM64 강제 → base image 도 arm64 로 고정.
docker buildx build --platform linux/arm64 --build-arg BASE_PLATFORM=linux/arm64 \
  -t "${ECR}/${REPO}:${TAG}" --push .
echo "pushed ${ECR}/${REPO}:${TAG}"
