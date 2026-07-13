# PoC Quickstart — 우리 PoC 환경 처음부터 구축

> ⚠️ **이 문서는 내부 testbed 데모 전용** (AgentCore/Lambda/CloudFront 기반 — 옛 아키텍처). **현재 고객 배포는 [`deploy/ec2-allinone/README.md`](../deploy/ec2-allinone/README.md) + [`docs/ONBOARDING.md`](ONBOARDING.md)** 를 사용한다.

DBAOps-Agent **PoC** (test bed 까지 포함된 데모 환경) 를 빈 AWS 계정에 처음부터 띄우는 step-by-step 가이드. 이 문서대로 따라가면 ~60~90분 후 CloudFront URL 로 UI + 시나리오 generator 까지 동작.

> 고객 환경에 올리는 거라면 이 repo 의 `deploy/ec2-allinone/` + [`docs/ONBOARDING.md`](ONBOARDING.md) 를 사용. 이 문서는 우리가 시연용으로 쓰는 **test bed 포함** 환경.

---

## 0. 들어가기 전에 — 사전 체크

- [ ] AWS 계정 (admin 권한 또는 [`docs/iam/IAM_APPLY_GUIDE.md`](iam/IAM_APPLY_GUIDE.md) 의 3 분할 policy 적용)
- [ ] 본인 PC: AWS CLI v2, Terraform 1.7+, Docker (buildx), Python 3.12+, git, `boto3` (`pip install boto3`)
- [ ] 리전: `ap-northeast-2` 고정

이 PoC 는 다음 자원을 자기 계정에 자동 생성:

- VPC + 2 AZ public/private subnet + NAT instance
- Aurora PostgreSQL cluster (1 writer + 1 reader)
- RDS MySQL (1 instance)
- MSK Serverless cluster
- EC2 (Prometheus host, t4g.nano)
- S3 bucket (logs)
- ECS cluster + 시나리오 generator 7 + 로그 burst generator 3
- 10 MCP Lambda
- AgentCore Gateway / Runtime
- ECS Streamlit (CloudFront → ALB → Fargate Spot)

월 비용 추정: ~$150 (RDS/MSK/Aurora 가 대부분). 데모 종료 후 step 24 destroy 권장.

---

## 1. AWS 자격증명

```bash
aws configure   # 또는 aws sso login --profile <profile>
aws sts get-caller-identity
```

`Account` 메모.

---

## 2. Bedrock 모델 호출 가능 여부 확인

```bash
aws bedrock-runtime invoke-model \
  --model-id global.anthropic.claude-opus-4-7 \
  --region ap-northeast-2 \
  --body '{"messages":[{"role":"user","content":"hi"}],"anthropic_version":"bedrock-2023-05-31","max_tokens":10}' \
  --cli-binary-format raw-in-base64-out /tmp/_bedrock.json
cat /tmp/_bedrock.json
```

`{"id":"msg_...","type":"message",...}` 면 OK. `AccessDeniedException` 떨어지면 IAM policy 의 `bedrock:InvokeModel` 액션 부여 여부 확인.

---

## 3. AgentCore 서울 GA 가드

```bash
bash scripts/verify_agentcore_seoul.sh
```

성공이면 `bedrock-agentcore-control` 가 ap-northeast-2 에서 활성. 실패하면 다른 리전 시도하거나 AWS 측 활성 요청.

---

## 4. State backend 부트스트랩 (한 번만)

```bash
make bootstrap
```

또는 직접:
```bash
bash scripts/bootstrap.sh
```

→ `dbaops-tfstate-<account>-ap-northeast-2` S3 + `dbaops-tfstate-lock` DynamoDB 생성.

---

## 5. Repo clone (이미 했으면 skip)

```bash
git clone https://github.com/blait/DBAOps-Agent.git
cd DBAOps-Agent
```

---

## 6. 1차 apply — 인프라 + ECR repo (Lambda/ECS service 는 아직 X)

```bash
make plan      # 또는: cd infra/envs/poc && terraform init -upgrade && terraform plan -out=tfplan
make apply
```

기본 var (`mcp_images_pushed=false`, `streamlit_image_pushed=false`) 로 동작.

이 단계에서 만들어지는 것:
- VPC + subnet + NAT + S3 endpoint
- Aurora PG cluster + RDS MySQL + MSK + Prometheus EC2 + S3 logs bucket
- 14 ECR repo (10 MCP + agent + streamlit + 2 generator)
- IAM role / Cognito / AgentCore IAM
- ECS cluster + 7 data + 3 log task definition + EventBridge schedule
- Streamlit ALB + CloudFront distribution

소요: ~10~15분 (Aurora 가 가장 느림).

---

## 7. RDS / Aurora 시드 (선택)

빈 DB 면 시나리오가 무의미하니 schema/data 시드:

```bash
bash scripts/seed_databases.sh
```

> ⚠️ 현재 이 스크립트는 placeholder. 시드 SQL 은 generator (`generators/data_generator/_schema.py`) 가 첫 부하 시점에 자동 생성하므로 **이 step skip 하고 step 12 시나리오 trigger 시 자동 생성됨**. 따로 시드 안 해도 됨.

---

## 8. 시나리오 generator 이미지 빌드 + 푸시

```bash
bash scripts/build_generator_images.sh
```

→ `dbaops-data-generator:latest` + `dbaops-log-generator:latest` ECR push.

---

## 9. MCP Lambda 이미지 빌드 + 푸시

```bash
bash scripts/build_mcp_images.sh
```

→ 10 MCP 이미지 push (~5~10분).

---

## 10. Agent 컨테이너 이미지 빌드 + 푸시

```bash
bash scripts/build_agent_image.sh
```

→ `dbaops-agent:latest` push.

---

## 11. Streamlit 이미지 빌드 + 푸시

```bash
bash scripts/build_streamlit_image.sh
```

→ `dbaops-streamlit:latest` push.

---

## 12. 2차 apply — MCP Lambda 함수 생성

```bash
cd infra/envs/poc
terraform apply -var=mcp_images_pushed=true -var=streamlit_image_pushed=false
```

→ 10 Lambda 함수 생성 (~3분).

---

## 13. AgentCore Gateway / Runtime 등록

```bash
cd ../../..
ENV=poc python scripts/register_gateway_targets.py
```

이 스크립트는 PoC 식별자 (`dbaops-poc-aurora-pg-writer` 등) 를 자동으로 INFRA_* env 에 박는다 — 따로 export 안 해도 됨.

출력 끝부분의 `agentRuntimeArn` 값 메모.

검증:
```bash
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-2 --no-cli-pager \
  --query 'agentRuntimes[?agentRuntimeName==`dbaops_poc`].{name:agentRuntimeName,arn:agentRuntimeArn,status:status}'
```

`status: READY` 까지 ~30초.

---

## 14. 3차 apply — Streamlit ECS service

```bash
cd infra/envs/poc

RUNTIME_ARN=$(aws bedrock-agentcore-control list-agent-runtimes \
  --region ap-northeast-2 --no-cli-pager --output json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(r['agentRuntimeArn'] for r in d['agentRuntimes'] if r['agentRuntimeName']=='dbaops_poc'))")

terraform apply \
  -var=mcp_images_pushed=true \
  -var=streamlit_image_pushed=true \
  -var="agentcore_runtime_arn=${RUNTIME_ARN}"
```

→ ECS service desired_count=1 + Streamlit task running (~2분).

---

## 15. 접속 URL

```bash
terraform output streamlit_url
# → https://dXXXXX.cloudfront.net
```

브라우저로 접속. 🤖 DBAOps Agent 단일 탭 + 🔌 MCP 연결설정 (시나리오 라이브 모니터는 `SHOW_GENERATORS=true` 일 때만).

---

## 16. 시나리오 generator 켜기

7 가지 부하 시나리오 + 3 가지 로그 burst 시나리오가 EventBridge cron 으로 자동 실행되도록 설정돼있음 (terraform 의 `aws_scheduler_schedule.data_gen / log_gen`). 추가로 ad-hoc 실행:

**옵션 A — UI 에서**: 시나리오 라이브 모니터 탭의 카드 → "▶ 시나리오 실행" 버튼.

**옵션 B — CLI**:
```bash
bash scripts/demo_up.sh                    # 모든 generator schedule 활성 (default ENABLED)
bash scripts/demo_up.sh data-slow-query    # ad-hoc 1 회 trigger
bash scripts/demo_up.sh log-postgres       # ad-hoc 로그 burst 1 회
```

---

## 17. 동작 검증 — 데모 시연 6 케이스

| # | 시나리오 | UI 탭 | 질문 예시 |
|---|---|---|---|
| 1 | data-baseline (자동) | 🖥️ OS | "최근 1시간 EC2 prometheus CPU peak" |
| 2 | data-slow-query | 🗄️ DB | "MySQL slow_log 최근 30분 TOP 5" |
| 3 | data-lock-contention | 🗄️ DB | "Aurora PG 락 경합 현황" |
| 4 | data-connection-spike | 🖥️ OS / 🗄️ DB | "Aurora connections 추이" |
| 5 | data-kafka-isr-shrink | 🗄️ DB | "Kafka consumer lag 현황" |
| 6 | log-postgres-burst | 📜 로그 | "최근 1시간 deadlock / FATAL 빈도" |

각 응답에 도구 인용 + 검증 카드 + 차트가 떠야 정상.

---

## 18. CloudFront 캐시 무효화 (선택)

UI 코드 변경 후 새 이미지 배포해도 CloudFront 가 캐시된 버전 줄 수 있음:

```bash
DIST_ID=$(aws cloudfront list-distributions --no-cli-pager --output json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(i['Id'] for i in d['DistributionList']['Items'] if 'DBAOps' in (i.get('Comment') or '')))")
aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
```

---

## 19. 로그 확인

```bash
# Streamlit task 로그
aws logs tail /ecs/dbaops-poc-streamlit --since 30m --format short --region ap-northeast-2

# AgentCore Runtime 로그 (도메인 에이전트 동작)
aws logs describe-log-groups --log-group-name-prefix /aws/bedrock-agentcore/runtimes/dbaops_poc --region ap-northeast-2

# MCP Lambda 로그
aws logs tail /aws/lambda/dbaops-poc-rds-pi --since 30m --region ap-northeast-2

# 시나리오 generator 로그
aws logs tail /ecs/dbaops-poc-generators --since 1h --region ap-northeast-2
```

---

## 20. 코드 변경 후 재배포

| 변경 | 명령 |
|---|---|
| `agent/`, `prompts/` | `bash scripts/build_agent_image.sh` 후 `aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id <id> --cli-input-json file:///tmp/runtime_update.json` (또는 register 스크립트 재실행) |
| `mcp_tools/<tool>/handler.py` | `bash scripts/build_mcp_images.sh` 후 `aws lambda update-function-code --function-name dbaops-poc-<tool> --image-uri <ecr>:latest` + `aws lambda wait function-updated` |
| `mcp_tools/<tool>/tool_io.json` | `ENV=poc python scripts/register_gateway_targets.py` (Lambda 재배포 불필요) |
| `ui/streamlit/` | `bash scripts/build_streamlit_image.sh` 후 ECS service `force-new-deployment` |
| Terraform | `terraform apply` |
| `generators/data_generator/workloads/*.py` | `bash scripts/build_generator_images.sh` (task definition 재생성 불필요 — 이미지 :latest 만 갱신) |

---

## 21. 트러블슈팅

| 증상 | 해결 |
|---|---|
| `verify_agentcore_seoul.sh` 실패 | AgentCore preview 가 서울 리전 활성 안 됐을 때 — AWS 측 신청 |
| 1차 apply 가 `AccessDenied` | admin 권한 확인. 사용자가 admin 이면 console 로그인 후 IAM user 권한 직접 점검 |
| Aurora 생성이 멈춤 | `terraform apply` timeout 가 짧을 때 — 그냥 다시 apply 하면 이어짐 |
| Lambda 가 `Image Not Found` | step 9 build_mcp_images.sh 안 했을 때. step 12 전에 반드시 |
| UI 가 502 / blank | `aws ecs describe-services --cluster dbaops-poc --services dbaops-poc-streamlit` 로 task running 인지. ALB target health 도 |
| 시나리오 카드의 "▶ 실행" 이 RunTask 실패 | task definition 이 아직 생성 안 됐을 가능성. step 6 의 1차 apply 가 끝났는지 |
| `register_gateway_targets.py` 재실행 시 "domain already exists" | 정상 — 멱등 처리됨, 무시 |
| Bedrock 호출이 `ThrottlingException` | RPM 제한. 잠시 대기. 데모 동시 1~2 user 까지는 OK |

---

## 22. 주기적 cron 끄기 (비용 절감)

PoC 가 안 쓸 때도 EventBridge cron 이 부하 generator 를 자동 trigger 하면서 RDS load 가 발생:

```bash
bash scripts/demo_down.sh
```

→ 모든 EventBridge schedule 을 DISABLED 로. RDS 는 살아있지만 외부 부하 없음.

---

## 23. 비용 추정

24시간 모두 켜두면 (서울 리전):

| 자원 | 월 비용 |
|---|---|
| Aurora PG cluster (db.t4g.medium × 2) | ~$60 |
| RDS MySQL (db.t4g.micro) | ~$15 |
| MSK Serverless | ~$50 (트래픽 의존) |
| EC2 Prometheus (t4g.nano) | ~$5 |
| NAT instance | ~$3 |
| ALB + CloudFront + ECS Fargate Spot 1 task | ~$22 |
| Lambda 10개 | ~$1 |
| AgentCore Runtime (preview) + Bedrock 호출 | 호출당 |
| **합계 (인프라)** | **~$155/월** + Bedrock 호출 |

---

## 24. PoC 종료 — destroy

### 옵션 A — 일시 중단 (재개 가능, 자원 보존)

```bash
bash scripts/demo_down.sh                                    # cron 끔
aws ecs update-service --cluster dbaops-poc --service dbaops-poc-streamlit \
  --desired-count 0 --region ap-northeast-2                   # UI task 끔
```

### 옵션 B — 완전 destroy

```bash
# 1) Streamlit service 먼저
cd infra/envs/poc
terraform destroy -target=module.streamlit -auto-approve \
  -var=mcp_images_pushed=true -var=streamlit_image_pushed=true \
  -var="agentcore_runtime_arn=${RUNTIME_ARN}"

# 2) AgentCore Runtime / Gateway (terraform 외부 — 직접 삭제)
RT_ID=dbaops_poc-XXXXXXXX
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id $RT_ID --region ap-northeast-2

GW_ID=$(aws bedrock-agentcore-control list-gateways --region ap-northeast-2 --no-cli-pager --output json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(next(g['gatewayId'] for g in d['items'] if g['name']=='dbaops-poc'))")
# Gateway target 부터 삭제
aws bedrock-agentcore-control list-gateway-targets --gateway-identifier $GW_ID --region ap-northeast-2 --no-cli-pager --output json \
  | python3 -c "import sys,json;[print(t['targetId']) for t in json.load(sys.stdin).get('items',[])]" \
  | xargs -I {} aws bedrock-agentcore-control delete-gateway-target --gateway-identifier $GW_ID --target-id {} --region ap-northeast-2
aws bedrock-agentcore-control delete-gateway --gateway-identifier $GW_ID --region ap-northeast-2

# 3) 나머지 전체
terraform destroy -auto-approve \
  -var=mcp_images_pushed=true -var=streamlit_image_pushed=true \
  -var="agentcore_runtime_arn=${RUNTIME_ARN}"

# 4) state 까지
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws s3 rb s3://dbaops-tfstate-${ACCOUNT_ID}-ap-northeast-2 --force --region ap-northeast-2
aws dynamodb delete-table --table-name dbaops-tfstate-lock --region ap-northeast-2
```

---

## 25. 다음 단계

- [docs/SERVICE_GUIDE.md](SERVICE_GUIDE.md) — 시스템 아키텍처, prompt, MCP 자동 노출 흐름
- 고객 환경 배포는 이 repo 의 [`deploy/ec2-allinone/`](../deploy/ec2-allinone/README.md) + [`docs/ONBOARDING.md`](ONBOARDING.md)

질문이나 막힘은 issue 로.
