# 연결 정보 — 어디서 오고, 무엇을 사람이 입력해야 하나

각 MCP 도구가 붙을 대상(DB host, Prometheus URL, MSK cluster, 자격증명 등)을
**어떻게 얻는가**를 정리한다. PoC(우리가 만든 testbed)와 고객 환경(올인원 EC2)이
근본적으로 다르므로 둘을 나눠 설명한다.

---

## 0. 한 줄 요약

| 환경 | 연결정보를 얻는 방법 |
|---|---|
| **PoC (testbed)** | 우리가 Terraform 으로 testbed 를 직접 만들었으므로, **terraform 이 자기 자신의 endpoint 를 output 으로 알고** Lambda env 로 자동 주입. 사람이 입력할 것 없음. |
| **고객 환경 (올인원 EC2)** | testbed 가 없다. **연결설정 UI** 가 instance role 로 AWS 를 탐색해 host/cluster/bucket 을 **드롭박스로 자동 제공**. 단 **DB 자격증명(user/password 또는 Secret)** 만은 사람이 입력해야 한다. |

핵심: **"우리가 원래 연결정보를 알았던" 게 아니라, testbed 를 우리가 만들었기 때문에 terraform 이
자동으로 알았던 것**이다. 고객 환경에선 그 자리를 연결설정 UI 의 자동 탐색이 대체한다.

---

## 1. PoC — terraform 이 자동으로 알았던 구조

> ※ 이 절은 AgentCore 시절 레거시 경로 — 현재 배포(올인원 EC2)에서는 사용되지 않는다.

우리가 `infra/envs/poc/` 로 Aurora/MySQL/MSK/Prometheus/S3 를 **직접 생성**하므로,
terraform 은 만들면서 그 endpoint 를 module output 으로 갖게 된다. 그걸 그대로 Lambda env 로 흘린다.

```
우리가 만든 testbed                    →  Lambda env_var (자동 주입)
────────────────────────────────────    ──────────────────────────────
module.aurora_postgres.endpoint        →  PG_HOST
module.aurora_postgres.master_user_secret_arn → PG_SECRET_ARN
module.rds_mysql.endpoint              →  MYSQL_HOST
module.rds_mysql.master_user_secret_arn→  MYSQL_SECRET_ARN
module.ec2_prometheus.prometheus_endpoint → PROMETHEUS_URL
module.msk_serverless.cluster_name     →  KAFKA_CLUSTER_NAME
module.s3_logs.bucket_name             →  (logs_bucket)
```

코드: `infra/envs/poc/main.tf` 의 각 `module "lambda_*"` 블록 `env_vars`.

`infra_context`(aurora_writer_id 등)는 더 단순하게 — **우리가 그 이름으로 만들었으니**
`scripts/register_gateway_targets.py` 에 하드코딩돼 있었다:

```python
"INFRA_AURORA_WRITER_ID": "dbaops-poc-aurora-pg-writer",
"INFRA_MYSQL_DB_ID":      "dbaops-poc-mysql",
"INFRA_MSK_CLUSTER_NAME": "dbaops-poc",
```

→ 즉 PoC 에선 **사람이 연결정보를 입력하거나 조회한 적이 없다.** 전부 terraform 자기참조.

---

## 2. 고객 환경 — 연결설정 UI 가 대체

testbed 가 없으므로 위 자동참조가 불가능하다. 대신 **🔌 MCP 연결설정** 탭(`ui/streamlit/components/view_connections.py`)이
EC2 instance role 로 고객 계정을 탐색해 드롭박스로 제공한다.

### 2-1. 자동으로 채워지는 것 (탐색 → 드롭박스)

| 필드 | 탐색 API | 필요 권한 | 비고 |
|---|---|---|---|
| PG/MySQL **Host·Port** | `rds:DescribeDBInstances`, `DescribeDBClusters` | `rds:Describe*` | 인스턴스/클러스터(writer·reader) 선택 시 자동 |
| **MSK Cluster Name** | `kafka:ListClustersV2` | `kafka:ListClustersV2` | 이름 문자열만 (host·비번 없음) |
| **Prometheus URL** | `ec2:DescribeInstances` | `ec2:Describe*` | EC2 선택 → `http://<private-ip>:9090` 자동 구성 |
| **S3 로그 버킷** | `s3:ListAllMyBuckets` | `s3:ListAllMyBuckets` | 버킷 드롭박스 |
| **Secret(자격증명)** | `secretsmanager:ListSecrets` | `secretsmanager:ListSecrets` | ARN/이름 드롭박스 (카드의 "🔑 Secret 목록 불러오기" 버튼으로 로드) |
| AWS Region / Bedrock Model | (정적 + `bedrock:ListInferenceProfiles`) | — | |

> 권한이 없으면 해당 항목만 **조용히 직접입력으로 fallback** 한다 (앱은 안 깨짐).
> 어떤 탐색이 권한으로 막혔는지는 UI 상단 "⚠️ 일부 탐색 권한 없음" expander 에 표시된다.

### 2-2. 사람이 반드시 입력해야 하는 것

| 항목 | 왜 자동이 안 되나 |
|---|---|
| **DB User / Password** | RDS describe 로는 **절대 안 나온다**(보안). 사람이 알아야 함. |
| 또는 **Secret ARN** | DB 자격증명이 Secrets Manager 에 있으면 드롭박스 선택 가능. 단 **값 읽기**는 `secretsmanager:GetSecretValue` 권한 + (고객 정책에 따라) secret 이름 제약(`DB*`, `rds*` 등)을 만족해야 함. |

즉 **테스트/연결이 막히는 거의 유일한 지점은 "DB 비밀번호를 모를 때"** 다.
host·port·cluster·bucket 은 다 자동으로 찾지만, **비밀번호만은 사람이 제공**해야 한다.

### 2-3. MSK 는 비번이 없다 (DB 와 다름)

MSK 메트릭 도구(`msk-metrics`)는 실제로는 **CloudWatch `AWS/Kafka` 네임스페이스**를 조회한다.
따라서 필요한 건 **클러스터 이름 문자열 하나**뿐 — host·password 가 없다.

- `kafka:ListClustersV2` 있으면 → 드롭박스 자동
- 없으면 → **클러스터 이름만 직접 입력** (메트릭 조회 자체는 `cloudwatch:*` 로 동작)

---

## 3. 도구별 필요 입력 — 한눈에

| 도구 | 자동(탐색) | 수동 입력 | 추가 권한 없이 동작? |
|---|---|---|---|
| `community-postgres` | host, port | **user/password 또는 Secret**, dbname(기본 postgres), `PG_ACCESS_MODE` | ❌ 자격증명 필요 |
| `community-mysql` | host, port | **user/password 또는 Secret**, db(기본 mysql) | ❌ 자격증명 필요 |
| `community-prometheus` | URL(EC2 탐색 시) | URL(탐색 안 되면) | ❌ URL 필요 |
| `msk-metrics` | cluster name | cluster name(탐색 안 되면), topic/CG(선택) | ⚠️ 이름만 |
| `rds-pi` | — | — | ✅ instance role 만 |
| `s3-log-fetch` | (버킷 드롭박스) | — | ✅ instance role 만 |
| `aws-api` | — | — | ✅ instance role 만 |
| `awslabs-cloudwatch` | — | — | ✅ instance role 만 |
| `awslabs-aws-doc` | — | — | ✅ (외부 문서) |
| `awslabs-aws-api` | — | — | ✅ instance role 만 |

→ **연결정보 불필요 6종**(rds-pi/s3/aws-api/awslabs×3)은 기본 ON 으로 즉시 동작.
**연결정보 필요 4종**(PG/MySQL/Prometheus/MSK)만 위 입력을 채운 뒤 켜진다.
(라우터 기본값: `mcp_router/connections.py` 의 `NO_CONFIG_TARGETS`.)

> `community-postgres` 의 `PG_ACCESS_MODE`: `restricted`(기본) | `unrestricted` —
> `unrestricted` 는 EXPLAIN/인덱스 분석 도구가 열리므로 **읽기전용 계정과 함께** 사용한다.

---

## 4. 연결정보가 저장되는 곳

연결설정 UI 가 저장 → `/data/connections.json` (라우터와 공유 볼륨) → 라우터가 mtime 감지해 자동 reload.

```json
{
  "tools": {
    "community-postgres": {
      "enabled": true,
      "PG_HOST": "...",            ← RDS 드롭박스로 자동
      "PG_PORT": "5432",
      "PG_DBNAME": "postgres",
      "PG_USER": "...",            ← 사람이 입력
      "PG_PASSWORD": "...",        ← 사람이 입력 (또는 PG_SECRET_ARN)
      "PG_SECRET_ARN": ""
    }
  },
  "infra_context": { "aurora_writer_id": "...", ... }
}
```

자격증명 우선순위(라우터 `connections.py:_resolve_secret`): **user/password 직접 > Secret ARN fetch**.
둘 다 없으면 그 stdio 도구는 spawn 하지 않는다(연결 실패 로그만, 앱은 정상).

---

## 5. 권한이 없을 때 — 그래도 동작하나

| 막힌 권한 | 영향 | 대안 |
|---|---|---|
| `rds:Describe*` | DB host 드롭박스 안 뜸 | host 직접 입력 |
| `kafka:ListClustersV2` | MSK 드롭박스 안 뜸 | cluster 이름 직접 입력 |
| `ec2:DescribeInstances` | Prometheus 드롭박스 안 뜸 | URL 직접 입력 |
| `secretsmanager:ListSecrets` | Secret 드롭박스 안 뜸 | ARN 직접 입력 또는 user/pass 직접 |
| `secretsmanager:GetSecretValue` | Secret 값 못 읽음 | user/pass 직접 입력 |
| **`bedrock:InvokeModel`** | **에이전트 자체가 안 돎** | **반드시 추가 — 우회 불가** |

→ 탐색 권한은 전부 "있으면 편하고 없으면 직접입력"이지만, **`bedrock:InvokeModel` 만은 필수**다.

---

## 6. 관련 코드 / 문서

- 자동 탐색: `ui/streamlit/components/view_connections.py` (`_discover_aws`, `_bedrock_models`)
- stdio 연결 구성: `mcp_router/connections.py` (`stdio_spec`, `_resolve_secret`, `NO_CONFIG_TARGETS`)
- PoC 자동주입: `infra/envs/poc/main.tf` (`module "lambda_*"` env_vars)
- infra_context: `agent/src/dbaops_agent/tools/mcp_tools.py` (`infra_context`)
- 배포 절차: [`deploy/ec2-allinone/README.md`](../deploy/ec2-allinone/README.md)
