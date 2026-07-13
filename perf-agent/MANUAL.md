# SQL Server Perf Agent — 매뉴얼

DBAOps(ec2-allinone)와 **A2A 프로토콜로 상호 연동**되는 SQL Server 쿼리 성능 에이전트.
루트 `docker-compose.yml`로 DBAOps 4개 + perf 3개 서비스가 한 박스에서 함께 뜬다.

```
        사용자 (브라우저)
     ┌────────┴─────────┐
     ▼                  ▼
 dbaops streamlit   perf-streamlit :8502
     :8501           (⚡ perf 채팅 / 🧭 ops 채팅 / 🔌 연동 관리)
     │                  │ A2A            │ A2A
     ▼                  ▼                ▼
 ┌────────┐  HTTP  ┌──────────────┐  A2A  ┌─────────────┐
 │ agent  │◀───────│  ops-facade  │◀─────▶│  perf-a2a   │
 │ :8080  │        │    :9001     │       │   :9000     │
 └───┬────┘        └──────────────┘       └──────┬──────┘
     │ MCP                                stdio MCP (도구 13개)
     ▼                                           │ pymssql
 mcp-router ─> Aurora/MySQL/Kafka/Prometheus     ▼
                                          RDS SQL Server
 slack-bot(@DBAOps 멘션) ─> agent :8080
```

- **perf-a2a (:9000)** — SQL Server 쿼리 성능 전문가. stdio MCP 도구 13개(진단 12 + Slack 알림 1).
  ops-facade에 A2A client로 물어볼 수 있음(양방향).
- **ops-facade (:9001)** — DBAOps agent 컨테이너(:8080/invocations, HTTP)는 A2A를 모르므로
  A2A ↔ HTTP 변환 파사드. perf-a2a에 대한 A2A client 도구도 가짐.
- 무한 위임 루프는 시스템 프롬프트의 역할 경계로 방지 — perf는 SQL Server 질문을
  절대 위임하지 않고, ops-facade는 SQL Server 컨텍스트가 필요할 때만 perf에 위임.

---

## 1. 기동

```bash
cp .env.example .env   # 값 채우기 (Slack 토큰, DB 시크릿 등)
docker compose up -d --build

# perf 3개만
docker compose up -d --build perf-a2a ops-facade perf-streamlit
```

| 서비스 | 포트 | 역할 |
|---|---|---|
| `perf-a2a` | 9000 (내부) | Query Performance 에이전트 A2A 서버 |
| `ops-facade` | 9001 (내부) | DBAOps A2A 파사드 |
| `perf-streamlit` | **8502** (외부) | Perf Streamlit UI |
| `streamlit` | 8501 (외부) | DBAOps 자체 UI (원본) |

```bash
docker compose logs -f perf-a2a      # 로그
docker compose restart ops-facade    # 재시작
```

## 2. 사용법

### 2-1. Streamlit UI — http://<host>:8502

- **⚡ Query Performance 탭** — SQL Server 쿼리 성능 질문
- **🧭 DBAOps Ops Agent 탭** — OS/Aurora/MySQL/Kafka/로그 질문 (파사드 경유)
- **🔌 연동 관리 탭** — DB/Slack/DBAOps agent/A2A×2 상태 확인, Slack 테스트

### 2-2. CLI (컨테이너 안)

```bash
docker compose exec perf-a2a python query_agent.py "지난 24시간 CPU 상위 쿼리 5개"
docker compose exec perf-a2a python connections.py status
```

### 2-3. A2A 직접 호출

```bash
docker compose exec perf-a2a python -c "
import urllib.request, json
print(json.loads(urllib.request.urlopen('http://perf-a2a:9000/.well-known/agent-card.json').read())['name'])
print(json.loads(urllib.request.urlopen('http://ops-facade:9001/.well-known/agent-card.json').read())['name'])"
```

Python 클라이언트 예시는 [AGENT_GUIDE.md](AGENT_GUIDE.md) 사용례 8 참고.

## 3. 에이전트 간 협업 (A2A)

- **perf → ops**: perf 탭에 "SQL Server 쿼리는 네가 보고, 같은 시간대 호스트 CPU는 ops한테
  물어봐서 종합해줘" → perf가 `a2a_send_message`로 ops-facade에 질문 → 파사드가
  agent(:8080) HTTP 호출 → 종합 리포트.
- **ops → perf**: ops 탭에 "인프라 점검하고 SQL Server 느린 쿼리도 같이" → 파사드가
  DBAOps 분석 + `a2a_send_message`로 perf에 질문 → 합쳐서 답변.

## 4. 연동 설정

### 4-1. Slack — Bot Token 방식 (webhook 아님)

DBAOps slack-bot과 **같은 토큰을 공유**한다. 앱 생성/토큰 발급은
[`dbaops/deploy/ec2-allinone/SLACK_SETUP.md`](../dbaops/deploy/ec2-allinone/SLACK_SETUP.md) 참고.

`.env`:
```bash
SLACK_BOT_TOKEN=xoxb-...   # chat:write 스코프 필요
SLACK_APP_TOKEN=xapp-...   # slack-bot(Socket Mode)용 — perf는 안 씀
SLACK_CHANNEL=#dbops-alerts  # perf 알림 기본 채널
```

- perf 에이전트의 `send_slack_notification` 도구는 `chat.postMessage` API로 발송
  (봇을 해당 채널에 `/invite` 해야 함)
- 테스트: `docker compose exec perf-a2a python connections.py test-slack`
- 토큰을 env 대신 SSM에 둘 수도 있음: `SLACK_BOT_TOKEN_PARAM=/dbops/slack/bot_token` (SecureString)

### 4-2. DB 자격증명

Secrets Manager `dbops-sqlserver-secret` — `{"host","port","username","password"}` JSON.

```bash
aws secretsmanager put-secret-value --secret-id dbops-sqlserver-secret --region ap-northeast-2 \
  --secret-string '{"host":"<rds-endpoint>","port":1433,"username":"admin","password":"<비밀번호>"}'
```

분석 대상 DB 변경: `.env`의 `DB_NAME` 수정 후 `docker compose up -d perf-a2a`.

### 4-3. IAM (EC2 instance role)

- `bedrock:InvokeModel*` — 에이전트 LLM
- `secretsmanager:GetSecretValue` — DB 자격증명
- (선택) `ssm:GetParameter` — SSM에 Slack 토큰을 둘 때

## 5. 환경 변수 (perf 서비스)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PERF_BEDROCK_MODEL_ID` | claude-sonnet-4-5 | perf/파사드 LLM |
| `DB_SECRET_ID` | dbops-sqlserver-secret | DB 자격증명 시크릿 |
| `DB_NAME` | master | 분석 대상 데이터베이스 |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` | — | Slack 알림 (bot token 방식) |
| `DBAOPS_AGENT_URL` | http://agent:8080/invocations | DBAOps agent 주소 |
| `PERF_A2A_URL` / `OPS_A2A_URL` | perf-a2a:9000 / ops-facade:9001 | A2A 주소 |
| `ENABLE_A2A` | 1 | perf의 A2A client 도구 on/off |

## 6. 도구 목록 (13개)

Query Store (6): check_query_store_enabled, get_query_store_top_queries,
get_query_store_regressed_queries, get_query_store_wait_stats,
get_query_execution_history, get_query_store_plan_summary

DMV (6): get_slow_queries, get_blocking_sessions, get_query_plan_from_cache,
get_expensive_queries_from_cache, suggest_indexes, get_index_usage

알림 (1): send_slack_notification (bot token)

기능 상세·사용례: [AGENT_GUIDE.md](AGENT_GUIDE.md)

## 7. 트러블슈팅

| 증상 | 확인 | 해결 |
|---|---|---|
| perf 탭 "A2A 호출 실패" | `docker compose ps`, `logs perf-a2a` | 컨테이너 재시작 |
| "Login failed for user" | `connections.py test-db` | 시크릿 비밀번호 (§4-2) |
| ops 탭 error | `logs ops-facade`, `logs agent` | agent 컨테이너 기동/Bedrock 권한 |
| Slack 발송 실패 `not_in_channel` | — | 해당 채널에서 `/invite @DBAOps` |
| Slack `invalid_auth` | `connections.py status` | SLACK_BOT_TOKEN 값/만료 확인 |
| 응답이 매우 느림 | — | 정상 범위. 도구 다수 + LLM 왕복, ops 경유는 수 분 |
