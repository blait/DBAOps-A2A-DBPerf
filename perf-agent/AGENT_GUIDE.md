# Query Performance Agent — 기능 및 사용 가이드

DBAOps-A2A-DBPerf 스택의 SQL Server 쿼리 성능 분석 에이전트.
Bedrock Claude(Sonnet 4.5)가 stdio MCP로 연결된 13개 진단 도구를 스스로 골라 호출하고,
결과를 해석해서 한국어/영어로 분석 리포트를 만들어 준다.

대상 DB: RDS SQL Server `sql-server-instance` (기본 분석 DB: `master`, `DB_NAME` 환경 변수로 변경)

---

## 이 에이전트가 할 수 있는 것

### 1. 쿼리 성능 진단 (Query Store 기반 — 과거 이력 분석)

Query Store가 켜져 있으면 시간 범위를 지정한 **과거 이력 분석**이 가능하다.

| 능력 | 사용하는 도구 | 설명 |
|---|---|---|
| Query Store 상태 확인 | `check_query_store_enabled` | 활성화 여부, 저장 용량, 캡처 모드. 에이전트가 항상 제일 먼저 호출 |
| 리소스 상위 쿼리 조회 | `get_query_store_top_queries` | CPU/실행시간/IO/메모리 기준 Top-N (기간 지정 가능) |
| 성능 회귀 감지 | `get_query_store_regressed_queries` | 최근 CPU가 과거 평균의 1.5배를 넘은 쿼리를 자동 탐지 |
| 쿼리별 대기 통계 | `get_query_store_wait_stats` | 어떤 쿼리가 어떤 대기(락, IO, CPU 등)로 느린지 분해 |
| 실행 이력 타임라인 | `get_query_execution_history` | 특정 query_id의 성능 추이 (기본 7일) — "언제부터 느려졌나" 답변용 |
| 실행 계획 요약 | `get_query_store_plan_summary` | 쿼리의 플랜 목록, 강제 플랜 여부, 플랜별 성능 비교 |

### 2. 실시간 진단 (DMV 기반 — Query Store 꺼져 있어도 동작)

| 능력 | 사용하는 도구 | 설명 |
|---|---|---|
| 지금 느린 쿼리 | `get_slow_queries` | 현재 실행 중이며 임계값(기본 5초) 이상 걸린 쿼리 |
| 블로킹 체인 | `get_blocking_sessions` | 누가 누구를 막고 있는지, 블로커/블록드 쿼리 텍스트와 대기 시간 |
| 플랜 캐시 검색 | `get_query_plan_from_cache` | 쿼리 텍스트 조각으로 실행 계획(XML) 검색 |
| 재시작 이후 비싼 쿼리 | `get_expensive_queries_from_cache` | 플랜 캐시 누적 통계 기준 Top-N (CPU/시간/읽기/쓰기) |

### 3. 최적화 권고

| 능력 | 사용하는 도구 | 설명 |
|---|---|---|
| 누락 인덱스 추천 | `suggest_indexes` | DMV 기반 누락 인덱스 + **바로 실행 가능한 CREATE INDEX 문 생성** (테이블 필터 가능) |
| 인덱스 사용 현황 | `get_index_usage` | UNUSED(안 쓰임) / EXPENSIVE(유지비용 과다) / USED 분류 |

### 4. 알림

| 능력 | 사용하는 도구 | 설명 |
|---|---|---|
| Slack 알림 발송 | `send_slack_notification` | Bot Token(chat.postMessage)으로 발송. 심각도 INFO/WARNING/CRITICAL. **사용자가 명시적으로 요청할 때만** 보내도록 프롬프트에 제한됨 |

### 에이전트의 동작 방식

1. 질문을 받으면 **Query Store 활성화 여부를 먼저 확인**
2. 켜져 있으면 이력 기반 도구, 꺼져 있으면 DMV 실시간 도구로 자동 전환 (제약도 함께 안내)
3. 필요한 도구를 조합 호출 → 결과를 해석 → 구조화된 리포트로 응답
   (Query Store 상태 / 분석 기간 / 상위 쿼리 / 발견된 문제 / 최적화 권고 / 액션 아이템)

### 할 수 없는 것 (범위 밖)

- DB 변경 작업 — 인덱스를 직접 생성하거나 쿼리를 죽이지 않음 (권고문만 생성, 실행은 사람 몫)
- CPU/메모리/스토리지 등 인스턴스 레벨 메트릭 — CloudWatch/Performance Insights 도구는 이 번들에 없음 (health-tools는 별도)
- 보안 감사, 백업/스토리지 수명주기 — 다른 에이전트 영역
- Performance Insights — 대상 인스턴스에 PI가 비활성화되어 있기도 함

---

## 사용법

```bash
# Streamlit UI
open http://<host>:8502

# CLI (venv)
VENV=/opt/dbaops/venv
$VENV/bin/python query_agent.py            # 대화형
$VENV/bin/python query_agent.py "지난 24시간 CPU 상위 쿼리 5개 보여줘"
```

분석 대상 DB를 바꾸려면 `/etc/dbaops/dbaops.env`의 `DB_NAME` 수정 후 `sudo systemctl restart dbperf-a2a`.

---

## 사용례

### 사용례 1: 일상 성능 점검

```
You> 지난 24시간 동안 CPU를 가장 많이 쓴 쿼리 5개를 보여주고, 문제가 있으면 알려줘
```

에이전트 동작: `check_query_store_enabled` → `get_query_store_top_queries(hours_back=24, top_n=5, metric="cpu")`
→ CPU ms, 실행 횟수, 쿼리 텍스트를 표로 정리하고 비정상 패턴(예: 평균 CPU가 수 초인 쿼리)을 짚어줌.

### 사용례 2: "갑자기 느려졌어요" 회귀 조사

```
You> 어제부터 애플리케이션이 느려졌다는 신고가 있어. 성능이 나빠진 쿼리가 있는지 찾아줘
```

에이전트 동작: `get_query_store_regressed_queries(hours_back=24)` → 회귀율(%) 상위 쿼리 목록
→ 특정 쿼리가 나오면 `get_query_execution_history(query_id=...)`로 언제부터 나빠졌는지 타임라인 확인
→ `get_query_store_plan_summary(query_id=...)`로 플랜이 바뀌었는지(플랜 회귀) 진단.

### 사용례 3: 지금 발생 중인 장애 대응 (블로킹/느린 쿼리)

```
You> 지금 DB가 멈춘 것 같아. 블로킹이나 오래 걸리는 쿼리가 있는지 당장 확인해줘
```

에이전트 동작: `get_blocking_sessions` + `get_slow_queries(threshold_seconds=5)` 동시 확인
→ 블로킹 체인(세션 A가 B를 몇 초째 막는 중, 각 쿼리 텍스트)과 장기 실행 쿼리를 보고
→ KILL할 세션 후보를 제안 (실행은 하지 않음).

### 사용례 4: 인덱스 최적화 작업

```
You> Orders 테이블에 필요한 인덱스 추천해주고, 반대로 안 쓰는 인덱스도 알려줘
```

에이전트 동작: `suggest_indexes(table_name="Orders")` + `get_index_usage`
→ 개선 효과 점수 순으로 CREATE INDEX 문을 그대로 복사해 쓸 수 있게 제시하고,
UNUSED/EXPENSIVE 인덱스는 DROP 검토 대상으로 분리해서 보고.

### 사용례 5: 특정 쿼리 심층 분석

```
You> sp_CustomerOrderSummary 관련 쿼리의 실행 계획을 찾아서 왜 느린지 분석해줘
```

에이전트 동작: `get_query_plan_from_cache(query_fragment="sp_CustomerOrderSummary")`
→ 플랜 XML에서 테이블 스캔, 암시적 변환, 스칼라 UDF 같은 안티패턴을 찾아 원인을 설명하고 재작성 방향 제안.

### 사용례 6: Query Store가 꺼진 DB 점검

```
You> 이 DB 성능 분석 좀 해줘
```

에이전트 동작: `check_query_store_enabled` → 꺼져 있음을 확인하고 안내
→ 자동으로 DMV 모드로 전환: `get_expensive_queries_from_cache` + `get_slow_queries`
→ "재시작 이후 누적 데이터 기준" 이라는 제약을 명시하고, Query Store 활성화를 권고
(`ALTER DATABASE ... SET QUERY_STORE = ON` 문 포함).

### 사용례 7: 점검 결과 Slack 발송

```
You> 오늘 성능 점검 결과를 요약해서 WARNING 등급으로 Slack에 보내줘
```

에이전트 동작: 위 분석 도구들 실행 후 `send_slack_notification(message=..., severity="WARNING")`
→ Slack 채널로 발송. (사전 설정: `.env`의 `SLACK_BOT_TOKEN`/`SLACK_CHANNEL` + 채널에 `/invite @DBAOps`)

### 사용례 8: 에이전트 없이 도구만 직접 사용 (MCP 클라이언트)

Claude Code 등 MCP 클라이언트를 EC2에서 쓸 때 `.mcp.json`:

```json
{
  "mcpServers": {
    "dbops-query-tools": {
      "command": "python3",
      "args": ["/path/to/perf-agent/mcp_query_tools.py"],
      "env": {
        "AWS_REGION": "ap-northeast-2",
        "DB_SECRET_ID": "dbops-sqlserver-secret",
        "DB_NAME": "DBOpsLab"
      }
    }
  }
}
```

스크립트에서 프로그래밍 방식으로 도구 호출:

```python
import asyncio
from mcp import ClientSession, StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(
        command="python3",
        args=["/path/to/perf-agent/mcp_query_tools.py"],
        env={"AWS_REGION": "ap-northeast-2", "DB_SECRET_ID": "dbops-sqlserver-secret", "DB_NAME": "DBOpsLab"},
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            result = await s.call_tool("get_query_store_top_queries",
                                       {"hours_back": 24, "top_n": 5, "metric": "cpu"})
            print(result.content[0].text)

asyncio.run(main())
```

### 사용례 9: 부하 실습 시나리오와 결합 (이 저장소의 load_generator 활용)

```bash
# 1. 부하 생성 (별도 터미널) — DBOpsLab에 고CPU 프로시저 워크로드 시작
./start_benchmark.sh 60

# 2. 에이전트로 진단
DB_NAME=DBOpsLab /opt/dbaops/venv/bin/python query_agent.py "CPU가 높은 원인 쿼리를 찾고 인덱스 개선안을 만들어줘"

# 3. 에이전트가 추천한 CREATE INDEX 적용 (또는 04_create_indexes_fix.sql 실행)

# 4. 개선 확인
DB_NAME=DBOpsLab /opt/dbaops/venv/bin/python query_agent.py "인덱스 적용 후 성능이 개선됐는지 회귀 쿼리 기준으로 비교해줘"
```

---

## 자주 쓰는 프롬프트 모음

| 목적 | 프롬프트 예시 |
|---|---|
| 전체 점검 | "지난 24시간 쿼리 성능 전반적으로 점검해줘" |
| CPU 원인 | "CPU 사용량 기준 상위 쿼리 10개와 각각의 개선 방법 알려줘" |
| IO 원인 | "논리 읽기가 많은 쿼리를 찾아서 인덱스로 해결 가능한지 봐줘" |
| 회귀 | "최근 48시간 내 성능이 나빠진 쿼리 있어?" |
| 블로킹 | "블로킹 세션 확인해줘" |
| 대기 분석 | "query_id 42가 뭘 기다리느라 느린지 대기 통계 봐줘" |
| 인덱스 | "누락 인덱스 추천 전부 보여줘. CREATE 문 포함해서" |
| 정리 | "안 쓰는 인덱스 찾아줘. 삭제해도 되는지 판단 근거도" |
| 알림 | "방금 분석 결과를 CRITICAL로 Slack에 보내줘" |
