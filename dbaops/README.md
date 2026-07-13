# DBAOps-Agent

LangGraph + AWS Bedrock + MCP 기반 DB·인프라 분석 에이전트. 자연어로 "최근 1시간 Aurora CPU 어때?" 라고 물으면 → AI 분석가가 도구를 직접 골라 호출 → 차트 포함 답변을 돌려준다.

**배포 방식**: EC2 한 대 위에 두 가지 중 택 1 — ① docker compose(권장) 또는 ② 생 EC2(venv+systemd, docker 불가 환경).
AgentCore/Gateway/Lambda 없이 동작. 인터페이스는 **Streamlit 웹 UI** + **Slack 봇** 두 가지.

> **고객 환경에 처음 배포 (docker)** → [`deploy/ec2-allinone/README.md`](deploy/ec2-allinone/README.md)
>
> **고객 환경에 처음 배포 (docker 없이, systemd)** → [`deploy/ec2-vanilla/README.md`](deploy/ec2-vanilla/README.md)
>
> **Slack 봇 연결** → [`deploy/ec2-allinone/SLACK_SETUP.md`](deploy/ec2-allinone/SLACK_SETUP.md)
>
> **통합 가이드 (프로젝트 소개~배포~사용법 한 문서)** → [`docs/ONBOARDING.md`](docs/ONBOARDING.md)
>
> 시스템 상세 → [`docs/SERVICE_GUIDE.md`](docs/SERVICE_GUIDE.md)

---

## 아키텍처

```
EC2 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
└─ docker compose
   ├─ mcp-router  :9000   MCP 도구 라우터
   ├─ agent       :8080   LangGraph 단일 에이전트
   ├─ streamlit   :8501   웹 UI + 🔌 MCP 연결설정
   ├─ slack-bot           Socket Mode (outbound only)
   └─ (선택 --profile prometheus) prometheus / postgres-exporter / mysqld-exporter / node-exporter
```

---

## 무엇이 들어있나

```
agent/              LangGraph 단일 에이전트 (Claude Code 스타일 프롬프트)
ui/streamlit/       Streamlit UI + MCP 연결설정 페이지
mcp_router/         MCP 도구 라우터 (stdio proxy + 커스텀 4종)
mcp_tools/          커스텀 MCP 도구 핸들러 (rds-pi / msk / s3-log / aws-api)
slack_bot/          Slack 봇 (Socket Mode, 대화형)
deploy/ec2-allinone/  배포 방법 ① docker-compose + 가이드
deploy/ec2-vanilla/   배포 방법 ② 생 EC2 (venv+systemd, docker 없이)
generators/         시나리오 부하 generator (PoC testbed 용)
infra/              Terraform (PoC 전용 — testbed 인프라)
docs/               가이드 문서
```

---

## Quick start (올인원 EC2)

```bash
# EC2 접속 후
git clone https://github.com/blait/DBAOps-Agent.git dbaops
cd dbaops/deploy/ec2-allinone
cp .env.example .env
nano .env                           # AWS_REGION, Slack 토큰(선택)

docker compose up -d --build        # 4개 서비스 기동
docker compose --profile prometheus up -d --build  # Prometheus 스택 포함
# → http://<ec2-ip>:8501 접속
# → 🔌 MCP 연결설정 탭에서 DB/Prometheus 정보 입력
```

Slack 봇:
```bash
# .env 에 SLACK_BOT_TOKEN / SLACK_APP_TOKEN 추가 후
docker compose up -d --build slack-bot
# 채널에서 @DBAOps 질문 → 스레드에서 멘션 없이 이어 대화
```

상세 절차: [`deploy/ec2-allinone/README.md`](deploy/ec2-allinone/README.md)

---

## 운영 명령

```bash
cd ~/dbaops/deploy/ec2-allinone

docker compose ps                        # 상태 확인
docker compose logs -f agent             # 실시간 로그
docker compose up -d --build             # 코드 갱신 후 재빌드
docker compose down                      # 종료 (데이터 유지)
docker compose down -v                   # 종료 + 연결설정 초기화
```

---

## 연결 도구 (10종)

| 도구 | 설명 | 추가 설정 |
|---|---|---|
| `community-postgres` | PostgreSQL / Aurora PG 쿼리 + EXPLAIN·인덱스 분석 (9개 도구) | Host + 자격증명 |
| `community-mysql` | MySQL / RDS MySQL 쿼리 | Host + 자격증명 |
| `community-prometheus` | PromQL 메트릭 — 동봉 프로파일로 즉시 구축 가능 (`http://prometheus:9090`) | URL |
| `msk-metrics` | MSK/Kafka CloudWatch 메트릭 | Cluster Name |
| `rds-pi` | RDS Performance Insights | — (instance role) |
| `awslabs-cloudwatch` | CloudWatch 메트릭/로그 | — |
| `s3-log-fetch` | S3 로그 byte-range 조회 | — |
| `aws-api` | RDS/EC2/MSK describe + RDS 이벤트/권고사항 + PI 분석 리포트 | — |
| `awslabs-aws-api` | 임의 read-only AWS CLI | — |
| `awslabs-aws-doc` | AWS 문서 검색 | — |

연결 정보 상세: [`docs/CONNECTION_INFO.md`](docs/CONNECTION_INFO.md)

---

## PoC (testbed 포함 환경)

시나리오 generator + Aurora/MySQL/MSK 를 직접 생성하는 전체 testbed 구축:
[`docs/POC_QUICKSTART.md`](docs/POC_QUICKSTART.md) (Terraform 기반, 25단계).

---

## 리전 / 모델

- 기본 리전: `ap-northeast-2` (서울) — `.env` 에서 변경 가능
- LLM: Bedrock Claude Opus 4.7 (`global.anthropic.claude-opus-4-7`)

---

## 라이선스

내부 PoC. 외부 공개 전 별도 검토 필요.
