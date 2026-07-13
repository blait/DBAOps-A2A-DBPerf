# SQL Server Perf Agent — 매뉴얼

DBAOps와 **A2A 프로토콜로 상호 연동**되는 SQL Server 쿼리 성능 에이전트.
docker 없이 **venv + systemd**로 DBAOps 4개 + dbaops-a2a + perf 2개 서비스가 한 EC2 호스트에서 함께 돈다.

```
        사용자 (브라우저)
     ┌────────┴─────────┐
     ▼                  ▼
 dbaops-streamlit   dbperf-streamlit :8502
     :8501           (⚡ perf 채팅 / 🧭 ops 채팅 / 🔌 연동 관리)
     │                  │ A2A            │ A2A
     ▼                  ▼                ▼
 ┌──────────────┐  A2A(native)  ┌──────────────┐
 │  dbaops-a2a  │◀─────────────▶│  dbperf-a2a  │
 │    :9102     │               │   :9100      │
 └──────┬───────┘               └──────┬───────┘
        │ invoke_single         stdio MCP (도구 13개)
        ▼                              │ pymssql
 dbaops-mcp-router :9000               ▼
   → Aurora/MySQL/Kafka/Prometheus  RDS SQL Server
 dbaops-agent :8080 (HTTP, DBAOps 자체 UI/Slack용)
 dbaops-slack-bot(@DBAOps 멘션) → agent :8080
```

- **dbperf-a2a (:9100)** — SQL Server 쿼리 성능 전문가. **LangGraph 4노드 파이프라인**
  (analyze→validate→revise(조건부)→report — DBAOps pipeline_graph와 같은 설계).
  stdio MCP 도구 13개(진단 12 + Slack 알림 1) + ask_dbaops_agent(A2A, 양방향).
  검증 단계는 PERF_VALIDATION=0으로 끌 수 있음(기본 켬).
- **dbaops-a2a (:9102)** — DBAOps RCA 에이전트를 A2A로 직접 노출(native). `dbaops/a2a_server.py`가
  DBAOps `invoke_single`을 a2a-sdk로 감쌈. dbperf-a2a에 대한 A2A client 도구도 가짐.
  **파사드/HTTP 변환 없음** — 두 A2A 서버가 직접 대화.
- 무한 위임 루프는 시스템 프롬프트의 역할 경계로 방지 — perf는 SQL Server 질문을
  절대 위임하지 않고, dbaops는 SQL Server 컨텍스트가 필요할 때만 perf에 위임.
- 포트는 DBAOps(9000/8080/8501)와 겹치지 않게 9100/9102/8502로 배치.
- `dbaops-agent(:8080)`는 DBAOps 자체 UI·Slack봇 전용 HTTP로 계속 남는다(A2A 경로와 무관).

---

## 1. 기동

루트 `deploy/install.sh`가 DBAOps(vanilla)와 함께 dbaops-a2a·perf 유닛을 설치·기동한다.

```bash
cd ~/DBAOps-A2A-DBPerf && bash deploy/install.sh
```

| systemd 유닛 | 포트 | 역할 |
|---|---|---|
| `dbperf-a2a` | 9100 (내부) | Query Performance 에이전트 A2A 서버 |
| `dbaops-a2a` | 9102 (내부) | DBAOps RCA 에이전트 A2A 서버 (native) |
| `dbperf-streamlit` | **8502** (외부) | Perf Streamlit UI |

```bash
systemctl status dbperf-a2a dbaops-a2a dbperf-streamlit --no-pager
sudo systemctl restart dbaops-a2a
journalctl -u dbperf-a2a -f
```

## 2. 사용법

### 2-1. Streamlit UI — http://\<host\>:8502

- **⚡ Query Performance 탭** — SQL Server 쿼리 성능 질문
- **🧭 DBAOps Ops Agent 탭** — OS/Aurora/MySQL/Kafka/로그 질문 (dbaops-a2a 경유)
- **🔌 연동 관리 탭** — DB/Slack/DBAOps agent/A2A×2 상태 확인, Slack 테스트

### 2-2. CLI

```bash
VENV=/opt/dbaops/venv
$VENV/bin/python /path/to/perf-agent/query_agent.py "지난 24시간 CPU 상위 쿼리 5개"
$VENV/bin/python /path/to/perf-agent/connections.py status
```

### 2-3. A2A 직접 호출

```bash
curl -s http://127.0.0.1:9100/.well-known/agent-card.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])'
curl -s http://127.0.0.1:9102/.well-known/agent-card.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])'
```

Python 클라이언트 예시는 [AGENT_GUIDE.md](AGENT_GUIDE.md) 사용례 8 참고.

## 3. 에이전트 간 협업 (A2A)

- **perf → dbaops**: perf 탭에 "SQL Server 쿼리는 네가 보고, 같은 시간대 호스트 CPU는
  DBAOps한테 물어봐서 종합해줘" → perf가 `a2a_send_message`로 dbaops-a2a(:9102)에 A2A 질문 →
  dbaops-a2a가 `invoke_single`로 분석 → 답변 반환 → perf가 종합 리포트.
- **dbaops → perf**: ops 탭에 "인프라 점검하고 SQL Server 느린 쿼리도 같이" → dbaops-a2a가
  DBAOps 분석 + `a2a_send_message`로 perf(:9100)에 질문 → 합쳐서 답변.

## 4. 연동 설정 (공유 env: /etc/dbaops/dbaops.env)

### 4-1. Slack — 에이전트별 앱 2개 (Bot Token 방식, webhook 아님)

채널에서 **각 에이전트를 따로 멘션**할 수 있다 — Slack 앱을 에이전트마다 하나씩 만든다:

| 앱(멘션 이름) | 봇 프로세스 | 백엔드 | 토큰 env |
|---|---|---|---|
| @dbaagent (기존) | `dbaops-slack-bot` | DBAOps agent :8080 | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` |
| @perfagent (신규 생성) | `dbperf-slack-bot` | Perf A2A :9100 | `PERF_SLACK_BOT_TOKEN` / `PERF_SLACK_APP_TOKEN` |

앱 생성/토큰 발급 절차는 [`../dbaops/deploy/ec2-allinone/SLACK_SETUP.md`](../dbaops/deploy/ec2-allinone/SLACK_SETUP.md)
그대로 (perfagent 앱은 매니페스트의 name/display_name만 바꿔 하나 더 생성).
멘션 이름은 Slack 앱의 display_name이 결정 — 코드와 무관.

```bash
# /etc/dbaops/dbaops.env
SLACK_BOT_TOKEN=xoxb-...          # @dbaagent (DBAOps 봇)
SLACK_APP_TOKEN=xapp-...
PERF_SLACK_BOT_TOKEN=xoxb-...     # @perfagent (Perf 봇)
PERF_SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=#dbops-alerts       # perf 알림 도구(send_slack_notification) 기본 채널
```

토큰 채운 뒤: `sudo systemctl enable --now dbaops-slack-bot dbperf-slack-bot`

사용 예 — 같은 채널에서:
```
@dbaagent Aurora 최근 1시간 CPU 어때?          ← 인프라 RCA
@perfagent mysql-poc 풀스캔 테이블 봐줘         ← 쿼리 성능 분석
  ↳ (스레드에서 멘션 없이) pg-test도 봐줘        ← 같은 세션으로 이어짐
```

- perf의 `send_slack_notification` 도구는 `chat.postMessage` API로 발송
  (봇을 해당 채널에 `/invite` 해야 함)
- 테스트: `/opt/dbaops/venv/bin/python connections.py test-slack`
- 토큰을 env 대신 SSM에 둘 수도 있음: `SLACK_BOT_TOKEN_PARAM=/dbops/slack/bot_token` (SecureString)
- env 수정 후: `sudo systemctl restart dbperf-a2a dbaops-a2a dbperf-streamlit`

### 4-2. DB 자격증명

Secrets Manager `dbops-sqlserver-secret` — `{"host","port","username","password"}` JSON.

```bash
aws secretsmanager put-secret-value --secret-id dbops-sqlserver-secret --region ap-northeast-2 \
  --secret-string '{"host":"<rds-endpoint>","port":1433,"username":"admin","password":"<비밀번호>"}'
```

분석 대상 DB 변경: env의 `DB_NAME` 수정 후 `sudo systemctl restart dbperf-a2a`.

### 4-3. IAM (EC2 instance role)

- `bedrock:InvokeModel*` — 에이전트 LLM
- `secretsmanager:GetSecretValue` — DB 자격증명
- (선택) `ssm:GetParameter` — SSM에 Slack 토큰을 둘 때

## 5. 환경 변수 (systemd 유닛 + 공유 env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PERF_BEDROCK_MODEL_ID` | claude-sonnet-4-5 | perf/dbaops-a2a LLM (env) |
| `BEDROCK_MODEL_ID` | claude-opus-4-7 | (DBAOps용, perf는 PERF_ 우선) |
| `DB_SECRET_ID` | dbops-sqlserver-secret | DB 자격증명 시크릿 (env) |
| `DB_NAME` | master | 분석 대상 데이터베이스 (env) |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` | — | Slack 알림 (bot token, env) |
| `DBAOPS_AGENT_URL` | http://127.0.0.1:8080/invocations | DBAOps agent (유닛에 고정) |
| `PERF_A2A_URL` / `OPS_A2A_URL` | 127.0.0.1:9100 / :9102 | A2A 주소 (유닛에 고정) |
| `ENABLE_A2A` | 1 | perf의 A2A client 도구 on/off |

> perf는 `PERF_BEDROCK_MODEL_ID`를 코드 기본값으로 쓴다. 유닛이 넘기지 않으면 코드 기본값
> (sonnet-4-5) 사용 — DBAOps의 `BEDROCK_MODEL_ID`(opus)와 독립.

## 6. 도구 목록 (멀티엔진 — mssql / postgres / mysql 자동 분기)

모든 진단 도구는 `target` 파라미터로 대상 DB를 고른다 (미지정 시 첫 타깃).
타깃은 `/etc/dbaops/dbaops.env`의 `DB_TARGETS`(JSON)로 등록 — `list_db_targets`로 조회.

조회 (1): list_db_targets

이력/저장소 (3): check_query_store_enabled (QS/pg_stat_statements/performance_schema),
get_top_queries, get_regressed_queries (mssql만 — 타 엔진은 안내 반환)

실시간 (4): get_slow_queries, get_blocking_sessions, get_query_plan, get_wait_stats

인덱스/건강 (4): suggest_indexes, get_index_usage, get_table_health, get_connection_stats

알림 (1): send_slack_notification (bot token)

기능 상세·사용례: [AGENT_GUIDE.md](AGENT_GUIDE.md)

## 7. 트러블슈팅

| 증상 | 확인 | 해결 |
|---|---|---|
| perf 탭 "A2A 호출 실패" | `systemctl status dbperf-a2a`, `journalctl -u dbperf-a2a` | 유닛 재시작 |
| "Login failed for user" | `connections.py test-db` | 시크릿 비밀번호 (§4-2) |
| ops 탭 error | `journalctl -u dbaops-a2a`, `-u dbaops-agent` | agent 기동/Bedrock 권한 |
| Slack `not_in_channel` | — | 해당 채널에서 `/invite @DBAOps` |
| Slack `invalid_auth` | `connections.py status` | SLACK_BOT_TOKEN 값/만료 확인 |
| 포트 8502 안 열림 | `ss -ltnp \| grep 8502` | dbperf-streamlit 상태/SG 인바운드 |
| 응답이 매우 느림 | — | 정상 범위. 도구 다수 + LLM 왕복, dbaops 경유는 수 분 |
