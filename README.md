# DBAOps-A2A-DBPerf

**DBAOps-Agent × SQL Server Query Performance Agent — A2A 프로토콜 연동 통합본.**

DB/인프라 RCA 에이전트(DBAOps)와 SQL Server 쿼리 성능 전문 에이전트(DBPerf)를
한 EC2 호스트에서 **docker 없이 venv + systemd**로 함께 띄우고,
두 에이전트가 **A2A로 서로 질문·협업**한다.

```
EC2 호스트 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
│
├─ DBAOps (dbaops/)                                       사용자 브라우저
│   ├─ dbaops-mcp-router   :9000  MCP 도구 서빙          ┌──────┴──────┐
│   ├─ dbaops-agent        :8080  LangGraph RCA(HTTP)    ▼             ▼
│   ├─ dbaops-a2a          :9102  ★native A2A★     dbaops UI     dbperf UI
│   ├─ dbaops-streamlit    :8501  DBAOps UI          :8501         :8502
│   └─ dbaops-slack-bot           Slack(@DBAOps)                    │ A2A
│                                    ▲                              ▼
├─ DBPerf (perf-agent/)             │ A2A ⇄ (native, 파사드 없음) ┌──────────┐
│   ├─ dbperf-a2a          :9100  ──┴──────────────────────────▶│ perf-a2a │
│   └─ dbperf-streamlit    :8502                        stdio MCP│(13 tools)│
│                                                                └────┬─────┘
└─ 모두 /opt/dbaops/venv 공유, 127.0.0.1 통신              pymssql ▼
                                                            RDS SQL Server
```

두 에이전트 모두 **native A2A**로 직접 대화한다 (통역 파사드 없음):
`dbperf-a2a(:9100) ⇄ dbaops-a2a(:9102)`. `dbaops-a2a`는 DBAOps 그래프(`invoke_single`)를
a2a-sdk로 직접 감싼 서버로, 원본 `dbaops_agent` 패키지는 수정하지 않고 `dbaops/a2a_server.py`
파일만 추가했다.

| 디렉토리 | 내용 |
|---|---|
| `dbaops/` | [blait/DBAOps-Agent](https://github.com/blait/DBAOps-Agent) `feat/ec2-allinone-slack` 스냅샷. **vanilla(venv+systemd, docker 불필요)** 배포 경로 포함. 원본 레포는 무수정 |
| `perf-agent/` | SQL Server 쿼리 성능 에이전트 (Strands + stdio MCP 도구 13개) + A2A 서버 + Streamlit |
| `deploy/` | 통합 `install.sh` / `update.sh` + systemd 유닛(dbaops-a2a + perf 2개) |

## Quick start (docker 없음)

```bash
git clone https://github.com/blait/DBAOps-A2A-DBPerf.git ~/DBAOps-A2A-DBPerf
cd ~/DBAOps-A2A-DBPerf
bash deploy/install.sh          # OS 패키지 + venv + systemd 유닛(dbaops 4 + a2a·perf 3) 등록·기동
```

`install.sh`가 하는 일:
1. `dbaops/deploy/ec2-vanilla/install.sh` 위임 → python3.12/node20/venv + DBAOps 4개 유닛
2. perf-agent 의존성 + a2a-sdk 설치, dbaops-a2a·dbperf-a2a·dbperf-streamlit 유닛 등록·기동
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
| dbaops-a2a | 9102 | ✗ |
| dbperf-streamlit | 8502 | ✓ |

SG 인바운드는 8501/8502만(접속할 IP 한정). A2A·MCP·agent 포트는 전부 127.0.0.1 내부 통신.

## A2A 연동 구조

- **dbperf-a2a (:9100)** — perf 에이전트 A2A 서버. card: `http://127.0.0.1:9100/.well-known/agent-card.json`
- **dbaops-a2a (:9102)** — DBAOps RCA 에이전트 A2A 서버(native). `dbaops/a2a_server.py`가
  DBAOps `invoke_single`을 a2a-sdk로 직접 감쌈. 원본 dbaops_agent 패키지 무수정.
- 두 서버가 서로 A2A client 도구를 가져 **양방향** 직접 통신(파사드/HTTP 변환 없음).
  어느 UI에서 질문하든 에이전트가 스스로 상대에게 `a2a_send_message`로 물어 답을 종합.
  무한 위임은 역할 경계 프롬프트로 방지.

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
systemctl status dbaops-agent dbaops-a2a dbperf-a2a dbperf-streamlit --no-pager
journalctl -u dbperf-a2a -f
cd ~/DBAOps-A2A-DBPerf && git pull && bash deploy/update.sh   # 코드 업데이트
```
