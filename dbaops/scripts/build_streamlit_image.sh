#!/usr/bin/env bash
# ui/streamlit/ 컨테이너 → ECR push (linux/arm64).
# 첫 apply (terraform) 가 ECR repo 를 만든 뒤 실행.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REPO="dbaops-streamlit"
TAG="${TAG:-latest}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

aws ecr describe-repositories --repository-names "${REPO}" --region "${REGION}" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "${REPO}" --region "${REGION}" >/dev/null

aws ecr get-login-password --region "${REGION}" | docker login --username AWS --password-stdin "${ECR}"

cd "$(dirname "$0")/../ui/streamlit"
docker buildx build --platform linux/arm64 -t "${ECR}/${REPO}:${TAG}" --push .
echo "pushed ${ECR}/${REPO}:${TAG}"
