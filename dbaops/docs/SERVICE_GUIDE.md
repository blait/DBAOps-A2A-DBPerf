# DBAOps-Agent — 서비스 가이드

DB / 인프라 분석을 자동화하는 에이전트 서비스. 자연어로 질문하면 AI 분석가가 도구를 직접 골라 호출하고 답한다.

> **⚠️ 아키텍처 변경 안내 (2026-06)**
>
> 현재 배포 방식은 **올인원 EC2 + docker compose** (AgentCore/Gateway/Lambda 없음).
> - 배포 가이드: [`deploy/ec2-allinone/README.md`](../deploy/ec2-allinone/README.md)
> - 통합 온보딩: [`docs/ONBOARDING.md`](ONBOARDING.md)
> - Slack 연동: [`deploy/ec2-allinone/SLACK_SETUP.md`](../deploy/ec2-allinone/SLACK_SETUP.md)
>
> 아래 문서의 AgentCore Runtime/Gateway/Cognito/Lambda 설명은 **초기 PoC 시절 구조**이며,
> 현재 코드 동작을 이해하는 데는 참고용으로만 활용할 것.
> 현행 핵심 코드: `mcp_router/`, `agent/src/dbaops_agent/single_graph.py`, `slack_bot/bot.py`.

본 문서는 코드를 그대로 옮긴 가이드다. 추측 없이 실제 파일·라인·등록된 도구만 적었다.

---

## 0. 시작하기 전에 — 대단히 중요한 한 가지 오해 풀기

여러 번 등장할 "도메인 에이전트 3 개" 라는 말 때문에 헷갈리기 쉬운데:

> **도메인 에이전트 3 개는 agent 컨테이너 안에서 메모리에 만들어지는 Python 객체**일 뿐, 별도 서비스나 별도 Lambda / ECS task 가 아니다. 그리고 이 객체들은 **pipeline 모드에서만** 쓰이는데, pipeline 모드는 현재 **UI 에 노출되지 않는다** (API 로 `mode=pipeline` + `domain` 지정 시에만 동작). 주력은 에이전트 1 개짜리 **single 모드**다.

비유하면 — Python 프로세스 하나 안에 `os_agent = ReactAgent(...)` , `db_agent = ReactAgent(...)`, `log_agent = ReactAgent(...)` 변수 3 개 가지고 있는 셈 (pipeline 모드 한정).

현행 배포 단위 (올인원 EC2 + docker compose):

| 항목 | 실제 배포 단위 수 |
|---|---|
| docker compose 컨테이너 | **4** — `mcp-router`(:9000) / `agent`(:8080) / `streamlit`(:8501) / `slack-bot` |
| 옵션 컨테이너 (`--profile prometheus`) | **4** — prometheus / postgres-exporter / mysqld-exporter / node-exporter |
| 도메인 에이전트 (Python 객체) | agent 컨테이너 메모리 안에 **3** 개 (pipeline 모드 한정, UI 미노출) |
| Lambda / AgentCore 리소스 | **0** — `mcp_tools/*/handler.py` 는 mcp-router 가 직접 import (`mcp_router/custom_tools.py`) |

이 한 가지만 머릿속에 두고 읽으면 나머지가 쉬워진다.

---

## 1. 한눈에 — 컴포넌트 지도

현행 구성 — 올인원 EC2 한 대 위의 docker compose (`deploy/ec2-allinone/docker-compose.yml`):

```
┌──────────────────────┐        ┌──────────────────────┐
│ 사용자 (브라우저)      │        │ 사용자 (Slack)        │
└──────────┬───────────┘        └──────────┬───────────┘
           │                               │ Socket Mode (outbound WS)
           ▼                               ▼
┌──────────────────────┐        ┌──────────────────────┐
│ streamlit  (:8501)   │        │ slack-bot            │
│  ui/streamlit/app.py │        │  slack_bot/bot.py    │
│  탭: 🤖 DBAOps Agent  │        │  thread = session    │
│     + 🔌 MCP 연결설정 │        │                      │
└──────────┬───────────┘        └──────────┬───────────┘
           │ HTTP POST /invocations (AGENT_HTTP_URL — compose 내부, 인증 없음)
           └───────────────┬───────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│ agent  (:8080)                                             │
│   메모리 안:                                               │
│     · single_graph    (주력 1-에이전트 — UI·Slack 공용)     │
│     · pipeline_graph  (LangGraph 4 노드 — API 전용,        │
│         os_metric/db_metric/log 도메인 에이전트 객체 3개)   │
└──────────────────────────┬─────────────────────────────────┘
                           │ MCP JSON-RPC (인증 없음, compose 내부)
                           ▼
┌────────────────────────────────────────────────────────────┐
│ mcp-router  (:9000)  — 구 AgentCore Gateway 역할            │
│   tools/list 로 56개 도구 노출 (10 타깃)                    │
│   · 커스텀 4 타깃: mcp_tools/*/handler.py 직접 import       │
│     (rds-pi / msk-metrics / s3-log-fetch / aws-api)        │
│   · stdio 6 타깃: awslabs cloudwatch/aws-doc/aws-api,      │
│     community prometheus/postgres/mysql (서브프로세스)      │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
         ┌─────────────────────────────────────┐
         │ 분석 대상 인프라                      │
         │   Aurora PostgreSQL · RDS MySQL      │
         │   MSK Serverless · Prometheus        │
         │   S3 logs bucket                     │
         └─────────────────────────────────────┘

(+ 옵션: docker compose --profile prometheus 로 prometheus /
 postgres-exporter / mysqld-exporter / node-exporter 4개 추가 기동)
```

Streamlit 은 `SHOW_GENERATORS=true` 일 때만 "🧪 시나리오 라이브 모니터" 탭이 추가된다.

---

## 2. 모드 — 어떤 그래프를 돌릴까

런타임은 세 가지 모드를 받는다 (`agent/src/dbaops_agent/runtime_entry.py:23`).

| 모드 | 동작 | 언제 쓰나 |
|---|---|---|
| **single** | 모든 도구 풀을 한 에이전트가 직접 사용 | **주력 — UI·Slack 공용** |
| pipeline | 도메인 에이전트 → 검증 → (필요 시 재분석) → 리포트 4 단계 | 코드에 유지, **API 로만 사용 가능** (`mode=pipeline` + `domain` 지정 필요), **UI 미노출** |
| swarm | (제거됨) — 옛 supervisor/specialist | 옛 클라이언트가 호출하면 명시적 에러 반환 |

### 2-1. Pipeline 그래프 — 4 단계로 분리한 이유

LLM 한테 "분석부터 검증까지 한 번에 해" 라고 시키면, 자기가 쓴 답을 객관적으로 점검 못 한다 (사람도 마찬가지). 그래서 **단계를 분리** 했다:

```
START
  │
  ▼
┌──────────────────┐
│ domain_agent     │  분석. 도구 호출 ReAct loop 로 자유롭게.
│                  │  결과: domain_response (문장)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ validation       │  방금 답을 다른 LLM 인스턴스가 검사.
│                  │  도구 인용 누락 / 추측을 단언처럼 / 수치 모순 — 3 종류만 본다.
└────┬─────────┬───┘
     │ pass    │ fail
     │         │
     │         ▼
     │   ┌──────────────┐
     │   │ revise (1회)  │  지적 받은 문제만 고치라고 같은 도메인 에이전트에 다시 시킴.
     │   └──────┬───────┘
     │          │
     ▼          ▼
┌──────────────────┐
│ report           │  사용자에게 보여줄 markdown 작성.
│                  │  fenced ```json-chart 블록으로 어떤 차트를 그릴지 명시.
└────────┬─────────┘
         │
         ▼
        END
```

코드 위치:
- 노드 등록: `pipeline_graph.py:345-355`
- 분기 함수 `_route_after_validation`: `pipeline_graph.py:329-335`

### 2-2. 각 노드가 하는 일

| 노드 | 입력 | 출력 | 코드 |
|---|---|---|---|
| `domain_agent` | 사용자 질문 + 시간 윈도 | 도구 호출 history + 최종 답변 텍스트 | `_domain_node` (`pipeline_graph.py:133`). LangGraph `create_react_agent` 사용. 한 번 invoke 안에 도구 60회까지 호출 가능. |
| `validation` | domain_agent 의 답변 + 도구 history | `{passed: bool, issues: [...]}` JSON | `_validation_node` (`pipeline_graph.py:148`). 도구 없이 LLM 1회 호출. |
| `revise` | validation 의 issues 목록 | 갱신된 답변 (같은 도메인 에이전트가 재작성) | `_revise_node` (`pipeline_graph.py:244`). **1회만**. |
| `report` | 최종 답변 + 도구 history + 검증 결과 | markdown + chart spec 리스트 | `_report_node` (`pipeline_graph.py:292`). 도구 없이 LLM 1회. |

### 2-3. 그래프의 메모리 (State)

`pipeline_graph.py:86`:

```python
class PipelineState(TypedDict, total=False):
    domain:           str           # "os_metric" / "db_metric" / "log"
    user_text:        str           # 사용자 원본 질문
    domain_messages:  list[...]     # 도메인 에이전트의 도구 호출 history
    domain_response:  str           # 최신 답변 (revise 후엔 갱신됨)
    validation:       dict          # {"passed": bool, "issues": [...]}
    revise_count:     int           # 0 → 1 까지 (1회 한정)
    report_markdown:  str
    report_charts:    list[dict]
```

세션 격리는 thread_id 로 한다:
```
thread_id = f"pipeline:{domain}:{session_id}"
```
같은 도메인 + 같은 session 끼리만 history 가 공유됨. 도메인이 다르거나 session 이 다르면 완전 분리.

---

## 3. 도메인 에이전트 (3 개) — 자세히

### 3-1. "에이전트 객체 3 개" 의 정확한 의미

다시 강조: **agent 컨테이너는 한 개**, 그 안 메모리에 LangGraph 가 만들어준 ReAct agent 객체가 도메인별로 1 개씩 = 총 3 개. 이게 "도메인 에이전트 3 개" 의 의미다 (pipeline 모드 한정 — UI 미노출, §2 참조).

생성 코드 (`pipeline_graph.py:103`):

```python
_DOMAIN_AGENTS_CACHE: dict[str, Any] = {}        # 모듈 전역 빈 dict

def _get_domain_agent(domain_key: str):
    if domain_key in _DOMAIN_AGENTS_CACHE:
        return _DOMAIN_AGENTS_CACHE[domain_key]   # 두 번째 호출부터는 즉시 반환
    tools = build_mcp_tools(max_response_chars=12000)   # MCP 카탈로그 자동 로드
    sys_prompt = _domain_system_prompt(domain_key)      # 도메인별 prompt 조합
    agent = create_react_agent(                         # LangGraph 가 그래프 객체 만듦
        model=get_llm(),
        tools=tools,
        prompt=SystemMessage(content=sys_prompt),
        name=f"{domain_key}_agent",
    )
    _DOMAIN_AGENTS_CACHE[domain_key] = agent
    return agent
```

순서:
1. dict 에 이미 있으면 → 그대로 반환 (캐시 히트, 거의 즉시)
2. 없으면 → MCP `tools/list` 호출, prompt .md 읽기, ReAct 그래프 컴파일 → dict 에 저장
3. 두 번째 같은 도메인 요청부터는 1번으로 끝

→ 결과: **컨테이너당 도메인별 빌드 1회만**. 같은 컨테이너의 같은 도메인이 100번 들어와도 build 는 1번.

### 3-2. 도구 풀 — 모든 도메인이 똑같이 56개 도구를 본다

세 도메인 모두 `build_mcp_tools(max_response_chars=12000)` 하나로 끝. 도구 접근 권한으로는 도메인을 안 나눈다.

> **그러면 도메인 분리가 무슨 의미가 있나?** → 권한이 아니라 **system prompt 의 "이 도메인은 이런 책임이 있다" 안내**로 LLM 이 자기 영역 안에서 답을 만들도록 유도. 도메인 경계를 넘어야 할 때 (db 분석 중 호스트 CPU 도 봐야 함) 는 자유롭게 다른 도구 호출 가능.

### 3-3. 시스템 프롬프트는 어떻게 만들어지나

`agent/src/dbaops_agent/prompts/` 디렉토리에 .md 파일 7 개:

| 파일 | 용도 |
|---|---|
| `_common.md` | 모든 도메인 prompt 에 prepend 되는 공통 RCA 룰 |
| `domain_os_metric.md` | os_metric 도메인 안내 |
| `domain_db_metric.md` | db_metric 도메인 안내 |
| `domain_log.md` | log 도메인 안내 |
| `validation.md` | 검증 노드의 시스템 프롬프트 |
| `revise.md` | 검증 fail 시 재분석 지시 템플릿 |
| `report.md` | 리포트 노드의 시스템 프롬프트 |

조립 (`pipeline_graph.py:51`):

```python
def _domain_system_prompt(domain_key: str) -> str:
    common = _read("_common.md").format(**infra_context())   # 인프라 식별자 채움
    domain_tpl = _read(f"domain_{domain_key}.md")             # 도메인 prompt
    return domain_tpl.format(common=common)                   # {common} 자리에 끼움
```

`infra_context()` 가 환경변수에서 `prom_instance_id`, `aurora_writer_id`, `mysql_db_id` 등을 읽어와 `_common.md` 의 `{prom_instance_id}` 같은 placeholder 를 채운다. 그 결과를 `domain_*.md` 의 `{common}` 자리에 끼운다.

→ 최종 system prompt = 도메인 전용 안내(짧음) + 공통 RCA 룰(`_common.md`, 정보 식별자 채워짐).

### 3-4. 도메인별 책임이 어떻게 다른가 — 한눈에

| 도메인 | 핵심 영역 | 자주 쓰는 도구 |
|---|---|---|
| **os_metric** | EC2 호스트와 RDS/EC2 호스트 메트릭 (CPU/MEM/IO/Network) 의 추세·이상 | `prometheus_*` (호스트 OS), `cloudwatch_*` (AWS 관리형) |
| **db_metric** | Aurora PG / RDS MySQL / MSK Kafka 의 내부 성능 (TPS·QPS·Lock·Cache·Lag·ISR) | `execute_sql / mysql_query / analyze_db_health / get_top_queries / rds_performance_insights / msk_metrics` |
| **log** | 로그 패턴 분류·빈도·RCA 후보 (S3 .gz / RDS engine logs / CloudWatch Logs Insights) | `s3_list_logs → s3_log_fetch`, `describe_db_log_files → download_db_log_file_portion`, `describe_log_groups → execute_log_insights_query` |

각 도메인 prompt 의 원문은 [§ 부록 A](#appendix-a-prompts) 참조.

### 3-5. 검증 노드는 정확히 무엇을 보나

`validation.md` 가 명시하는 **3 가지 실패 모드만** 본다 (다른 건 안 봄):

1. **missing_citation** — 구체적 단언인데 도구 결과 인용이 없는 경우
   - ❌ "DB load is high" (인용 없음)
   - ✅ "CPU was 92% during 14:02–14:07 (cloudwatch_metric AWS/RDS)"

2. **flat_speculation** — 추측인데 hedging 없이 단언처럼 쓴 경우
   - ❌ "이 문제는 인덱스 부재 때문이다."
   - ✅ "인덱스 부재로 인한 풀스캔이 의심된다 (likely, mid). 검증: EXPLAIN on dbaops_orders.user_id"

3. **contradiction** — 같은 답변 안 또는 도구 결과와 수치가 모순
   - ❌ "CPU 정상 범위" 단언 + 다른 줄에서 "CPU 92% peak" 언급

출력 형태는 JSON 한 객체로 강제:
```json
{
  "passed": true | false,
  "issues": [{"kind": "missing_citation|flat_speculation|contradiction", "detail": "<인용 + 사유>"}]
}
```

LLM 이 가끔 이 형식을 못 지킬 때를 대비해 `_parse_validation_json` (`pipeline_graph.py:175`) 가 첫 `{...}` 블록만 추출해 try/except. 파싱 실패하면 over-strict 회피를 위해 `passed=true` 로 처리.

### 3-6. 재분석 (revise) — 1회 한정

검증이 fail 떨어지면 revise 노드로. 같은 도메인 에이전트에게:
- 기존 도구 history 그대로 + revise 지시 prompt 를 user 메시지로 prepend
- 새 도구 호출이 필요하면 호출 OK
- 중복 도구 호출은 금지 (기존 결과 활용)
- 인용이 부족한 단언 → 인용 보강 또는 가설로 전환
- 수치 모순 → 도구 결과로 확인 후 한쪽으로 통일

`revise_count` 가 1 이 되면 다시 fail 떨어져도 revise 안 함 → report 로 직행 (보고서에 "검증 미통과 항목 남음" 경고 prepend).

### 3-7. 리포트 — markdown + 차트 명세

리포트 노드는 도구 없이 LLM 1번 호출. 다음을 받는다:
- 사용자 질문
- (검증 통과한) 도메인 에이전트 최종 답변
- 시계열 데이터를 만든 도구 호출들의 압축 요약 (`tool_call_id` + 결과 sample)

출력은 **markdown 한 덩어리**. 5 섹션 구조:

```
## 분석 요약       ← 한 단락
## 핵심 발견        ← bullet 3~6 개, 도구 인용 필수
## 시각화          ← fenced ```json-chart 블록 최대 3개
## 가설과 검증 방법
## 권고             ← 비파괴적 다음 행동
```

차트 명세는 다음 6 종류 중 LLM 이 선택:

```json
{
  "chart_type":          "line | bar | scatter | histogram | area | table",
  "title":               "<짧은 한국어 제목>",
  "source_tool_call_id": "<도구 history 의 tool_call_id 중 하나>",
  // chart_type 별 추가 필드:
  "metric_filter":  ["substring", ...],          // line / area
  "x_field":        "top_sql[*].label",          // bar / scatter (dotted-path)
  "y_field":        "top_sql[*].aas",
  "top_n":          10,                          // bar
  "field":          "series[*].value",           // histogram
  "bins":           20,
  "rows_field":     "top_sql",                   // table
  "columns":        ["label","aas"]
}
```

`source_tool_call_id` 가 핵심 — Streamlit UI 가 메시지 history 에서 이 id 의 도구 결과를 찾아내 데이터를 추출, dotted-path (`top_sql[*].aas` 처럼 `[*]` 와 `[N]` 모두 지원) 로 필요 부분만 잘라 chart_type 에 맞춰 렌더.

UI 의 chart_type 분기는 `view_swarm.py:_render_one_chart` (라인 545-) 에 있고 `streamlit.line_chart / bar_chart / area_chart / scatter_chart / dataframe` 을 직접 호출.

---

## 4. MCP 도구 — 자동 노출

### 4-1. 가장 큰 한 가지 결정

> **LLM 이 보는 도구 description 은 우리가 안 쓴다. MCP 서버가 자기 자신을 직접 설명한다.**

옛날엔 우리가 `@tool def mysql_query(sql: str): "..."` 처럼 LangChain wrapper 38 개를 손으로 작성했고, 그 docstring 이 LLM 한테 노출됐다. 우리가 추측해서 적은 cheat-sheet 가 틀린 케이스가 발견됐다 (예: MySQL EXPLAIN ANALYZE 거부). 그래서 다 버리고 MCP 서버 자체 description 만 쓰기로 했다.

### 4-2. 동작 흐름

`agent/src/dbaops_agent/tools/mcp_auto.py` (160줄) 의 `build_mcp_tools()` 가:

1. `MCPClient.list_tools()` 를 부른다.
   - 내부적으로 mcp-router 의 `tools/list` JSON-RPC 호출
   - cursor 페이지네이션으로 다 모음 (`mcp_client.py:list_tools`)
2. 각 도구 spec 의 `inputSchema` (JSON Schema) → `pydantic.create_model` 로 args model 동적 생성
3. `StructuredTool.from_function` 으로 LangChain Tool 만듦
4. invoker 는 thin wrapper — 호출 받으면 None 인자 제거 + `MCPClient.call(...)` + 응답 12k 자 truncate (`mcp_auto._truncate` — 시계열 응답은 자르지 않고 **JSON 구조를 보존한 다운샘플링**으로 12,000자 이내로 줄인다. `normalize_message` 는 13,000자 캡.)

도구 이름 변환 — mcp-router 가 namespacing 한 `community-mysql___mysql_query` 를 LangChain 호환 식별자 `community_mysql__mysql_query` 로 (`_safe_tool_name`).

(구 Gateway 가 자동 끼워넣던 검색 도구 `x_amz_bedrock_agentcore_search` 는 여전히 자동 제외 — `_BUILTIN_TOOLS_TO_SKIP`.)

### 4-3. 언제 도구 카탈로그를 가져오나

컨테이너 cold start 후 **첫 invoke 시 1회만**. `_TOOLS_CACHE` 또는 `_DOMAIN_AGENTS_CACHE` 가 모듈 전역이라 같은 컨테이너 안에선 재호출 안 함. 컨테이너 교체(재배포 등) 시 다시 1회.

도구 description 을 바꿨는데 컨테이너가 안 죽었으면 반영 안 됨 → `docker compose build agent && docker compose up -d agent` 로 agent 컨테이너 교체 필요 (§9).

### 4-4. 등록된 target 10 개와 도구 수

`mcp_router/registry.py` (커스텀 4 + stdio 6) 와 `mcp_tools/<dir>/tool_io.json` 기준:

| Target | 출처 | 도구 수 | 주요 도구 |
|---|---|---|---|
| `rds-pi` | 우리 PoC | 1 | `rds_performance_insights` (DBInstanceIdentifier auto-resolve) |
| `msk-metrics` | 우리 PoC | 1 | `msk_metrics` (Cluster Name + Topic + Consumer Group dim auto-wiring) |
| `s3-log-fetch` | 우리 PoC | 2 | `s3_list_logs`, `s3_log_fetch` |
| `aws-api` | 우리 PoC | 11 | `describe_rds_instances/_clusters`, `describe_db_log_files`, `download_db_log_file_portion`, `describe_rds_events`, `describe_db_recommendations`, `list_msk_clusters`, `describe_ec2_instances`, `pi_create_analysis_report`, `pi_get_analysis_report`, `describe_pi_dimensions` |
| `awslabs-cloudwatch` | awslabs | 19 | `get_metric_data`, `execute_log_insights_query`, `get_active_alarms`, `analyze_metric` 등 |
| `awslabs-aws-doc` | awslabs | 4 | `search_documentation`, `read_documentation`, `recommend` |
| `awslabs-aws-api` | awslabs | 2 | `call_aws`, `suggest_aws_commands` (READ_OPERATIONS_ONLY=true) |
| `community-prometheus` | pab1it0 | 6 | `execute_query`, `execute_range_query`, `list_metrics`, `get_metric_metadata` |
| `community-postgres` | crystaldba | 9 | `execute_sql`, `explain_query`, `analyze_db_health`, `get_top_queries`, `analyze_workload_indexes`, `list_schemas`, `list_objects`, `get_object_details`, `analyze_query_indexes` |
| `community-mysql` | benborla | 1 | `mysql_query` |

총 **56** 개. 자동 빌드 시 LLM 한테 모두 노출.

### 4-5. PoC 특화 변환은 어디로 갔는가

수동 wrapper 가 사라졌으니 변환 로직은 **백엔드 handler** (`mcp_tools/*/handler.py` — mcp-router 가 직접 import) 에 들어있다:

| 변환 | 위치 |
|---|---|
| `rds_performance_insights` 의 `db_id` 가 DBInstanceIdentifier 면 RDS DescribeDBInstances 로 DbiResourceId 자동 변환 | `mcp_tools/rds_pi/handler.py:_resolve_dbi_resource_id` |
| `rds_performance_insights` 의 `group_by` 가 dimension full-name 이면 prefix 로 truncate | `mcp_tools/rds_pi/handler.py:_normalize_group` |
| `msk_metrics` 의 메트릭별 dimension 자동 wiring (BytesIn → Topic, MaxOffsetLag → Topic+ConsumerGroup) | `mcp_tools/msk_metrics/handler.py:_build_dimensions` |
| MySQL `EXPLAIN ANALYZE` / `EXPLAIN FORMAT=` 거부 사실은 도메인 prompt 에 명시 | `prompts/domain_db_metric.md` |
| AWS API 응답 정제 (RDS/EC2/MSK 메타) | `mcp_tools/aws_api/handler.py` |
| S3 listing-first + gz 디코딩 | `mcp_tools/s3_log_fetch/handler.py` |

### 4-6. 각 도구가 붙을 대상(연결정보)은 어디서 오나

PoC 는 terraform 이 testbed 자기참조로 자동 주입, 고객 환경(올인원 EC2)은 연결설정 UI 가
instance role 로 탐색해 드롭박스 제공 + DB 자격증명만 사람이 입력 — 상세는
[`CONNECTION_INFO.md`](CONNECTION_INFO.md).

---

## 5. AgentCore 구성 (⚠️ legacy — 현행 배포에서는 미사용, 문서 상단 안내 참조)

### 5-1. Runtime — 컨테이너 1 개의 정체

| 속성 | 값 |
|---|---|
| 이름 | `dbaops_poc` |
| ID | `dbaops_poc-IHXuy85IwY` |
| 컨테이너 이미지 | `<account>.dkr.ecr.<region>.amazonaws.com/dbaops-agent:latest` |
| Network mode | `PUBLIC` |
| 모델 | `global.anthropic.claude-opus-4-7` (`agent/src/dbaops_agent/llm.py:13`) |
| 모델 옵션 | `max_tokens=4096`, temperature 미설정 (Opus 4.7 거부) |
| Role | `dbaops-poc-agentcore-runtime` |

엔트리포인트 (`agent/Dockerfile:18`):
```
CMD ["python", "-m", "dbaops_agent.runtime_entry"]
```

`runtime_entry.serve()` 가 `ThreadingHTTPServer` 로 `:8080/ping`, `:8080/invocations` 처리. POST `/invocations` 에서 `Accept: application/x-ndjson` 또는 `request.stream=true` 면 NDJSON streaming, 아니면 동기 JSON 응답.

런타임 환경변수 (terraform output 으로 주입):
- `BEDROCK_MODEL_ID`, `BEDROCK_REGION`
- `GATEWAY_ENDPOINT`, `COGNITO_TOKEN_URL`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`, `COGNITO_SCOPE`
- `INFRA_PROM_INSTANCE_ID`, `INFRA_AURORA_WRITER_ID`, `INFRA_AURORA_READER_ID`, `INFRA_AURORA_CLUSTER_ID`, `INFRA_MYSQL_DB_ID`, `INFRA_MSK_CLUSTER_NAME`, `INFRA_LOG_BUCKET`
- `TOOL_BUDGET=128`, `LOG_LEVEL=INFO`, `DBAOPS_IGNORE_BUDGET=1`

### 5-2. Gateway

| 속성 | 값 |
|---|---|
| 이름 | `dbaops-poc` |
| MCP endpoint | `https://dbaops-poc-tjefplfunu.gateway.bedrock-agentcore.ap-northeast-2.amazonaws.com/mcp` |
| 인증 | Cognito JWT (client_credentials flow) |
| Role | `dbaops-poc-agentcore-gateway` |

10 개 Lambda target 등록 (`scripts/register_gateway_targets.py:_TOOL_TARGETS`). target 이름은 `<dash-case>` (예: `community-mysql`), Gateway 가 도구 노출 시 `<target>___<tool>` 형태로 namespacing 한다.

### 5-3. Cognito

| 리소스 | 위치 |
|---|---|
| User pool | `dbaops-poc` (`infra/modules/agentcore/main.tf:25`) |
| Resource server | `dbaops-gateway` with scope `invoke` (라인 48) |
| Client | `dbaops-poc-streamlit` — `client_credentials` flow, secret 발급 (라인 59) |
| Domain | `dbaops-poc-ap-northeast-2.auth.<region>.amazoncognito.com` |

토큰 URL: `https://<domain>/oauth2/token`. Agent 컨테이너의 `MCPClient._CognitoTokenProvider` (`mcp_client.py:37`) 가 만료 30 초 전 자동 갱신.

---

## 6. 인프라 (Terraform) (⚠️ legacy — PoC testbed 프로비저닝용, 현행 배포는 `deploy/ec2-allinone/` 참조)

`infra/modules/` 의 11 개 모듈, `infra/envs/poc/main.tf` 에서 호출:

| 모듈 | 역할 |
|---|---|
| `network` | VPC + 2 AZ private/public subnet + NAT |
| `iam` | MCP Lambda base role (RDS/PI/CloudWatch/MSK/S3 RO 권한) |
| `s3_logs` | 로그 적재용 S3 bucket |
| `ec2_prometheus` | self-hosted Prometheus + node_exporter EC2 |
| `aurora_postgres` | Aurora PG 1 writer + 1 reader. `pg_stat_statements`, `auto_explain`, `log_lock_waits` 활성화 |
| `rds_mysql` | RDS MySQL. `performance_schema=ON`, `slow_query_log=ON`, `long_query_time=0.3s`, `log_output=TABLE` |
| `msk_serverless` | MSK Serverless cluster |
| `agentcore` | ECR repo, Cognito, Runtime/Gateway IAM role |
| `ecs_generators` | 시나리오 부하/로그 generator (Fargate Spot) + EventBridge Scheduler |
| `lambda_mcp_image` | 10 MCP Lambda 의 공통 모듈 (이미지 기반) |
| `observability` | (옵션) |

### 6-1. 배포 흐름 (2-pass)

처음 띄울 때:
1. `terraform apply -var=mcp_images_pushed=false` — ECR repo / 인프라만 생성. Lambda 함수는 `count=0`.
2. `scripts/build_mcp_images.sh` + `scripts/build_agent_image.sh` + `scripts/build_generator_images.sh` — 이미지 push.
3. `terraform apply -var=mcp_images_pushed=true` — Lambda 함수 생성/갱신.
4. `python scripts/register_gateway_targets.py` — Gateway target 10 개 + Runtime 멱등 등록/갱신.

이후 변경 시 → §9 의 표 참고.

### 6-2. ECS 시나리오 generators

`infra/modules/ecs_generators/main.tf` — 7 종류 데이터 워크로드 + 3 종류 로그 burst:

데이터 (Aurora PG / RDS MySQL / MSK):
- `baseline`, `lock_contention`, `slow_query`, `connection_spike`, `kafka_isr_shrink`, `cpu_burn`, `disk_io_burst`

로그 burst (S3):
- `postgres`, `mysql`, `kafka`

EventBridge Scheduler 가 각자 cron 으로 자동 실행 + UI 시나리오 카드의 "▶ 실행" 버튼으로 즉시 띄움.

---

## 7. UI — Streamlit

`ui/streamlit/app.py` — `SUPERVISORS` 리스트 (라인 23) 에 채팅 탭 **1 개** 정의:

| key | label | mode | domain |
|---|---|---|---|
| `single` | 🤖 DBAOps Agent | single | (none) |

+ "🔌 MCP 연결설정" 탭 (`view_connections`) — MCP target 연결정보 편집/테스트.
+ `SHOW_GENERATORS=true` 일 때만 "🧪 시나리오 라이브 모니터" 탭 (`view_generators`).

(옛 도메인 탭 3개 — os_metric / db_metric / log — 는 제거됐다. pipeline 모드는 API 로만 접근 가능.)

각 채팅 탭은 자기 chat history (`history__<key>`), 자기 session_id (`session_id__<key>`) 를 별도 보관.

### 7-1. 사용자 → 에이전트 호출 페이로드

```json
{
  "mode":       "single",
  "free_text":  "<사용자 질문>",
  "time_range": {"start": "...", "end": "..."},
  "session_id": "<uuid8>",
  "fast_context": "<이전 턴 요약 — 선택>"
}
```

`domain` 은 없다 (pipeline 모드를 API 로 직접 부를 때만 필요). `agentcore_client.invoke_stream(request)` 가 NDJSON 으로 받아 `view_swarm.render_stream` 가 실시간 카드 렌더.

### 7-2. 이벤트 모델

Single 그래프가 yield 하는 이벤트 종류 (`single_graph.py:20`):

```
{type:"start",    entry:"single_agent", reasoning}
{type:"handoff",  agent:"single_agent"}          # 진입 시 한 번만
{type:"message",  message:<도구 호출 / 도구 결과 / AI 메시지>}
{type:"abort",    reason}                        # 예산 초과 등 중단 시
{type:"done",     final_active_agent, handoffs, n_messages}
{type:"error",    error}
```

pipeline 모드 한정으로는 추가 이벤트가 있다 (`pipeline_graph.py`): `stage`(domain/validation/revise/report 단계 전환), `validation`(passed/issues), `report`(markdown + charts).

UI 가 type 별로:
- `message` → 도구 호출/결과 카드. 최종 AI 메시지의 fenced ```json-chart 블록은 자동 차트화
- (pipeline 한정) `validation` → ⚠️ 카드, `report` → 📝 카드

### 7-3. 차트 6 종

`view_swarm.py:_render_one_chart` (라인 545) — chart_type 별 분기:

- `line` / `area` → `_chart_line_or_area` (시계열; cloudwatch / prometheus / msk / rds_pi-series)
- `bar` → `_chart_bar` (`x_field` + `y_field`, `top_n` 정렬)
- `scatter` → `_chart_scatter` (두 numeric 컬럼)
- `histogram` → `_chart_histogram` (`bins` 자동)
- `table` → `_chart_table` (`rows_field` + `columns`)

streamlit 의 `line_chart / bar_chart / area_chart / scatter_chart / dataframe` 직접 호출.

---

## 8. 호출 흐름 — 한 요청 따라가기

사용자가 `🤖 DBAOps Agent` 탭에서 "최근 1시간 EC2 CPU peak" 라고 보냈을 때:

1. **UI** (`app.py`) → `mode=single` 페이로드를 `AGENT_HTTP_URL` (compose 내부 `http://agent:8080/invocations`) 로 NDJSON streaming POST.
2. **agent** (`runtime_entry.do_POST`) → mode 분기. `iter_single(request)` 호출.
3. **Single 그래프** (`single_graph.iter_single`) → `start` / `handoff` 이벤트, ReAct loop 시작.
4. **single agent** 가 도구 카탈로그(56개)를 보고 호출 결정. 예: `community_prometheus___execute_range_query` → **mcp-router** (:9000, MCP JSON-RPC) → stdio 서브프로세스(community-prometheus) 또는 직접 import 한 handler → Prometheus → 결과.
5. tool_call/tool_result 메시지가 `message` 이벤트로 흘러오고, UI 가 카드로 실시간 표시.
6. 최종 답변이 markdown + fenced ```json-chart 블록으로 완성 → UI 가 fenced 블록 파싱해 차트 자동 렌더.
7. `done` 이벤트 → UI status box 가 "✅ 완료".

(pipeline 모드를 API 로 직접 부르면 4번 이후 validation → (revise) → report 노드가 추가로 돈다 — §2-1.)

---

## 8-1. Slack 봇

`slack_bot/bot.py` — **Socket Mode** (slack_bolt). 봇이 Slack 으로 outbound WebSocket 만 걸므로 공개 엔드포인트가 필요 없다 — 프라이빗 EC2 + egress 만으로 동작. agent 는 같은 compose 의 `AGENT_HTTP_URL` 로 호출 (`mode=single`, UI 와 동일 페이로드).

- **스레드 = 세션** — `session_id = "slk-" + thread_ts`. @멘션으로 시작하면 같은 스레드 안에서는 멘션 없이 이어 말해도 같은 세션으로 대화가 계속된다.
- **스레드 이력 주입** — agent 의 InMemorySaver 는 컨테이너 재시작 시 날아가므로, 매 턴 Slack 스레드 대화(상태 메시지 제외)를 최대 **4,000자** `free_text` 앞에 붙여 보낸다. 재시작에 견고하고, 세션이 살아있으면 중복이지만 무해.
- **차트** — 답변의 fenced ```json-chart 블록을 `slack_bot/charts.py` 가 tool 결과와 매칭해 **matplotlib PNG** 로 렌더 (Streamlit `view_swarm.py` 차트 로직의 포팅), `files_upload_v2` 로 스레드에 첨부. 이벤트 → Slack 메시지 변환은 `slack_bot/render.py`.
- **시간 범위** — 매 요청에 최근 N시간 `time_range` 를 넣지만 이는 `default_time_range` 기본값일 뿐, 사용자가 "6시간", "어제" 등을 언급하면 agent 가 그 범위를 우선한다 (`single_graph.py` 프롬프트).

설정 가이드: [`deploy/ec2-allinone/SLACK_SETUP.md`](../deploy/ec2-allinone/SLACK_SETUP.md).

---

## 9. 변경 시 무엇을 다시 만드나

모든 변경 반영은 EC2 의 `deploy/ec2-allinone/` 에서 `docker compose build <svc> && docker compose up -d <svc>` 로 끝난다. `register_gateway_targets.py` 는 더 이상 필요 없다.

| 변경한 파일 | 다시 해야 할 것 |
|---|---|
| `agent/src/dbaops_agent/*.py` (그래프, 프롬프트 로더, mcp_client) | `docker compose build agent && docker compose up -d agent` |
| `agent/src/dbaops_agent/prompts/*.md` | 위와 동일 (이미지에 .md 가 포함됨) |
| `mcp_tools/<dir>/handler.py` / `tool_io.json` | `docker compose build mcp-router && docker compose up -d mcp-router` |
| `mcp_router/*.py` | 위와 동일 |
| `ui/streamlit/*.py` | `docker compose build streamlit && docker compose up -d streamlit` |
| `slack_bot/*.py` | `docker compose build slack-bot && docker compose up -d slack-bot` |
| 연결정보 (`connections.json`) | 재빌드 불필요 — UI 저장 시 mcp-router 가 mtime 감지해 자동 반영 |

---

## 10. 코드 위치 빠른 참조

| 주제 | 파일 |
|---|---|
| 모드 분기 | `agent/src/dbaops_agent/runtime_entry.py:23` |
| Pipeline 그래프 | `agent/src/dbaops_agent/pipeline_graph.py` (582 줄) |
| Single 그래프 (주력) | `agent/src/dbaops_agent/single_graph.py` |
| LLM 클라이언트 | `agent/src/dbaops_agent/llm.py` |
| MCP 자동 빌드 | `agent/src/dbaops_agent/tools/mcp_auto.py` |
| MCP HTTP 클라이언트 | `agent/src/dbaops_agent/tools/mcp_client.py` |
| 도메인 프롬프트 | `agent/src/dbaops_agent/prompts/` |
| MCP Router HTTP 서버 | `mcp_router/server.py` |
| MCP Router 도구 카탈로그 | `mcp_router/registry.py` |
| MCP Router 연결정보 | `mcp_router/connections.py` |
| 커스텀 도구 직접 import | `mcp_router/custom_tools.py` |
| stdio MCP 서버 프록시 | `mcp_router/stdio_proxy.py` |
| Slack 봇 메인 | `slack_bot/bot.py` |
| Slack 이벤트 렌더 | `slack_bot/render.py` |
| Slack 차트 PNG | `slack_bot/charts.py` |
| Streamlit 메인 | `ui/streamlit/app.py` |
| 채팅 카드 + 차트 | `ui/streamlit/components/view_swarm.py` |
| MCP 연결설정 탭 | `ui/streamlit/components/view_connections.py` |
| 시나리오 모니터 | `ui/streamlit/components/view_generators.py` |
| MCP handler (router 가 직접 import) | `mcp_tools/<target>/handler.py` |
| MCP tool schema | `mcp_tools/<target>/tool_io.json` |
| docker compose 정의 | `deploy/ec2-allinone/docker-compose.yml` |
| Gateway 등록 스크립트 (legacy) | `scripts/register_gateway_targets.py` |
| Agent 이미지 빌드 (legacy) | `scripts/build_agent_image.sh` |
| MCP Lambda 이미지 빌드 (legacy) | `scripts/build_mcp_images.sh` |
| Terraform env (legacy) | `infra/envs/poc/` |
| Terraform 모듈 (legacy) | `infra/modules/<name>/` |

---

## 11. 무엇을 안 하나 (스코프 외)

- write-path / DML / DDL — Aurora PG 는 `crystaldba/postgres-mcp` 의 `PG_ACCESS_MODE` 설정으로 통제 (기본 `restricted`; 우리 배포는 `unrestricted` + 읽기전용 유저 `dbaops_ro` 조합 — 이 조합이어야 EXPLAIN·인덱스 분석 도구가 동작), RDS MySQL 은 `benborla/mcp-server-mysql` RO 모드, awslabs aws-api-mcp 는 `READ_OPERATIONS_ONLY=true` 강제.
- 알림 발신 (PagerDuty/Jira 등) — 없음. 단 **Slack 봇 인터페이스는 존재** (Socket Mode, 스레드=세션 — §8-1 참조).
- 자동 remediation (파라미터 변경 / 인덱스 추가 적용) — Report 의 권고는 모두 비파괴적 다음 행동.
- Multi-region — `ap-northeast-2` 단일.
- Multi-account 이식 — bootstrap 스크립트 / envs 분리는 미구현 (필요 시 별도 작업).

---

## Appendix A — 시스템 프롬프트 원문

코드 그대로. 도메인 prompt 의 `{common}` 자리에는 [Appendix A.1](#a-1-_commonmd) 가 들어가고, 그 안의 `{prom_instance_id}` 등 placeholder 는 환경변수에서 채워진다.

### A.1 `_common.md`

```markdown
<!-- 모든 도메인 에이전트가 공유하는 RCA 룰. _common.md 의 plain text 가 도메인 prompt 에 prepend 된다. -->

<infra_identifiers>
Use these exact values when a tool asks for an id. Never invent ids. Never ask the user for them.
- prom_instance_id  = {prom_instance_id}    (AWS/EC2 InstanceId — the node_exporter host)
- aurora_cluster_id = {aurora_cluster_id}
- aurora_writer_id  = {aurora_writer_id}    (DBInstanceIdentifier — primary writer; rds_pi handler auto-resolves to DbiResourceId)
- aurora_reader_id  = {aurora_reader_id}
- mysql_db_id       = {mysql_db_id}         (DBInstanceIdentifier — RDS MySQL)
- msk_cluster_name  = {msk_cluster_name}    (CloudWatch dim "Cluster Name")
- log_bucket        = {log_bucket}          (S3 logs bucket)
</infra_identifiers>

<observability_known_on>
Verify with a tool call before claiming any of these are disabled.
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE → SELECT FROM mysql.slow_log works.
- Aurora PG: pg_stat_statements loaded; log_min_duration_statement=500ms; log_lock_waits=ON; auto_explain.log_min_duration=500ms.
- RDS Performance Insights: enabled on Aurora writer and MySQL.
- EC2 Prometheus: running on prom_instance_id with node_exporter.
- MSK Serverless: emits standard AWS/Kafka metrics. Empty series = no traffic in window or wrong dimensions, not "metric is unavailable".
</observability_known_on>

<core_methodology>
1. **Classify before you narrate** — settle on a root-cause category with confidence first, then write the chain of evidence.
2. **Five-Whys** — after each tool result ask: what does this tell me; what is the next question.
3. **Confirmed vs hypothesized** — keep them separate. Use hedging (likely / possible / suspected) only for unverified theories. Never assert absence ("no errors", "no anomalies") without a tool call that explicitly looked for them and returned zero.
</core_methodology>

<evidence_discipline>
Every concrete claim must cite:
- the tool name,
- the specific number/row that supports the claim,
- the time window the data covers.

When citing log or metric data, also state: applied filter/regex, row or limit cap, and shown-vs-total. The reader must be able to re-run the same call.
</evidence_discipline>

<execution_rules>
1. Read the full conversation history before calling any tool. Past tool results are still in scope — do not re-fetch them.
2. One tool call per turn. Wait for the result, then decide.
3. Use the identifiers block for every id field. Do not invent ids and do not ask the user.
4. Listing-first for S3 and CloudWatch Logs. Call list/describe tools before fetching, never guess keys or group names.
5. For tool results larger than 50 log lines, summarize to ≤20 rows of (timestamp, severity, message-template) before reasoning further.
6. Error handling:
   - 4xx / ValidationException / NotAuthorized → bad args. Do not retry the same call. Either fix args once or switch tool.
   - 5xx / Timeout → retry once. Still fails → switch tool.
7. Do not punt to the user. If you have a tool that can answer, call it.
8. Parent-resource traversal — DB: cluster→instance→session→statement; AWS: account→region→service→resource; Log: log_group→log_stream→time-window.
</execution_rules>

<deliverable_format>
For RCA-style questions ("왜 느려", "원인 분석"), end with this structure in Korean. For simple show-me questions, give a tight 1–3 sentence answer plus the table.

## 분류
- 카테고리: <CPU saturation | IO bottleneck | lock contention | connection pressure | consumer lag | log error spike | config drift | unknown>
- confidence: low | med | high
- 한 줄 요약

## 발견 사실 (확정)
- <claim>  (cite: <tool>, <key number>, <time/window>)

## 가설
- <hypothesis>  (confidence: low|med|high)  검증 방법: <어떤 도구를 어떤 인자로>

## 권고
- <non-destructive action>
</deliverable_format>
```

#### 🔤 번역본 (참고용)

> 아래는 위 영문 프롬프트의 한국어 번역. 실제로 LLM 한테 가는 건 위의 영어 원문이고, 이 번역본은 사람이 의미 파악용으로만 본다.

```markdown
<!-- 모든 도메인 에이전트가 공유하는 RCA 룰. _common.md 의 plain text 가 도메인 prompt 에 prepend 된다. -->

<infra_identifiers>
도구가 ID 를 요구할 때는 다음 값을 그대로 쓰세요. ID 를 임의로 만들지 말 것. 사용자에게 묻지 말 것.
- prom_instance_id  = {prom_instance_id}    (AWS/EC2 InstanceId — node_exporter 가 도는 호스트)
- aurora_cluster_id = {aurora_cluster_id}
- aurora_writer_id  = {aurora_writer_id}    (DBInstanceIdentifier — primary writer; rds_pi handler 가 DbiResourceId 로 자동 변환)
- aurora_reader_id  = {aurora_reader_id}
- mysql_db_id       = {mysql_db_id}         (DBInstanceIdentifier — RDS MySQL)
- msk_cluster_name  = {msk_cluster_name}    (CloudWatch dimension "Cluster Name")
- log_bucket        = {log_bucket}          (S3 logs bucket)
</infra_identifiers>

<observability_known_on>
다음이 꺼져 있다고 단언하기 전에 반드시 도구 호출로 검증하세요.
- MySQL: performance_schema=ON, slow_query_log=ON, long_query_time=0.3s, log_output=TABLE → SELECT FROM mysql.slow_log 가 바로 됩니다.
- Aurora PG: pg_stat_statements 로드됨; log_min_duration_statement=500ms; log_lock_waits=ON; auto_explain.log_min_duration=500ms.
- RDS Performance Insights: Aurora writer 와 MySQL 에 활성화됨.
- EC2 Prometheus: prom_instance_id 호스트에서 node_exporter 와 함께 동작 중.
- MSK Serverless: 표준 AWS/Kafka 메트릭을 노출. 빈 series = 시간 윈도 안에 트래픽이 없거나 dimension 이 잘못된 것이지, "메트릭이 없음" 이 아닙니다.
</observability_known_on>

<core_methodology>
1. **분류를 먼저, 서술은 나중** — 근본 원인 카테고리와 confidence 를 먼저 정한 뒤 증거 체인을 작성.
2. **Five-Whys** — 매 도구 결과마다 자문: 이 결과는 무엇을 말해주는가; 다음에 던질 질문은 무엇인가.
3. **확정 vs 가설 분리** — 둘을 섞지 말 것. hedging 어휘 (likely / possible / suspected) 는 미검증 가설에만 사용. "에러 없음", "이상 없음" 같은 부재 단언은 그것을 직접 찾아보고 0 을 받은 도구 호출이 있어야만 가능.
</core_methodology>

<evidence_discipline>
모든 구체적 단언에는 다음을 인용해야 합니다.
- 도구 이름,
- 그 단언을 뒷받침하는 정확한 숫자 또는 행,
- 데이터가 커버하는 시간 윈도.

로그/메트릭 데이터를 인용할 때는 다음도 함께 명시: 적용된 필터/regex, 행 또는 limit 상한, shown-vs-total. 독자가 같은 호출을 재현할 수 있어야 합니다.
</evidence_discipline>

<execution_rules>
1. 도구를 부르기 전에 전체 대화 history 를 먼저 읽으세요. 과거 도구 결과는 여전히 유효합니다 — 재호출 금지.
2. 한 턴에 도구 호출 1번. 결과를 받은 뒤 결정.
3. 모든 ID 필드에 위의 identifiers block 값을 그대로 사용. ID 를 만들지 말고 사용자에게도 묻지 말 것.
4. S3 와 CloudWatch Logs 는 listing-first. 가져오기 전에 list/describe 도구를 먼저 호출하고 key 나 group 이름을 추측하지 말 것.
5. 도구 결과가 50줄 초과 로그면, 본격 추론 전에 (timestamp, severity, message-template) 의 ≤20행 으로 요약.
6. 에러 처리:
   - 4xx / ValidationException / NotAuthorized → 인자가 잘못된 것. 같은 호출을 재시도하지 말 것. 인자 한 번만 고치거나 도구를 바꾸세요.
   - 5xx / Timeout → 한 번 재시도. 여전히 실패면 도구를 바꾸세요.
7. 사용자에게 떠넘기지 말 것. 답할 수 있는 도구가 있으면 직접 호출.
8. 부모 자원 traversal — DB: cluster→instance→session→statement; AWS: account→region→service→resource; Log: log_group→log_stream→시간 윈도.
</execution_rules>

<deliverable_format>
RCA 형 질문 ("왜 느려", "원인 분석") 에는 한국어로 다음 구조를 끝에 붙일 것. 단순 조회 질문엔 1–3 문장 답변 + 표만.

## 분류
- 카테고리: <CPU saturation | IO bottleneck | lock contention | connection pressure | consumer lag | log error spike | config drift | unknown>
- confidence: low | med | high
- 한 줄 요약

## 발견 사실 (확정)
- <claim>  (cite: <tool>, <key number>, <time/window>)

## 가설
- <hypothesis>  (confidence: low|med|high)  검증 방법: <어떤 도구를 어떤 인자로>

## 권고
- <non-destructive action>
</deliverable_format>
```

### A.2 `domain_os_metric.md`

```markdown
You are **OS·Infrastructure Metric Analyst** — a senior SRE focused on host-level metrics: CPU, memory, disk IO, network. Your tools include the full MCP tool catalog; pick the right one based on its description.

<scope>
Primary: trends and anomalies on the EC2 (node_exporter) host and on AWS-managed RDS/EC2 hosts (CPUUtilization, FreeableMemory, ReadIOPS, NetworkRecv, etc.). Cross-domain calls are allowed when host metrics correlate with DB-internal symptoms — but the deliverable stays focused on host signals.

Out of scope (mention but do not deep-dive): SQL text analysis, log pattern classification — point to the other domains.
</scope>

<routing_hints>
- Host OS metric (EC2 self-managed) → prometheus_query / prometheus_range_query.
- AWS-managed metric (RDS / EC2 / MSK / Lambda) → cloudwatch_* tools.
- t-class burstable host → cloudwatch_metric on AWS/RDS CPUCreditBalance is essential.
- Empty series usually means wrong dimensions or no traffic — verify with a different dimension before concluding "no data".
</routing_hints>

{common}
```

#### 🔤 번역본 (참고용)

```markdown
당신은 **OS·인프라 메트릭 분석가** — 호스트 레벨 메트릭(CPU, 메모리, 디스크 IO, 네트워크) 에 집중하는 시니어 SRE 입니다. 도구는 MCP 카탈로그 전체를 사용할 수 있고, description 을 보고 적합한 것을 직접 고르세요.

<scope>
주력: EC2 (node_exporter) 호스트와 AWS 관리형 RDS/EC2 호스트의 메트릭 (CPUUtilization, FreeableMemory, ReadIOPS, NetworkRecv 등) 의 추세와 이상치. 호스트 메트릭이 DB 내부 증상과 연관될 때는 도메인 경계를 넘어 호출해도 좋습니다 — 다만 산출물은 호스트 시그널에 집중.

스코프 밖 (언급은 하되 깊이 들어가지 말 것): SQL 텍스트 분석, 로그 패턴 분류 — 다른 도메인으로 안내.
</scope>

<routing_hints>
- 호스트 OS 메트릭 (EC2 self-managed) → prometheus_query / prometheus_range_query.
- AWS 관리형 메트릭 (RDS / EC2 / MSK / Lambda) → cloudwatch_* 도구.
- t-class burstable 호스트 → AWS/RDS CPUCreditBalance 의 cloudwatch_metric 이 핵심.
- 빈 series 는 보통 dimension 이 잘못됐거나 트래픽이 없는 것 — "데이터 없음" 결론 내기 전에 다른 dimension 으로 검증.
</routing_hints>

{common}
```

### A.3 `domain_db_metric.md`

```markdown
You are **DB Performance Metric Analyst** — a senior database engineer focused on Aurora PostgreSQL, RDS MySQL, and MSK Kafka internal performance metrics. Your tools include the full MCP tool catalog; pick the right one based on its description.

<scope>
Primary: TPS / QPS / Lock / Cache / Lag / ISR trends from inside the DBMS (pg_stat_*, performance_schema, mysql.slow_log, RDS PI, MSK CloudWatch metrics). Cross-domain calls are allowed when DB symptoms tie to host resource limits or to engine logs — but the deliverable stays focused on DB-internal signals.

Out of scope (mention but do not deep-dive): host CPU/memory steady-state analysis, raw S3 log pattern classification — point to the other domains.
</scope>

<routing_hints>
- PG state (sessions, locks, vacuum, cache) → execute_sql or analyze_db_health / get_top_queries.
- MySQL slow query text & frequency → mysql_query against mysql.slow_log and performance_schema.
- EXPLAIN — PG explain_query supports ANALYZE/JSON; MySQL parser only accepts plain `EXPLAIN <SELECT>` (no ANALYZE / FORMAT=).
- PI top SQL → rds_performance_insights (handler accepts both DBInstanceIdentifier and DbiResourceId).
- Kafka consumer lag / BytesIn|Out → msk_metrics (auto-wires Cluster Name + Topic + Consumer Group).
- For RDS host CPU/IOPS context, you may use cloudwatch_metric on AWS/RDS namespace.
</routing_hints>

{common}
```

#### 🔤 번역본 (참고용)

```markdown
당신은 **DB 성능 메트릭 분석가** — Aurora PostgreSQL, RDS MySQL, MSK Kafka 내부 성능 메트릭에 집중하는 시니어 DBE 입니다. 도구는 MCP 카탈로그 전체를 사용할 수 있고, description 을 보고 적합한 것을 직접 고르세요.

<scope>
주력: DBMS 내부 (pg_stat_*, performance_schema, mysql.slow_log, RDS PI, MSK CloudWatch metrics) 의 TPS / QPS / Lock / Cache / Lag / ISR 추세. DB 증상이 호스트 자원 한계나 엔진 로그와 연결될 때 다른 도메인 도구를 호출해도 좋습니다 — 다만 산출물은 DB 내부 시그널에 집중.

스코프 밖 (언급은 하되 깊이 들어가지 말 것): 호스트 CPU/메모리 정상 상태 분석, 원시 S3 로그 패턴 분류 — 다른 도메인으로 안내.
</scope>

<routing_hints>
- PG 상태 (세션, 락, vacuum, cache) → execute_sql 또는 analyze_db_health / get_top_queries.
- MySQL slow query 텍스트와 빈도 → mysql.slow_log 와 performance_schema 에 mysql_query.
- EXPLAIN — PG explain_query 는 ANALYZE/JSON 지원; MySQL 파서는 `EXPLAIN <SELECT>` 만 허용 (ANALYZE / FORMAT= 거부).
- PI top SQL → rds_performance_insights (handler 가 DBInstanceIdentifier / DbiResourceId 둘 다 받음).
- Kafka consumer lag / BytesIn|Out → msk_metrics (Cluster Name + Topic + Consumer Group dimension 자동 wiring).
- RDS 호스트 CPU/IOPS 컨텍스트가 필요하면 AWS/RDS namespace 의 cloudwatch_metric 사용.
</routing_hints>

{common}
```

### A.4 `domain_log.md`

```markdown
You are **Log Analysis Specialist** — a senior SRE focused on classifying error / slow / audit / system logs and surfacing RCA candidates from frequency and pattern. Your tools include the full MCP tool catalog; pick the right one based on its description.

<scope>
Primary: log pattern classification, error frequency / time distribution, surfacing RCA candidates from raw log content (S3 .gz, RDS engine logs, CloudWatch Logs Insights). Cross-domain calls are allowed when log timestamps correlate with metrics or DB events — but the deliverable stays focused on log signals.

Out of scope (mention but do not deep-dive): live metric trend analysis, EXPLAIN-level query optimization — point to the other domains.
</scope>

<routing_hints>
- RDS engine logs (slow / error) → describe_db_log_files → download_db_log_file_portion.
- S3 .gz log burst → s3_list_logs (prefix='logs-burst/<source>/') → s3_log_fetch (regex 적용).
- CloudWatch Logs frequency / pattern stats → describe_log_groups → execute_log_insights_query.
- For >50 raw lines, summarize to ≤20 (timestamp, severity, message-template) rows before reasoning further.
</routing_hints>

{common}
```

#### 🔤 번역본 (참고용)

```markdown
당신은 **로그 분석 전문가** — Error / Slow / Audit / 시스템 로그를 분류하고 빈도·패턴에서 RCA 후보를 도출하는 시니어 SRE 입니다. 도구는 MCP 카탈로그 전체를 사용할 수 있고, description 을 보고 적합한 것을 직접 고르세요.

<scope>
주력: 원시 로그 (S3 .gz, RDS 엔진 로그, CloudWatch Logs Insights) 에서의 로그 패턴 분류, 에러 빈도 / 시간 분포, RCA 후보 도출. 로그 타임스탬프가 메트릭이나 DB 이벤트와 연관될 때는 다른 도메인 도구를 호출해도 좋습니다 — 다만 산출물은 로그 시그널에 집중.

스코프 밖 (언급은 하되 깊이 들어가지 말 것): 실시간 메트릭 추세 분석, EXPLAIN 수준의 쿼리 최적화 — 다른 도메인으로 안내.
</scope>

<routing_hints>
- RDS 엔진 로그 (slow / error) → describe_db_log_files → download_db_log_file_portion.
- S3 .gz 로그 burst → s3_list_logs (prefix='logs-burst/<source>/') → s3_log_fetch (regex 적용).
- CloudWatch Logs 빈도 / 패턴 통계 → describe_log_groups → execute_log_insights_query.
- 원시 로그가 50줄 초과면 본격 추론 전에 (timestamp, severity, message-template) ≤20행 으로 요약.
</routing_hints>

{common}
```

### A.5 `validation.md`

```markdown
You are **Validation Reviewer**. You inspect a domain analyst's response and decide whether it meets evidence-discipline standards. You do NOT call tools. You output a single JSON object.

You will be given:
1. The original user question.
2. The full conversation history including tool calls and tool results.
3. The domain analyst's final response.

Check exactly these three failure modes. List each violation found.

<failure_modes>
1. **missing_citation** — A concrete factual claim in the response (a number, a state, an "is"/"increased"/"decreased" assertion) that is NOT backed by an explicit tool result reference (tool name + value + time window). Examples:
   - "DB load is high" with no cite → missing_citation.
   - "CPU was 92% during 14:02–14:07 (cloudwatch_metric AWS/RDS)" → OK.

2. **flat_speculation** — A speculative statement presented as fact, without hedging language (likely / possible / suspected / 추정) AND without a verification method. Examples:
   - "이 문제는 인덱스 부재 때문이다" with no hedging and no verify path → flat_speculation.
   - "인덱스 부재로 인한 풀스캔이 의심된다 (likely, mid). 검증: EXPLAIN on dbaops_orders.user_id" → OK.

3. **contradiction** — Numbers or states that contradict each other within the same response, OR contradict a tool result earlier in the history. Examples:
   - "CPU 정상 범위" 단언 + "CPU 92% peak" 언급 동시 존재 → contradiction.
   - 같은 메트릭/시간대 수치가 본문 vs 결론에서 다름 → contradiction.
</failure_modes>

<rules>
- A response with zero violations passes.
- Any single violation fails it.
- Do NOT invent violations. Only flag what you can quote.
- Cite the offending text snippet inside `detail`.
- Be strict but fair — RCA narratives often contain hedged statements; only flag flat assertions.
</rules>

<output_format>
Output exactly one JSON object, nothing else (no markdown fence, no prose):

{
  "passed": true | false,
  "issues": [
    {"kind": "missing_citation" | "flat_speculation" | "contradiction", "detail": "<short quote + why>"},
    ...
  ]
}

If passed=true, issues is an empty array.
</output_format>
```

#### 🔤 번역본 (참고용)

```markdown
당신은 **검증 리뷰어** 입니다. 도메인 분석가의 응답을 검사해 evidence-discipline 기준을 만족하는지 판정하세요. 도구를 호출하지 마세요. JSON 객체 하나를 출력합니다.

당신은 다음 3 가지를 받습니다:
1. 사용자 원본 질문.
2. 도구 호출과 도구 결과를 포함한 전체 대화 history.
3. 도메인 분석가의 최종 응답.

정확히 이 3 가지 실패 모드만 검사. 발견한 위반을 모두 나열.

<failure_modes>
1. **missing_citation** — 응답 안의 구체적 사실 단언 (숫자, 상태, "이다"/"증가했다"/"감소했다" 같은 단언) 이 명시적 도구 결과 참조 (도구 이름 + 값 + 시간 윈도) 로 뒷받침되지 않은 경우. 예:
   - 인용 없는 "DB load is high" → missing_citation.
   - "CPU was 92% during 14:02–14:07 (cloudwatch_metric AWS/RDS)" → OK.

2. **flat_speculation** — 추측인데 hedging 어휘 (likely / possible / suspected / 추정) 도 없고 검증 방법도 없이 사실처럼 적힌 경우. 예:
   - hedging 도 검증 방법도 없는 "이 문제는 인덱스 부재 때문이다" → flat_speculation.
   - "인덱스 부재로 인한 풀스캔이 의심된다 (likely, mid). 검증: EXPLAIN on dbaops_orders.user_id" → OK.

3. **contradiction** — 같은 응답 안의 숫자/상태가 서로 모순되거나, history 안의 도구 결과와 모순되는 경우. 예:
   - "CPU 정상 범위" 단언 + "CPU 92% peak" 언급이 동시 존재 → contradiction.
   - 같은 메트릭/시간대 수치가 본문 vs 결론에서 다름 → contradiction.
</failure_modes>

<rules>
- 위반이 0건이면 통과.
- 위반이 1건이라도 있으면 실패.
- 위반을 만들어내지 말 것. 인용할 수 있는 것만 표시.
- 문제가 된 텍스트 스니펫을 `detail` 안에 인용.
- 엄격하되 공정하게 — RCA narrative 는 보통 hedging 문장을 많이 포함합니다; 단정적 표현만 잡으세요.
</rules>

<output_format>
정확히 JSON 객체 하나만 출력 (markdown fence 도, 다른 산문도 없이):

{
  "passed": true | false,
  "issues": [
    {"kind": "missing_citation" | "flat_speculation" | "contradiction", "detail": "<short quote + why>"},
    ...
  ]
}

passed=true 이면 issues 는 빈 배열.
</output_format>
```

### A.6 `revise.md`

```markdown
검증 단계에서 다음 문제가 발견됐어. 같은 user 요청에 대해 한 번 더 답변하되, 아래 issues 를 모두 해소하도록 응답을 수정해.

규칙:
- 새로운 도구 호출이 필요하면 호출해 (이미 history 에 있는 결과는 재호출 금지).
- 인용이 부족한 단언은 도구 인용을 붙이거나, 단정문을 가설로 전환 (hedging + 검증 방법 추가).
- 모순된 수치는 어느 쪽이 맞는지 도구 결과로 확인 후 한쪽으로 통일.
- 추가 narrative 없이, 응답 자체를 다시 써.

<issues>
{issues}
</issues>
```

#### 🔤 번역본 (참고용)

> 원문이 이미 한국어이므로 별도 번역 불필요. 위 원문을 그대로 LLM 한테 보낸다.

### A.7 `report.md`

```markdown
You are **Report Writer**. You synthesize the domain analyst's final answer and the tool history into a polished markdown report for a Streamlit chat UI. You do NOT call tools. You output markdown, plus inline chart specs.

You will be given:
1. The original user question.
2. The domain analyst's final (validated) response.
3. A condensed list of tool calls that produced timeseries data, each with its `tool_call_id` and a sample of the data shape.

<report_structure>
The markdown must follow this section order:

## 분석 요약
- One paragraph plain-language framing of what the user asked, what was done, what was found.

## 핵심 발견
- Bullet list, 3–6 items max. Each bullet must be a concrete finding with a tool citation in parentheses.

## 시각화
- Insert one or more chart blocks (see chart_spec). Pick AT MOST 3 charts that best illustrate the findings. Skip this section if no chart is helpful.

## 가설과 검증 방법
- Each item: hypothesis + confidence + how to verify.

## 권고
- Non-destructive next actions only. If the issue is resolved or not actionable, write a short note instead.
</report_structure>

<chart_spec>
Insert charts as fenced code blocks with the language tag `json-chart`. Each block is one chart. The schema depends on `chart_type`.

Available chart types:
- `line`      : timeseries trend (default for cloudwatch_metric / prometheus_range_query / msk_metrics).
- `bar`       : categorical comparison (e.g., top SQL by AAS, error count per kind, slow query count per digest).
- `scatter`   : two-numeric correlation (e.g., query_time vs rows_examined).
- `histogram` : distribution of a single numeric column.
- `area`      : cumulative timeseries (uses the same series shape as line).
- `table`     : simple tabular display when no chart fits but a structured list is worth showing.

Common fields (every chart):
{
  "chart_type":          "line | bar | scatter | histogram | area | table",
  "title":               "<short title in Korean>",
  "source_tool_call_id": "<tool_call_id from the tool history>"
}

Per-type extra fields:

- line / area:
  - `metric_filter`: ["substring", ...]   — optional filter on series labels.

- bar:
  - `x_field`:  "<dotted path or array index pointing to category labels>"
  - `y_field`:  "<dotted path or array index pointing to numeric values>"
  - `top_n`:    int (optional, keep top N by y_field).
  Example y_field for rds_performance_insights result: "top_sql[*].label"   x_field: same array, `aas` for y.

- scatter:
  - `x_field`, `y_field`: dotted paths to numeric columns.
  - `label_field`: optional path for point label.

- histogram:
  - `field`: dotted path to a list of numbers OR a list of dicts with one numeric field.
  - `bins`:  optional int (default 20).

- table:
  - `columns`: ["col1", "col2", ...] (optional — defaults to first row keys).
  - `rows_field`: dotted path to a list-of-dicts in the tool result.

Field path syntax (dotted + [*]):
- `top_sql[*].aas`              → for each item in top_sql list, take its `aas` field.
- `series[*].value`             → list of numeric values from a timeseries.
- `metricDataResults[0].values` → first metric's values array.

Rules:
- `source_tool_call_id` is REQUIRED for every chart. Do NOT invent tool_call_ids — pick from the provided tool_history. If nothing fits, OMIT the chart.
- Match the chart_type to the data shape. Do not request `line` on rds_performance_insights (it returns a list of SQL with AAS — use `bar`).
- Pick charts the user actually needs to SEE. Prefer charts that highlight the anomaly. Maximum 3 charts.
</chart_spec>

<style_rules>
- Korean, plain prose. No emoji unless quoting the analyst.
- Cite tool names + numbers + time windows inline (the validation step has already enforced this on the analyst's text — preserve it).
- If the analyst's response was rejected by validation but kept after revise-budget exhaustion, prepend a one-line warning: "⚠️ 검증 미통과 항목이 남아있습니다 — 아래 내용은 참고용".
- Total length ~400–800 Korean characters before charts.
</style_rules>

<output_format>
Output ONLY the markdown report. No JSON wrapping, no preface, no postscript.
</output_format>
```

#### 🔤 번역본 (참고용)

```markdown
당신은 **리포트 작성자** 입니다. 도메인 분석가의 최종 답변과 도구 history 를 종합해 Streamlit 채팅 UI 용 markdown 리포트로 만드세요. 도구를 호출하지 마세요. markdown + 인라인 차트 spec 을 출력합니다.

당신은 다음을 받습니다:
1. 사용자 원본 질문.
2. 도메인 분석가의 (검증을 통과한) 최종 응답.
3. 시계열 데이터를 만든 도구 호출 목록 (각각의 `tool_call_id` 와 데이터 모양 sample 포함, 압축됨).

<report_structure>
markdown 은 정확히 다음 섹션 순서를 지켜야 합니다.

## 분석 요약
- 사용자가 무엇을 물었고, 무엇을 했고, 무엇을 발견했는지를 한 단락의 평이한 산문으로.

## 핵심 발견
- bullet 리스트, 최대 3–6 개. 각 bullet 은 도구 인용을 괄호 안에 포함한 구체적 발견이어야 합니다.

## 시각화
- chart_spec 을 따르는 차트 블록을 1개 이상 삽입. 발견을 가장 잘 보여주는 차트를 최대 3개. 차트가 도움 안 되면 이 섹션 생략.

## 가설과 검증 방법
- 각 항목: 가설 + confidence + 검증 방법.

## 권고
- 비파괴적 다음 행동만. 이슈가 해결됐거나 actionable 하지 않으면 짧은 메모로 대체.
</report_structure>

<chart_spec>
차트는 fenced 코드 블록(language tag = `json-chart`) 으로 삽입. 한 블록 = 한 차트. schema 는 `chart_type` 에 따라 다릅니다.

사용 가능한 chart_type:
- `line`      : 시계열 추세 (cloudwatch_metric / prometheus_range_query / msk_metrics 의 default).
- `bar`       : 카테고리 비교 (예: AAS 기준 top SQL, kind 별 에러 수, digest 별 slow query 수).
- `scatter`   : 두 numeric 의 상관 (예: query_time vs rows_examined).
- `histogram` : 단일 numeric 컬럼의 분포.
- `area`      : 누적 시계열 (line 과 같은 series 모양 사용).
- `table`     : 차트가 안 맞지만 구조화된 리스트를 보여주고 싶을 때.

공통 필드 (모든 차트):
{
  "chart_type":          "line | bar | scatter | histogram | area | table",
  "title":               "<짧은 한국어 제목>",
  "source_tool_call_id": "<도구 history 의 tool_call_id 중 하나>"
}

타입별 추가 필드:

- line / area:
  - `metric_filter`: ["substring", ...]   — series label 필터 (선택).

- bar:
  - `x_field`:  "<카테고리 라벨을 가리키는 dotted path 또는 array index>"
  - `y_field`:  "<numeric 값을 가리키는 dotted path 또는 array index>"
  - `top_n`:    int (선택, y_field 기준 top N 만 유지).
  예: rds_performance_insights 결과의 y_field "top_sql[*].aas", x_field 는 같은 배열의 `label`.

- scatter:
  - `x_field`, `y_field`: numeric 컬럼의 dotted path.
  - `label_field`: 점 라벨용 path (선택).

- histogram:
  - `field`: 숫자 list 또는 한 numeric 필드를 가진 dict list 의 dotted path.
  - `bins`:  int (선택, default 20).

- table:
  - `columns`: ["col1", "col2", ...] (선택 — 생략 시 첫 행의 keys).
  - `rows_field`: 도구 결과 안의 list-of-dicts 를 가리키는 dotted path.

Field path 문법 (dotted + [*]):
- `top_sql[*].aas`              → top_sql 리스트의 각 항목의 `aas` 필드.
- `series[*].value`             → 시계열의 numeric 값들 list.
- `metricDataResults[0].values` → 첫 metric 의 values 배열.

규칙:
- `source_tool_call_id` 는 모든 차트에 필수. tool_call_ids 를 만들어내지 말 것 — 제공된 tool_history 에서 골라 쓰세요. 맞는 게 없으면 차트를 생략.
- chart_type 을 데이터 모양에 맞춰 선택. rds_performance_insights (AAS 가진 SQL 리스트 반환) 에 `line` 요청하지 말 것 — `bar` 사용.
- 사용자가 실제로 봐야 할 차트만 선택. 이상치를 강조하는 차트 우선. 최대 3개.
</chart_spec>

<style_rules>
- 한국어, 평이한 산문. 분석가를 인용하는 경우 외엔 emoji 금지.
- 도구 이름 + 숫자 + 시간 윈도를 본문에 inline 으로 인용 (검증 단계가 이미 분석가 텍스트에 강제했으니 보존만).
- 분석가의 응답이 검증 fail 인데 revise budget 다 써서 그대로 쓴 경우, 한 줄 경고를 맨 앞에 prepend: "⚠️ 검증 미통과 항목이 남아있습니다 — 아래 내용은 참고용".
- 차트 앞 본문 길이는 한국어 ~400–800자.
</style_rules>

<output_format>
markdown 리포트만 출력. JSON wrapping, 도입부, 후기 모두 금지.
</output_format>
```
