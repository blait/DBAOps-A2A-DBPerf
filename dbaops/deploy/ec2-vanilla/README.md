# DBAOps-Agent 생(vanilla) EC2 배포 — docker 없이

docker 를 쓸 수 없는(또는 원치 않는) 환경을 위한 배포 방법.
**venv + systemd** 로 4개 서비스를 EC2 호스트에서 직접 구동한다.

> docker 가 가능한 환경이라면 [`../ec2-allinone/README.md`](../ec2-allinone/README.md)(docker compose)를 권장 —
> OS 의존성(파이썬/노드 버전)을 이미지가 격리해줘서 환경 편차 문제가 없다.
> 두 방식은 **기능이 완전히 동일**하다 (같은 코드, 같은 연결설정 UI, 같은 Slack 봇).

```
EC2 호스트 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
├─ systemd: dbaops-mcp-router   :9000   MCP 도구 서빙 (56개/10타깃)
├─ systemd: dbaops-agent        :8080   LangGraph 에이전트
├─ systemd: dbaops-streamlit    :8501   웹 UI (외부 노출)
└─ systemd: dbaops-slack-bot            Slack Socket Mode (선택)
   모든 서비스가 /opt/dbaops/venv 하나를 공유, 서로 127.0.0.1 로 통신
```

---

## 1. 사전 조건

| 항목 | 내용 |
|---|---|
| EC2 | Amazon Linux 2023 권장 (Ubuntu 22.04+ 도 지원). t3.large 이상, 디스크 20GB+ |
| Instance role | `DatabaseAdministrator` 관리형 + `bedrock:InvokeModel` 인라인 |
| SG 인바운드 | 8501 (Streamlit, 접속할 IP 만) |
| SG 아웃바운드 | 443 (Bedrock/AWS API/Slack), DB 포트 (5432/3306), Prometheus (9090) |
| DB 접근 | EC2 → 분석 대상 RDS/Prometheus 라우팅 가능해야 함 (같은 VPC 또는 피어링) |

docker · ECR · 외부 레지스트리 접근은 **불필요**. 필요한 OS 패키지(python3.12,
nodejs20, libpq, 한글 폰트)는 설치 스크립트가 dnf/apt 로 설치한다.

## 2. 설치 (10분)

```bash
# 1) 코드 받기
git clone https://github.com/blait/DBAOps-Agent.git ~/dbaops
cd ~/dbaops/deploy/ec2-vanilla

# 2) 설치 — OS 패키지 + venv + systemd 유닛 등록 + 기동까지 한 번에
bash install.sh
```

`install.sh` 가 하는 일:
1. `dnf`(AL2023) 또는 `apt`(Ubuntu) 로 python3.12 / nodejs20 / libpq / 한글 폰트 설치
2. `/opt/dbaops/venv` 생성 후 4개 서비스 의존성 설치 (수 분 소요)
3. MySQL MCP 서버(node) `npm install`
4. `/etc/dbaops/dbaops.env` 생성 (이미 있으면 유지)
5. systemd 유닛 4개 설치 → mcp-router / agent / streamlit **자동 기동**
   (slack-bot 은 토큰이 env 에 있을 때만)

재실행해도 안전하다(멱등).

## 3. 환경변수

`/etc/dbaops/dbaops.env` (root 600):

```bash
AWS_REGION=ap-northeast-2
BEDROCK_REGION=ap-northeast-2
BEDROCK_MODEL_ID=global.anthropic.claude-opus-4-7

# Slack 쓸 때만 (발급: ../ec2-allinone/SLACK_SETUP.md)
#SLACK_BOT_TOKEN=xoxb-...
#SLACK_APP_TOKEN=xapp-...
#STREAMLIT_URL=http://<EC2-IP>:8501
```

수정 후 반영:

```bash
sudo systemctl restart dbaops-mcp-router dbaops-agent dbaops-streamlit
# Slack 토큰을 처음 채웠다면:
sudo systemctl enable --now dbaops-slack-bot
```

## 4. 정상 확인

```bash
systemctl status dbaops-mcp-router dbaops-agent dbaops-streamlit --no-pager
curl -s http://localhost:9000/healthz          # {"status": "ok", ...}
curl -s http://localhost:8501 -o /dev/null -w '%{http_code}\n'   # 200
```

브라우저에서 `http://<EC2-IP>:8501` 접속 → **🔌 MCP 연결설정** 탭에서
DB/Prometheus 연결 입력 (docker 방식과 완전히 동일 —
[`docs/ONBOARDING.md`](../../docs/ONBOARDING.md) §5 참조).

## 5. 운영

```bash
# 로그 (journald)
journalctl -u dbaops-agent -f
journalctl -u dbaops-mcp-router --since "10 min ago"

# 재시작 / 중지
sudo systemctl restart dbaops-agent
sudo systemctl stop dbaops-slack-bot

# 코드 업데이트
cd ~/dbaops && git pull
bash deploy/ec2-vanilla/update.sh    # pip/npm 갱신 + 유닛 갱신 + 재시작
```

서비스는 크래시 시 자동 재시작(`Restart=always`)되고 부팅 시 자동 기동된다.

## 6. Prometheus 모니터링 (선택)

vanilla 배포에서는 exporter 도 바이너리로 직접 띄운다. 고객이 **자체 Prometheus 를
운영 중이면 이 절은 통째로 생략** — 연결설정에 그 URL 만 입력하면 된다.

exporter(postgres/mysqld/node)는 전부 정적 Go 바이너리라 설치가 간단하다:

```bash
# 예: node_exporter (EC2 호스트 메트릭)
cd /opt/dbaops
curl -sLO https://github.com/prometheus/node_exporter/releases/download/v1.8.2/node_exporter-1.8.2.linux-amd64.tar.gz
tar xzf node_exporter-*.tar.gz && sudo mv node_exporter-*/node_exporter /usr/local/bin/
# systemd 유닛 작성 후 enable --now (prometheus/postgres_exporter/mysqld_exporter 동일 패턴)
```

Prometheus 본체·exporter 구성 값(스크레이프 대상, 자격증명 처리)은 docker 경로의
[`../ec2-allinone/prometheus/prometheus.yml`](../ec2-allinone/prometheus/prometheus.yml) 과
[`docs/ONBOARDING.md`](../../docs/ONBOARDING.md) §4-4 를 그대로 참고하면 된다
(대상 주소만 `localhost:<port>` 로 바뀜).

## 7. 트러블슈팅

| 증상 | 확인 |
|---|---|
| `install.sh` 에서 python3.12 없음 | AL2 (2023 아님) 이거나 오래된 Ubuntu — OS 업그레이드 필요 |
| pip 설치 중 `drain3 requires cachetools==4.2.1` 경고 | 무해 — 서비스 동작에 영향 없음 (실검증 완료) |
| streamlit `Port 8501 is not available` | 8501 을 다른 프로세스(docker 등)가 점유 — `ss -ltnp \| grep 8501` 로 확인 후 정리 |
| mcp-router 기동 실패 | `journalctl -u dbaops-mcp-router -n 50` — 대부분 pip 의존성/PATH 문제 |
| 연결 테스트에서 MySQL 실패 | `node --version` 20+ 확인, `/opt/dbaops/mysql-mcp/node_modules` 존재 확인 |
| Slack 차트 한글 깨짐 | `fc-list \| grep -i noto` 로 CJK 폰트 확인 (install.sh 가 설치함) |
| agent 가 Bedrock 오류 | instance role 에 `bedrock:InvokeModel` 있는지, `BEDROCK_MODEL_ID` 권한 리전 확인 |
| 부팅 후 서비스 안 뜸 | `systemctl is-enabled dbaops-agent` — disabled 면 `sudo systemctl enable <unit>` |

## 8. docker 방식과의 차이 요약

| | docker (ec2-allinone) | vanilla (이 문서) |
|---|---|---|
| 사전 요구 | docker + compose | 없음 (스크립트가 OS 패키지 설치) |
| 파이썬/노드 | 이미지에 내장 | 호스트에 직접 설치 (AL2023/Ubuntu22+) |
| 프로세스 관리 | compose `restart: unless-stopped` | systemd `Restart=always` |
| 로그 | `docker compose logs` | `journalctl -u <unit>` |
| 업데이트 | `git pull && compose up -d --build` | `git pull && bash update.sh` |
| Prometheus 스택 | `--profile prometheus` 로 동봉 | exporter 바이너리 직접 설치 (§6) |
| 기능 | 동일 | 동일 |
