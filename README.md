# DBAOps-A2A-DBPerf

**DBAOps-Agent(ec2-allinone) × SQL Server Query Performance Agent — A2A 프로토콜 연동 통합본.**

DB/인프라 RCA 에이전트(DBAOps)와 SQL Server 쿼리 성능 전문 에이전트(DBPerf)를
한 EC2 박스(docker compose)에서 함께 띄우고, 두 에이전트가 **A2A로 서로 질문·협업**한다.

```
        사용자
   ┌──────┴───────┬──────────────┐
   ▼              ▼              ▼
 dbaops UI    perf UI :8502   Slack(@DBAOps 멘션)
  :8501           │ A2A           │
   │              ▼               │
   │       ┌─────────────┐  A2A  ┌─────────────┐
   └──────▶│   agent     │◀──────│  perf-a2a   │
           │   :8080     │facade │   :9000     │
           └──────┬──────┘ :9001 └──────┬──────┘
                  │ MCP                 │ stdio MCP
                  ▼                     ▼
     Aurora PG / RDS MySQL /      RDS SQL Server
     MSK / Prometheus / 로그       (Query Store/DMV)
```

| 디렉토리 | 내용 |
|---|---|
| `dbaops/` | [blait/DBAOps-Agent](https://github.com/blait/DBAOps-Agent) `feat/ec2-allinone-slack` 스냅샷 (LangGraph agent + mcp-router + Streamlit + Slack bot). 원본 레포는 수정하지 않음 |
| `perf-agent/` | SQL Server 쿼리 성능 에이전트 (Strands + stdio MCP 도구 13개) + A2A 서버/파사드 + Streamlit |
| `docker-compose.yml` | 두 스택 통합 기동 (dbaops 4 + perf 3 서비스) |

## Quick start

```bash
cp .env.example .env      # AWS 리전, Bedrock 모델, Slack 토큰, DB 시크릿
docker compose up -d --build
```

- DBAOps UI → http://<host>:8501
- Perf UI → http://<host>:8502 (perf 채팅 / ops 채팅 / 연동 관리)
- Slack → 채널에서 `@DBAOps 질문` (Socket Mode, 공개 엔드포인트 불필요)

상세 가이드:
- perf 에이전트 매뉴얼 — [perf-agent/MANUAL.md](perf-agent/MANUAL.md)
- perf 기능/사용례 — [perf-agent/AGENT_GUIDE.md](perf-agent/AGENT_GUIDE.md)
- DBAOps allinone — [dbaops/deploy/ec2-allinone/README.md](dbaops/deploy/ec2-allinone/README.md)
- Slack 앱 설정 — [dbaops/deploy/ec2-allinone/SLACK_SETUP.md](dbaops/deploy/ec2-allinone/SLACK_SETUP.md)

## A2A 연동 구조

- **perf-a2a (:9000)** — perf 에이전트를 A2A 서버로 노출. agent card:
  `http://perf-a2a:9000/.well-known/agent-card.json`
- **ops-facade (:9001)** — DBAOps agent는 HTTP(:8080/invocations)만 말하므로,
  A2A ↔ HTTP를 변환하는 파사드. 양방향: 파사드도 perf-a2a에 A2A로 물어볼 수 있다.
- 사용자가 어느 쪽 UI에서 질문하든, 에이전트가 스스로 판단해 상대에게
  `a2a_send_message`로 질문하고 답을 종합한다.

## Slack — Bot Token 방식

webhook이 아니라 **bot token(xoxb-…)** 방식. DBAOps slack-bot(Socket Mode 대화형)과
perf 에이전트의 알림 도구(`send_slack_notification`, chat.postMessage)가
같은 토큰을 공유한다. `.env`에 `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_CHANNEL` 설정.

## 요구 사항

- EC2 instance role: `bedrock:InvokeModel*`, `secretsmanager:GetSecretValue`
- Bedrock 모델 액세스 (기본: DBAOps=Opus 4.7, perf=Sonnet 4.5 — `.env`로 변경)
- 분석 대상: RDS SQL Server(Query Store 권장), Aurora PG/MySQL/MSK/Prometheus(선택)
