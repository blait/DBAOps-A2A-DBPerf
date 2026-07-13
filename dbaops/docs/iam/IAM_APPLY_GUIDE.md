# IAM Policy 적용 가이드 — PoC (testbed 포함)

DBAOps-Agent **PoC** (testbed: Aurora/MySQL/MSK/EC2/시나리오 generator 까지 자기 계정에 띄우는 케이스) 배포 담당자에게 권한을 부여하는 절차.

> 고객 인프라에 agent 만 올리는 케이스(올인원 EC2)는 이 권한이 불필요 — `DatabaseAdministrator` 관리형 + `bedrock:InvokeModel` 인라인이면 됨. [`deploy/ec2-allinone/README.md`](../../deploy/ec2-allinone/README.md) 참조. 이 PoC 권한은 **RDS/MSK/EC2 instance/Scheduler 등을 추가로 생성/관리** 해야 해서 권한 폭이 더 넓음.

---

## 무엇을 적용하는가

같은 디렉토리 안 JSON 3 개 — 각각 managed policy 한계(6,144 자) 안에 들어가도록 분할:

| 파일 | 용도 | 크기 |
|---|---|---|
| `dbaops-poc-deployer-policy-1-compute.json` | VPC / ELB / CloudFront / Lambda / ECS / ECR / EventBridge Scheduler | 4,967 자 |
| `dbaops-poc-deployer-policy-2-data.json` | RDS / MSK / S3 / Secrets / DynamoDB (state lock) | 2,606 자 |
| `dbaops-poc-deployer-policy-3-iam-auth.json` | IAM / Cognito / Logs / Bedrock / STS | 2,025 자 |

3 개 모두 **customer managed policy 로 만들어 사용자에게 attach**.

PoC 만의 추가 권한 (올인원 EC2 배포와 비교):

| 추가 권한 | 사유 |
|---|---|
| `rds:CreateDBCluster / CreateDBInstance / DBParameterGroup / DBSubnetGroup` | Aurora PG + RDS MySQL 자동 생성 |
| `kafka:CreateClusterV2 / DeleteCluster` | MSK Serverless 자동 생성 |
| `scheduler:CreateSchedule` 등 | EventBridge Scheduler — 시나리오 cron |
| `secretsmanager:CreateSecret / DeleteSecret` | Aurora master secret 자동 발급 |
| `cloudfront:CreateInvalidation` 등 | UI 코드 변경 시 CF 캐시 무효화 |
| `ec2:DescribeImageAttribute` | NAT instance AMI 조회 |

---

## 방법 1 — IAM 콘솔 (GUI, 가장 쉬움)

### 1-A. Policy 3 개 생성

각 JSON 파일마다 반복:

1. AWS Console → **IAM** → 왼쪽 **Policies** → 우상단 **Create policy**
2. 상단 탭 **JSON** 클릭
3. JSON 파일 내용 그대로 붙여넣기
4. **Next: Tags** → 태그 (옵션) → **Next: Review**
5. **Name** 입력 (아래 표 참조)
6. **Create policy**

| JSON 파일 | Policy Name |
|---|---|
| `dbaops-poc-deployer-policy-1-compute.json` | `DBAOpsPoCDeployerCompute` |
| `dbaops-poc-deployer-policy-2-data.json`    | `DBAOpsPoCDeployerData` |
| `dbaops-poc-deployer-policy-3-iam-auth.json`| `DBAOpsPoCDeployerAuth` |

### 1-B. 사용자에게 attach

1. IAM → **Users** → 배포 담당자 user 선택
2. **Permissions** 탭 → **Add permissions** → **Attach policies directly**
3. 검색창에 `DBAOpsPoCDeployer` → 위 3 개 정책 모두 체크
4. **Next** → **Add permissions**

---

## 방법 2 — AWS CLI

```bash
# Policy 3 개 생성
aws iam create-policy \
  --policy-name DBAOpsPoCDeployerCompute \
  --policy-document file://dbaops-poc-deployer-policy-1-compute.json \
  --description "DBAOps-Agent PoC deploy — VPC/ELB/Lambda/ECS/ECR/Scheduler"

aws iam create-policy \
  --policy-name DBAOpsPoCDeployerData \
  --policy-document file://dbaops-poc-deployer-policy-2-data.json \
  --description "DBAOps-Agent PoC deploy — RDS/MSK/S3/Secrets"

aws iam create-policy \
  --policy-name DBAOpsPoCDeployerAuth \
  --policy-document file://dbaops-poc-deployer-policy-3-iam-auth.json \
  --description "DBAOps-Agent PoC deploy — IAM/Cognito/Logs/Bedrock"

# Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 사용자에 attach (USER_NAME 치환)
USER_NAME=<배포담당자_user>
for p in DBAOpsPoCDeployerCompute DBAOpsPoCDeployerData DBAOpsPoCDeployerAuth; do
  aws iam attach-user-policy \
    --user-name "$USER_NAME" \
    --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/$p
done
```

IAM role 에 attach 하려면 `attach-role-policy` + `--role-name` 으로.

---

## 방법 3 — Terraform

```hcl
resource "aws_iam_policy" "compute" {
  name        = "DBAOpsPoCDeployerCompute"
  description = "DBAOps-Agent PoC deploy - VPC/ELB/Lambda/ECS/ECR/Scheduler"
  policy      = file("${path.module}/dbaops-poc-deployer-policy-1-compute.json")
}

resource "aws_iam_policy" "data" {
  name        = "DBAOpsPoCDeployerData"
  description = "DBAOps-Agent PoC deploy - RDS/MSK/S3/Secrets"
  policy      = file("${path.module}/dbaops-poc-deployer-policy-2-data.json")
}

resource "aws_iam_policy" "auth" {
  name        = "DBAOpsPoCDeployerAuth"
  description = "DBAOps-Agent PoC deploy - IAM/Cognito/Logs/Bedrock"
  policy      = file("${path.module}/dbaops-poc-deployer-policy-3-iam-auth.json")
}

variable "deployer_user_name" { type = string }

resource "aws_iam_user_policy_attachment" "compute" {
  user       = var.deployer_user_name
  policy_arn = aws_iam_policy.compute.arn
}

resource "aws_iam_user_policy_attachment" "data" {
  user       = var.deployer_user_name
  policy_arn = aws_iam_policy.data.arn
}

resource "aws_iam_user_policy_attachment" "auth" {
  user       = var.deployer_user_name
  policy_arn = aws_iam_policy.auth.arn
}
```

---

## 적용 후 검증

배포 담당자 측에서:

```bash
# 권한 부착 확인
USER_NAME=$(aws sts get-caller-identity --query 'Arn' --output text | awk -F/ '{print $NF}')
aws iam list-attached-user-policies --user-name "$USER_NAME"
# DBAOpsPoCDeployer{Compute,Data,Auth} 3 개 모두 나와야

# DBAOps 가 만들 자원 sample lookup
aws ec2 describe-vpcs --max-items 1 --no-cli-pager
aws rds describe-db-clusters --max-records 20 --no-cli-pager
aws kafka list-clusters-v2 --no-cli-pager
aws s3 ls --no-cli-pager
aws dynamodb describe-table --table-name dbaops-tfstate-lock --region ap-northeast-2 --no-cli-pager 2>&1 | head -1
aws bedrock list-foundation-models --region ap-northeast-2 --no-cli-pager \
  --query 'modelSummaries[?contains(modelId,`opus-4`)].modelId'
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-2 --no-cli-pager
aws bedrock-runtime invoke-model --model-id global.anthropic.claude-opus-4-7 --region ap-northeast-2 \
  --body '{"messages":[{"role":"user","content":"hi"}],"anthropic_version":"bedrock-2023-05-31","max_tokens":10}' \
  --cli-binary-format raw-in-base64-out /tmp/_b.json && cat /tmp/_b.json
```

모두 성공이어야 함 (RDS/MSK 응답이 비어있어도 권한 OK).

---

## 추가로 클라우드 팀이 해줘야 하는 것 — IAM 외

| 항목 | 요청 |
|---|---|
| **서비스 쿼터** | Fargate Spot vCPU 8 / Lambda 동시 100 / RDS Aurora cluster 1 / RDS instance 1 / MSK Serverless cluster 1 / EC2 t4g 1 |
| **리전** | ap-northeast-2 (서울) — 단일 |
| **state bucket 사전 생성 (옵션)** | `dbaops-tfstate-<account>-<region>` S3 + `dbaops-tfstate-lock` DynamoDB. 배포 담당자가 직접 만들 수도 있음 (S3:CreateBucket 권한 부여됨). |

---

## 회수 절차 (PoC 종료 시)

```bash
USER_NAME=<배포담당자_user>
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

for p in DBAOpsPoCDeployerCompute DBAOpsPoCDeployerData DBAOpsPoCDeployerAuth; do
  aws iam detach-user-policy --user-name "$USER_NAME" \
    --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/$p
  aws iam delete-policy --policy-arn arn:aws:iam::$ACCOUNT_ID:policy/$p
done
```

---

## 권한 범위 자세히

| 액션 | Resource 좁힘 | 비고 |
|---|---|---|
| Lambda | `arn:aws:lambda:*:*:function:dbaops-*` | 다른 Lambda 건드리지 않음 |
| S3 | `arn:aws:s3:::dbaops-*` | dbaops 로 시작하는 bucket 만 |
| IAM Role | `arn:aws:iam::*:role/dbaops-*` | dbaops-* role / instance-profile |
| RDS / MSK / Cognito / Bedrock 등 | `Resource: "*"` | resource ARN prefix 가 lookup 시점에 알려지지 않거나 service 가 wildcard 만 지원 |

`bedrock-agentcore:*` 는 preview 라 wildcard. GA 시 좁힐 수 있음.

---

## 문의

- 액션 누락 의심되는 `AccessDenied` 시 → 에러의 `Action` 키 회신 (예: `User: ... is not authorized to perform: ec2:Foo`)
- AgentCore preview 활성 region 확인 필요 시 → AWS support
