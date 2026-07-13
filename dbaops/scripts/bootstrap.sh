#!/usr/bin/env bash
# Terraform state 백엔드 부트스트랩 — S3 버킷 + DynamoDB lock 테이블.
# 멱등.
set -euo pipefail

REGION="${REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="dbaops-tfstate-${ACCOUNT_ID}-${REGION}"
LOCK_TABLE="dbaops-tfstate-lock"

echo "==> bootstrap region=${REGION} bucket=${BUCKET} lock=${LOCK_TABLE}"

if ! aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  aws s3api create-bucket \
    --bucket "${BUCKET}" \
    --region "${REGION}" \
    --create-bucket-configuration "LocationConstraint=${REGION}"
  aws s3api put-bucket-versioning --bucket "${BUCKET}" --versioning-configuration Status=Enabled
  aws s3api put-bucket-encryption --bucket "${BUCKET}" --server-side-encryption-configuration '{
    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
  }'
  aws s3api put-public-access-block --bucket "${BUCKET}" --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
  echo "  created bucket ${BUCKET}"
else
  echo "  bucket ${BUCKET} already exists"
fi

if ! aws dynamodb describe-table --table-name "${LOCK_TABLE}" --region "${REGION}" >/dev/null 2>&1; then
  aws dynamodb create-table \
    --table-name "${LOCK_TABLE}" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${REGION}" >/dev/null
  echo "  created lock table ${LOCK_TABLE}"
else
  echo "  lock table ${LOCK_TABLE} already exists"
fi

cat <<EOF

다음 단계: infra/envs/poc/backend.tf 의 backend "s3" 블록을 아래 값으로 채우세요.

  bucket         = "${BUCKET}"
  key            = "envs/poc/terraform.tfstate"
  region         = "${REGION}"
  dynamodb_table = "${LOCK_TABLE}"
  encrypt        = true
EOF
