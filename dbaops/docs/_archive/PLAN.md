# DBAOps Agent — LangGraph + AWS Bedrock AgentCore 구현 플랜

## Context

**왜 만드는가**: DBA / SRE가 OS 인프라, DBMS 내부 성능, 로그를 각각 다른 도구로 들여다보며 추세·이상·RCA를 수동으로 엮는 작업을 반복한다. 이 PoC는 그 워크플로우를 LangGraph 에이전트로 묶어, 시간 범위 + 대상만 던지면 "추세 + 이상 지점 + 가설 + 다음 확인 항목"을 구조화된 리포트로 돌려주는 단일 분석 면을 만든다.

**무엇을 만드는가**: AWS Bedrock AgentCore (Runtime + Gateway + MCP) 위에 LangGraph 그래프를 올리고, 서울 리전(ap-northeast-2)의 Aurora PG / RDS MySQL / MSK / EC2 Prometheus를 분석 대상으로 한다. 더미 데이터·로그 생성기로 정상/이상 트래픽을 주입해 분석 시나리오를 검증한다. PoC 비용은 데모 외 시간엔 최소화한다.

**의도된 결과**: Streamlit 웹 UI에서 자연어 요청 → 라우터가 OS / DB-perf / Log 서브그래프로 분기 → MCP 도구 호출 → 구조화된 분석 리포트(JSON + Markdown)를 반환. 추후 MSSQL / ClickHouse / Slack 인터페이스로 확장 가능한 구조.

---

## 사용자 결정 사항 (확정)

| 항목 | 결정 |
|---|---|
| DBMS 범위 | Aurora PostgreSQL + RDS MySQL + MSK (Kafka). MSSQL / ClickHouse는 인터페이스만, 이번 빌드에선 제외 |
| AgentCore 구성 | Runtime + Gateway + MCP 풀구성 |
| 리전 | **ap-northeast-2 (서울) 단일 리전**. 빌드 시점에 `aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-2`로 GA 확인 가드 추가 |
| MSK | **24×7 상시 운영** (MSK Serverless) |
| 사용자 면 | **Streamlit 웹 UI** |
| LLM 모델 | **전부 Opus 4.7** (`claude-opus-4-7`) |
| 비용 정책 | DB는 야간 자동 stop, 그 외는 idle 최소 사양 |

---

## 최종 아키텍처 (서울 단일 리전)

```
[Streamlit UI (ECS Fargate)] ──HTTPS──► [AgentCore Runtime (Seoul)]
                                              │ MCP
                                              ▼
                                    [AgentCore Gateway (Seoul)]
                                              │
                ┌──────────┬───────────┬──────┴──────┬───────────┬──────────┐
                ▼          ▼           ▼             ▼           ▼          ▼
        prometheus_q  cw_metrics   rds_pi    sql_readonly   msk_metrics  s3_log_fetch
          (Lambda)     (Lambda)   (Lambda)    (Lambda)       (Lambda)    (Lambda)
                │                      │           │            │           │
                ▼                      ▼           ▼            ▼           ▼
   [EC2 Prometheus + Node Exporter] [Aurora PG] [RDS MySQL] [MSK Serverless] [S3 logs]
                ▲                      ▲           ▲            ▲           ▲
                └──────────────────────┴───────────┴────────────┴───────────┘
                       [ECS Fargate Spot: data generator + log generator]
```

전체 us-west-2 cross-region 트릭 불필요. Bedrock 모델 호출도 서울 리전에서.

---

## 1. 디렉토리 레이아웃

```
dbaops-agent/
├── README.md
├── Makefile                              # bootstrap | plan | apply | deploy-agent | demo-up | demo-down | destroy
│
├── infra/                                # Terraform 1.7+ (서울 단일 리전)
│   ├── envs/poc/{main.tf, backend.tf, providers.tf, variables.tf}
│   └── modules/
│       ├── network/                      # VPC / 2 AZ / NAT instance(t4g.nano) / VPC endpoints
│       ├── aurora_postgres/              # PG 15, db.t4g.medium writer + reader, PI on
│       ├── rds_mysql/                    # MySQL 8.0, db.t4g.micro, slow+general → CW
│       ├── msk_serverless/               # IAM auth, 3 partitions
│       ├── ec2_prometheus/               # t4g.small Spot, Prometheus + Node Exporter + cloudwatch_exporter
│       ├── ecs_generators/               # Fargate Spot, EventBridge Scheduler trigger
│       ├── ecs_streamlit/                # Streamlit UI 서비스, ALB 뒤
│       ├── s3_logs/                      # 30d → Glacier IR → 90d expire
│       ├── iam/                          # 실행 역할, MCP Lambda 역할
│       ├── observability/                # CW 로그그룹 7d 보존
│       └── agentcore/                    # Runtime / Gateway / Targets / Cognito identity
│
├── agent/                                # LangGraph 애플리케이션 (ARM64 컨테이너)
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── src/dbaops_agent/
│       ├── runtime_entry.py              # AgentCore Runtime invocation contract
│       ├── graph.py                      # StateGraph: router + 3 subgraphs + hypothesis + reporter
│       ├── state.py                      # AnalysisState TypedDict
│       ├── nodes/
│       │   ├── router.py
│       │   ├── os_subgraph.py
│       │   ├── db_subgraph.py
│       │   ├── log_subgraph.py
│       │   ├── hypothesis.py
│       │   └── reporter.py
│       ├── tools/mcp_client.py           # MCP client → Gateway, retry + budget + dedup cache
│       ├── analyzers/
│       │   ├── anomaly.py                # z-score + EWMA + change-point
│       │   └── log_classify.py           # Drain3 템플릿
│       └── llm.py                        # Bedrock Opus 4.7 client
│
├── ui/streamlit/                         # 단일 페이지: 요청 폼 + 리포트 렌더
│   ├── app.py
│   ├── Dockerfile
│   └── components/{request_form.py, report_view.py}
│
├── mcp_tools/                            # Lambda 백엔드 MCP 도구 6종
│   ├── prometheus_query/                 # PromQL /query, /query_range
│   ├── cloudwatch_metrics/               # GetMetricData 정규화
│   ├── rds_pi/                           # Performance Insights top SQL
│   ├── sql_readonly/                     # sqlglot AST gate, statement_timeout, IAM auth
│   ├── msk_metrics/                      # CW MSK + JMX-via-Prometheus
│   └── s3_log_fetch/                     # gz 로그 byte-range 읽기
│
├── generators/
│   ├── data_generator/workloads/         # baseline | lock_contention | slow_query | connection_spike | kafka_isr_shrink
│   └── log_generator/                    # pg_log | mysql_log | kafka_log → S3 + CW
│
├── schemas/                              # JSON Schema: analysis_request, analysis_report, mcp_tool_io/*
│
└── scripts/
    ├── bootstrap.sh                      # TF state 버킷 + DDB lock
    ├── verify_agentcore_seoul.sh         # 빌드 가드: AgentCore Seoul GA 확인
    ├── build_agent_image.sh              # buildx --platform linux/arm64 + ECR push
    ├── register_gateway_targets.py       # 멱등 CreateGatewayTarget
    └── seed_databases.sh
```

---

## 2. AgentCore 와이어링

### 2a. 빌드 가드
스크립트 첫 줄에서 서울 리전 GA 확인:
```bash
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-2
# 실패 시 명확한 메시지로 abort: "AgentCore not yet GA in ap-northeast-2 — switch region or wait"
```

### 2b. Gateway target — 전부 Lambda 타겟
| MCP 도구 | 입력 | 출력 (요약 JSON, ≤100 KB) |
|---|---|---|
| `prometheus_query` | promql, range | timeseries [{ts, value}] |
| `cloudwatch_get_metric_data` | namespace, metric, dims, range, stat | timeseries |
| `rds_performance_insights` | db_id, range, group_by | top SQL by AAS |
| `sql_readonly_pg` / `sql_readonly_mysql` | sql, db_id | rows (LIMIT 1000), AST 검증 |
| `msk_metrics` | cluster_arn, metric, range | timeseries |
| `s3_log_fetch` | bucket, key, byte_range, regex | matched lines |

**등록 흐름** (`scripts/register_gateway_targets.py`):
1. `CreateGateway` — `protocolType=MCP`, ingress = Cognito JWT (Streamlit이 발급)
2. 각 Lambda별 `CreateGatewayTarget` — Lambda ARN + `tool_io` JSON Schema
3. DB 비밀번호 → Secrets Manager (Lambda 역할이 read), CW/PI/MSK는 IAM 만으로 충분

### 2c. Runtime 패키징
- Python 3.12 slim ARM64 컨테이너, AgentCore SDK harness
- ECR (서울)
- env: `GATEWAY_ENDPOINT`, `BEDROCK_REGION=ap-northeast-2`, `BEDROCK_MODEL_ID=claude-opus-4-7`
- 실행 역할: `bedrock-agentcore:InvokeGateway` + `bedrock:InvokeModel`

---

## 3. LangGraph 그래프 설계

```
ENTRY → router_node ── Opus 4.7 ──► {os | db | log | multi}
            │
            ├─► os_subgraph  ──┐
            ├─► db_subgraph  ──┼─► hypothesis_node (multi 또는 ≥2 finding)
            └─► log_subgraph ──┘            │
                                             ▼
                                      reporter_node (deterministic JSON + Markdown)
                                             │
                                            END
```

**`AnalysisState`** (`agent/src/dbaops_agent/state.py`):
```python
{
  "request": AnalysisRequest,        # time_range, targets, lens
  "route": "os" | "db" | "log" | "multi",
  "os_findings" | "db_findings" | "log_findings": list[Finding] | None,
  "raw_signals": dict,               # (tool, hash(params)) dedup cache
  "hypotheses": list[Hypothesis] | None,
  "report": AnalysisReport | None,
  "messages": list[BaseMessage],
  "tool_budget": int                 # MCP 호출당 1 차감
}
```

**서브그래프별 노드**:
- **OS**: `os_plan` (LLM이 PromQL + CW metric 선정) → `os_fetch` (병렬 MCP) → `os_anomaly` (deterministic z-score+EWMA) → `os_summarize` (LLM → Findings)
- **DB**: `db_plan` (LLM) → 병렬 `db_fetch_pg` (`pg_stat_activity/statements/locks` + PI top SQL), `db_fetch_mysql` (`events_statements_summary_by_digest`, `INNODB_LOCK_WAITS`), `db_fetch_kafka` (BytesIn/Out, UnderReplicatedPartitions, ConsumerLag) → `db_correlate` (deterministic time-window join) → `db_summarize` (LLM)
- **Log**: `log_plan` (LLM) → `log_fetch` (S3 ranged + chunked) → `log_classify` (Drain3 deterministic) → `log_rca` (LLM → RCA + 추가 확인 항목)

**LLM은 plan / summarize / hypothesis / log_rca 에서만**. fetch · 이상치 계산 · 템플릿 추출 · 상관 · 리포트 조립은 deterministic Python.

**도구 호출**은 `ToolNode` 미사용 — `tools/mcp_client.py` 가 retry + budget + dedup cache 직접 관리. Gateway 부하 통제와 비용 가드.

**Streamlit ↔ Runtime**:
1. UI 폼 → `AnalysisRequest` JSON → Cognito 토큰 첨부 → AgentCore Runtime Invoke
2. Runtime은 LangGraph DynamoDB checkpointer 사용, 중간 상태 보존 → UI에서 progress 폴링

---

## 4. 생성기 설계 (ECS Fargate Spot, 0.25 vCPU / 512 MB)

### 데이터 생성기
| 패턴 | 스케줄 | 대상 | 효과 |
|---|---|---|---|
| `baseline` | 데모 시간 always-on | 양 DB + MSK | PG 50 TPS / MySQL 30 QPS / Kafka 100 msg/s |
| `lock_contention` | 30분마다 3분 | PG | 핫 row `SELECT … FOR UPDATE` 동시 |
| `slow_query` | 20분마다 2분 | MySQL | 인덱스 누락 풀스캔 조인 |
| `connection_spike` | 45분마다 90초 | PG | 10초간 200 short conn |
| `kafka_isr_shrink` | 60분마다 60초 | MSK | producer batch jump + consumer pause |

EventBridge Scheduler → ECS RunTask. Spot 중단 OK (idempotent).

### 로그 생성기 (별도 task definition)
- **PG**: `ERROR: deadlock detected`, `LOG: duration: ... ms statement: …`, `FATAL: too many connections`
- **MySQL**: error log `[ERROR]`, slow log w/ Query_time + Lock_time, audit log JSON
- **Kafka**: `server.log` ISR shrink, `connect.log` task failure, `ksql` query restart
- baseline 1 line/s + EventBridge-triggered burst (200 lines/min × 2분)
- 출력: `s3://dbaops-logs/{db}/{date}/{hour}.log.gz` + CloudWatch Logs (라이브 검색 시연)

---

## 5. 비용 가드 (서울, 24×7 MSK 가정)

| 레버 | 선택 | $/월 |
|---|---|---|
| AgentCore Runtime | scale-to-zero, 호출 시만 과금 | ~$0 |
| AgentCore Gateway | $0.005/1k API + $0.025/1k search | <$1 |
| Aurora PG (db.t4g.medium × 2) | **야간 자동 stop** (16h/day stopped) | ~$25 |
| RDS MySQL (db.t4g.micro) | 야간 stop | ~$5 |
| **MSK Serverless 24×7** | 사용자 결정 | **~$540** |
| EC2 Prometheus (t4g.small Spot) | gp3 20 GB | ~$8 |
| NAT instance (t4g.nano) | NAT GW 대체 | ~$3 |
| VPC endpoints (S3, Secrets, bedrock-runtime) | | ~$15 |
| CloudWatch Logs | 7d 보존 | <$5 |
| S3 logs | gzip + Glacier IR | <$2 |
| ECS Streamlit | 0.25 vCPU + ALB | ~$20 |
| ECS Fargate Spot 생성기 | 데모 외 정지 | $5–10 |
| Bedrock Opus 4.7 | 사용량 기반 | 호출량에 비례 |

**Idle 베이스라인 (DB stop, MSK 24×7)**: **~$620/월**, 90% 가까이가 MSK.
**완전 운영 (DB 24×7 + 데모 호출)**: **~$700–800/월** + LLM 호출 비용.

> 비용 감축 옵션을 PoC 진행 중에 다시 논의할 수 있도록 `make demo-down` 으로 MSK까지 내릴 수 있는 hidden path 는 유지 (확정은 아님).

---

## 6. 단계별 롤아웃

**Phase 1 — End-to-end thin slice (Week 1)**: 데모 가능한 한 줄 슬라이스
- TF state bootstrap. `network`, `iam`, `s3_logs`, Aurora PG, EC2 Prometheus 배포
- Lambda 1개: `prometheus_query`
- AgentCore Gateway + 단일 target
- Minimal LangGraph: router → os_subgraph → reporter (anomaly / hypothesis 미포함)
- Streamlit UI 기본형
- 검증: 시간 범위 + 인스턴스 입력 → CPU / 메모리 추세 리포트 받기

**Phase 2 — 생성기 + 로그 경로 (Week 2)**
- RDS MySQL, ECS data generator (baseline), log generator + S3 sink
- `s3_log_fetch`, `sql_readonly_pg` Lambda
- `log_subgraph` Drain3 까지 end-to-end

**Phase 3 — DB-perf + MSK (Week 3)**
- MSK Serverless + producer/consumer
- `rds_performance_insights`, `sql_readonly_mysql`, `msk_metrics` Lambda
- `db_subgraph` PI top-SQL + Kafka lag
- 이상 워크로드 활성화 (lock_contention, slow_query, connection_spike, isr_shrink)

**Phase 4 — Cross-correlation + reporter polish (Week 4)**
- `hypothesis_node` 도입
- JSON + Markdown 리포트 템플릿 (`schemas/analysis_report.json` 기준)
- `analyzers/anomaly.py`: z-score + EWMA + change-point
- LangGraph DynamoDB checkpointer + tool-budget 가드

**Phase 5 — 데모 자동화 + 비용 게이트 (Week 5)**
- `make demo-up` / `demo-down`
- EventBridge 야간 DB stop
- CloudWatch 대시보드 + 비용 태그
- README 데모 스크립트

**Phase 6 — Observability + 확장 훅 (Week 6, optional)**
- AgentCore Observability 대시보드
- MSSQL + ClickHouse 인터페이스 stub (스키마만)

---

## 7. 검증 방법 (end-to-end)

1. **인프라**: `make plan` → `make apply` → `terraform output streamlit_url` 으로 UI 접근
2. **AgentCore 가드**: `bash scripts/verify_agentcore_seoul.sh` — 0 exit 면 진행
3. **Phase 1 슬라이스**: Streamlit에서 `lens=os, target=ec2-prometheus, range=last 1h` → 리포트에 CPU/Memory 추세 + 단순 이상 표시 확인
4. **로그 경로**: 로그 생성기 burst 발생 → `lens=log, source=postgres` → 리포트에서 `deadlock detected` 빈도 증가 + 시간대 정확히 식별
5. **DB-perf**: `lock_contention` 트리거 → `lens=db, target=aurora` → `pg_stat_activity` waiting 프로세스 + 락 체인 식별, PI top SQL과 시간 정렬
6. **Kafka**: `kafka_isr_shrink` 트리거 → `UnderReplicatedPartitions > 0` 시간 + ConsumerLag 급증 매칭
7. **회귀**: pytest로 deterministic 노드 (anomaly, log_classify, db_correlate) 단위 테스트
8. **비용**: 매일 cost-explorer 태그별 점검 — 예산 초과 시 Slack alert

---

## 8. 핵심 수정 대상 파일

- `infra/envs/poc/main.tf` — 모듈 조립
- `infra/modules/agentcore/{runtime,gateway,targets}.tf` — AgentCore 리소스
- `agent/src/dbaops_agent/graph.py` — LangGraph StateGraph 정의
- `agent/src/dbaops_agent/runtime_entry.py` — AgentCore invocation contract
- `agent/src/dbaops_agent/tools/mcp_client.py` — Gateway MCP 호출
- `mcp_tools/sql_readonly/handler.py` — sqlglot AST gate (보안 핵심)
- `scripts/register_gateway_targets.py` — Gateway target 멱등 등록
- `scripts/verify_agentcore_seoul.sh` — 빌드 가드
- `ui/streamlit/app.py` — 사용자 면
- `generators/data_generator/workloads/*.py` — 5개 패턴

---

## 9. 남은 미결 사항 (구현 시작 전 결정 필요 없음, 진행하며 정리)

1. **이상 탐지 강화**: z-score + EWMA로 충분한지, Phase 6에 Lookout for Metrics 추가할지
2. **DB 인증**: IAM auth + Secrets Manager 자동 회전 (가정) vs 정적 read-only 비밀번호
3. **데이터 현실성**: 순수 합성 vs TPC-H seed vs 익명화된 카카오 스냅샷
4. **MSSQL / ClickHouse stub 시점**: Phase 6 / 별도 후속
5. **컴플라이언스**: 한국어 로그 컨텐츠가 us-west-2 모델로 가지 않음 (서울 모델 호출로 해결됨)
