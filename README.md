# DBAOps-A2A-DBPerf

**DBAOps-Agent × SQL Server Query Performance Agent — A2A 프로토콜 연동 통합본.**

DB/인프라 RCA 에이전트(DBAOps)와 SQL Server 쿼리 성능 전문 에이전트(DBPerf)를
한 EC2 호스트에서 **docker 없이 venv + systemd**로 함께 띄우고,
두 에이전트가 **A2A로 서로 질문·협업**한다.

```
EC2 호스트 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
│
├─ DBAOps (vanilla, dbaops/)                         ┌─ 사용자 브라우저 ─┐
│   ├─ dbaops-mcp-router   :9000  MCP 도구 서빙       │                  │
│   ├─ dbaops-agent        :8080  LangGraph RCA       ▼                  ▼
│   ├─ dbaops-streamlit    :8501  DBAOps UI ──────────┘         perf UI :8502
│   └─ dbaops-slack-bot           Slack(@DBAOps 멘션)              │ A2A
│                                    ▲                            │
├─ DBPerf (perf-agent/)             │ HTTP(:8080)                 ▼
│   ├─ dbperf-a2a          :9100  ──┼── A2A ⇄ ──┐         ┌──────────────┐
│   ├─ dbperf-ops-facade   :9101  ──┘           └────────▶│  perf-a2a    │
│   └─ dbperf-streamlit    :8502                  stdio MCP│  (13 tools)  │
│                                                          └──────┬───────┘
└─ 모두 /opt/dbaops/venv 공유, 127.0.0.1 통신          pymssql ▼
                                                        RDS SQL Server
```

| 디렉토리 | 내용 |
|---|---|
| `dbaops/` | [blait/DBAOps-Agent](https://github.com/blait/DBAOps-Agent) `feat/ec2-allinone-slack` 스냅샷. **vanilla(venv+systemd, docker 불필요)** 배포 경로 포함. 원본 레포는 무수정 |
| `perf-agent/` | SQL Server 쿼리 성능 에이전트 (Strands + stdio MCP 도구 13개) + A2A 서버/파사드 + Streamlit |
| `deploy/` | 통합 `install.sh` / `update.sh` + perf systemd 유닛 3개 |

## Quick start (docker 없음)

```bash
git clone https://github.com/blait/DBAOps-A2A-DBPerf.git ~/DBAOps-A2A-DBPerf
cd ~/DBAOps-A2A-DBPerf
bash deploy/install.sh          # OS 패키지 + venv + systemd 7개 유닛 등록·기동
```

`install.sh`가 하는 일:
1. `dbaops/deploy/ec2-vanilla/install.sh` 위임 → python3.12/node20/venv + DBAOps 4개 유닛
2. perf-agent 의존성 설치 + perf 3개 systemd 유닛 등록·기동
3. `/etc/dbaops/dbaops.env`에 perf 기본값 추가 (공유 env)

접속:
- DBAOps UI → http://\<EC2-IP\>:8501
- **Perf UI → http://\<EC2-IP\>:8502** (perf 채팅 / ops 채팅 / 연동 관리)
- Slack → 채널에서 `@DBAOps 질문` (Socket Mode)

상세: [perf-agent/MANUAL.md](perf-agent/MANUAL.md) · [perf-agent/AGENT_GUIDE.md](perf-agent/AGENT_GUIDE.md) ·
[dbaops/deploy/ec2-vanilla/README.md](dbaops/deploy/ec2-vanilla/README.md) ·
[dbaops/deploy/ec2-allinone/SLACK_SETUP.md](dbaops/deploy/ec2-allinone/SLACK_SETUP.md)

## 포트 배치 (충돌 방지)

| 서비스 | 포트 | 외부노출 |
|---|---|---|
| dbaops-mcp-router | 9000 | ✗ |
| dbaops-agent | 8080 | ✗ |
| dbaops-streamlit | 8501 | ✓ |
| dbperf-a2a | 9100 | ✗ |
| dbperf-ops-facade | 9101 | ✗ |
| dbperf-streamlit | 8502 | ✓ |

SG 인바운드는 8501/8502만(접속할 IP 한정). A2A·MCP·agent 포트는 전부 127.0.0.1 내부 통신.

## A2A 연동 구조

- **dbperf-a2a (:9100)** — perf 에이전트를 A2A 서버로 노출.
  agent card: `http://127.0.0.1:9100/.well-known/agent-card.json`
- **dbperf-ops-facade (:9101)** — DBAOps agent는 HTTP(:8080/invocations)만 말하므로
  A2A ↔ HTTP를 변환하는 파사드. 양방향: 파사드도 perf-a2a에 A2A로 물어본다.
- 어느 UI에서 질문하든, 에이전트가 스스로 판단해 상대에게 `a2a_send_message`로
  질문하고 답을 종합한다. 무한 위임은 역할 경계 프롬프트로 방지.

## Slack — Bot Token 방식

webhook이 아니라 **bot token(xoxb-…)**. DBAOps slack-bot(Socket Mode 대화형)과
perf 알림 도구(`send_slack_notification`, chat.postMessage)가 같은 토큰을 공유한다.
`/etc/dbaops/dbaops.env`에 `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_CHANNEL`.
토큰 발급: [dbaops/deploy/ec2-allinone/SLACK_SETUP.md](dbaops/deploy/ec2-allinone/SLACK_SETUP.md)

## 요구 사항

- EC2: Amazon Linux 2023 권장(Ubuntu 22.04+ 가능), t3.large+, 디스크 20GB+
- Instance role: `DatabaseAdministrator` 관리형 + `bedrock:InvokeModel` + `secretsmanager:GetSecretValue`
- Bedrock 모델 액세스 (DBAOps=Opus 4.7, perf=Sonnet 4.5 — env로 변경 가능)
- EC2 → 분석 대상 RDS(SQL Server/Aurora/MySQL)·Prometheus 네트워크 라우팅

## 운영

```bash
systemctl status dbaops-agent dbperf-a2a dbperf-ops-facade dbperf-streamlit --no-pager
journalctl -u dbperf-a2a -f
cd ~/DBAOps-A2A-DBPerf && git pull && bash deploy/update.sh   # 코드 업데이트
```
