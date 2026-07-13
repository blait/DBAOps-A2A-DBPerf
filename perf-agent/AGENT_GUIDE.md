# Perf Agent — 기능 및 사용 가이드

DBAOps-A2A-DBPerf 스택의 **쿼리 성능 전문 에이전트** (LangGraph 기반).
**SQL Server / PostgreSQL / MySQL** 세 엔진을 하나의 도구 세트로 진단한다 —
도구는 논리적으로 하나, 내부 SQL만 엔진별로 자동 분기(dialect).

워크플로: `analyze(ReAct) → validate(검증) → revise(조건부 1회) → report`
— 분석 결과를 별도 LLM이 검증해 날조/누락을 거른 뒤 리포트로 정리한다.

---

## 대상 DB (target)

모든 진단 도구는 `target` 파라미터로 대상을 고른다. 등록된 타깃은 대화에서
"pg-test 봐줘", "mysql 쪽은?" 처럼 자연어로 지정하면 에이전트가 알아서 매핑한다.

| target | engine | 설명 |
|---|---|---|
| `mssql-main` | SQL Server | RDS sql-server-instance (기본) |
| `pg-test` | PostgreSQL | dbaops-seoul-test-pg (appdb) |
| `mysql-poc` | MySQL 8.0 | dbaops-poc-mysql (dbaops, 3,400만건 테이블) |

타깃 추가/변경: `/etc/dbaops/dbaops.env`의 `DB_TARGETS`(JSON) 수정 후 `sudo systemctl restart dbperf-a2a`.

## 이 에이전트가 할 수 있는 것 (도구 13개)

### 📋 타깃 조회 (1)
| 도구 | 쉽게 말하면 |
|---|---|
| `list_db_targets` | "분석 가능한 DB 목록 보여줘" |

### 📜 이력/저장소 (3)
| 도구 | 쉽게 말하면 | 엔진별 소스 |
|---|---|---|
| `check_query_store_enabled` | "쿼리 이력 녹화장치 켜져 있어?" | Query Store / pg_stat_statements / performance_schema |
| `get_top_queries` | "리소스(CPU/시간/IO) 제일 많이 먹은 쿼리 톱N" | mssql은 시간창 지원, pg/mysql은 누적 |
| `get_regressed_queries` | "예전보다 느려진 쿼리 찾아줘" | **mssql 전용** — 타 엔진은 대안 안내 반환 |

### 🔴 실시간 (4)
| 도구 | 쉽게 말하면 |
|---|---|
| `get_slow_queries` | "지금 이 순간 N초 넘게 돌고 있는 쿼리" |
| `get_blocking_sessions` | "누가 누구를 막고 있어?" (락 체인 — 가해자/피해자 쿼리) |
| `get_query_plan` | "이 쿼리 실행계획 보여줘" (플랜캐시 XML / EXPLAIN JSON) |
| `get_wait_stats` | "뭘 기다리느라 느린 거야?" (mssql: 쿼리별, pg: 스냅샷, mysql: 누적) |

### 🔧 인덱스/건강 (4)
| 도구 | 쉽게 말하면 |
|---|---|
| `suggest_indexes` | "인덱스 만들면 빨라질 곳" — mssql은 CREATE INDEX DDL까지, pg/mysql은 풀스캔 테이블 후보 |
| `get_index_usage` | "안 쓰는 인덱스 뭐야?" (삭제 후보) |
| `get_table_health` | pg: dead tuple/VACUUM, mysql: 단편화/OPTIMIZE 후보, mssql: 인덱스 단편화 |
| `get_connection_stats` | 세션 상태 분포 (idle in transaction 등) + max_connections |

### 📢 알림 (1)
`send_slack_notification` — 분석 결과를 Slack 채널로 발송 (명시적 요청 시에만)

### 🤝 A2A 협업 (내장 도구)
`ask_dbaops_agent` — OS/인프라 메트릭·Kafka·로그 등 쿼리 튜닝 밖 질문을
DBAOps 에이전트에 A2A로 위임하고 답을 인용. (반대로 DBAOps도 `ask_perf_agent`로 위임해옴)

### 할 수 없는 것
- DB 변경 실행 (인덱스 생성/쿼리 킬 — 권고문만 제시)
- OS/인프라 메트릭·로그 RCA (DBAOps 담당 — A2A로 자동 위임)
- pg/mysql의 시간구간별 성능 이력 (누적 통계만 — 도구가 제약을 정직하게 안내)

---

## 사용법

### Streamlit UI — https://dcc3of9o678kv.cloudfront.net (또는 :8502)
⚡ Query Performance 탭에서 질문. 🧭 탭은 DBAOps, 🔌 탭은 연동 상태.

### Slack — `@perfagent 질문` (토큰 설정 시)
스레드에서 멘션 없이 후속 질문 가능. 설정: [MANUAL.md](MANUAL.md) §4-1.

### CLI
```bash
VENV=/opt/dbaops/venv
$VENV/bin/python query_agent.py "mysql-poc 풀스캔 많은 테이블"   # 원샷
$VENV/bin/python query_agent.py                                  # 대화형
$VENV/bin/python connections.py status                           # 연동 상태
```

### A2A (다른 에이전트/스크립트에서)
Agent card: `http://127.0.0.1:9100/.well-known/agent-card.json`

---

## 사용례

### 1. 멀티엔진 한눈에
```
You> 등록된 DB 다 보여주고, 각각 상태 요약해줘
```
→ list_db_targets → 엔진별 check/connection_stats 순회 → 3개 DB 요약 리포트

### 2. MySQL 풀스캔 잡기 (실제 데모 검증됨)
```
You> mysql-poc 풀스캔 많은 테이블이랑 미사용 인덱스 봐줘
```
→ suggest_indexes + get_index_usage → "dbaops_orders 누적 1.3조 행 풀스캔, 지연 5.62일" 식의
실데이터 리포트 + 인덱스 후보 제시

### 3. PG 건강 점검
```
You> pg-test 상위 쿼리랑 vacuum 상태 분석해줘
```
→ get_top_queries + get_table_health → dead tuple %, autovacuum 이력 포함 리포트

### 4. SQL Server 회귀 조사
```
You> mssql 쪽에 어제부터 느려진 쿼리 있어?
```
→ get_regressed_queries (Query Store 시간창 비교) → 회귀율 상위 목록
(Query Store 꺼져 있으면 그 사실과 켜는 법 안내)

### 5. 장애 대응 (블로킹)
```
You> pg-test 지금 락 걸린 거 있는지 당장 확인해줘
```
→ get_blocking_sessions + get_slow_queries → 블로킹 체인(가해자/피해자 쿼리, 대기시간) 보고

### 6. 인프라와 교차 분석 (A2A)
```
You> mssql 쿼리는 네가 보고, 같은 시간대 호스트/인프라 상황은 DBAOps한테 물어서 종합해줘
```
→ 자기 도구로 쿼리 분석 + ask_dbaops_agent로 DBAOps 답변 인용 → 통합 리포트

### 7. 결과 Slack 공유
```
You> 방금 분석 WARNING으로 Slack에 보내줘
```
→ send_slack_notification (사전에 SLACK_BOT_TOKEN/SLACK_CHANNEL 설정 필요)

---

## 자주 쓰는 프롬프트

| 목적 | 예시 |
|---|---|
| 타깃 확인 | "분석 가능한 DB 뭐 있어?" |
| 상위 쿼리 | "pg-test에서 제일 비싼 쿼리 5개" |
| 실시간 | "mysql-poc 지금 느린 쿼리/블로킹 확인" |
| 플랜 | "orders 들어간 쿼리 실행계획 봐줘 (pg-test)" |
| 인덱스 | "mssql 누락 인덱스 추천, CREATE 문 포함" |
| 건강 | "pg-test 테이블 bloat 상태" / "mysql-poc 단편화" |
| 교차 | "쿼리는 네가, 인프라는 DBAOps한테 — 종합 리포트" |
