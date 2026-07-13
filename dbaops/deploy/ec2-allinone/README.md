# DBAOps-Agent 올인원 EC2 배포 (docker compose)

AgentCore / Gateway / Cognito / Lambda **없이** EC2 한 대에서 전체 시스템을 구동한다.
고객이 받은 권한이 AWS 관리형 `DatabaseAdministrator`(데이터 읽기) 한 장뿐인 환경을 위한 구성.

> docker 를 쓸 수 없는 환경이라면 **[`../ec2-vanilla/README.md`](../ec2-vanilla/README.md)**
> (venv + systemd 로 호스트에 직접 구동, 기능 동일)를 사용한다.

```
EC2 (instance role: DatabaseAdministrator + bedrock:InvokeModel)
└─ docker compose
   ├─ mcp-router  :9000   MCP 도구 라우터 (AgentCore Gateway 대체)
   ├─ agent       :8080   LangGraph 파이프라인/단일 에이전트
   ├─ streamlit   :8501   웹 UI + 🔌 MCP 연결설정
   ├─ slack-bot           Socket Mode (outbound only)
   └─ (선택 --profile prometheus) prometheus / postgres-exporter / mysqld-exporter / node-exporter
```

---

## 0. 사전 조건 (인프라팀이 준비)

EC2 자체 생성·IAM role 생성은 `DatabaseAdministrator` 로는 불가하므로 **인프라팀이 제공**해야 한다.

| 항목 | 내용 |
|---|---|
| **EC2 인스턴스** | 분석 대상 DB·Prometheus 와 **같은 VPC**(또는 라우팅 가능). 권장 t3.large 이상, 디스크 30GB+ |
| **Instance profile(IAM role)** | 아래 2개 정책을 attach: `DatabaseAdministrator` (관리형) + `bedrock:InvokeModel` 인라인 |
| **Egress** | Bedrock 호출용 (NAT 또는 bedrock-runtime VPC endpoint). 이미지/패키지 pull 용 인터넷 또는 프록시 |
| **인바운드 8501** | Streamlit 접속 경로 (사내망/VPN/보안그룹 제한 권장) |
| **DB 접근 SG** | EC2 → 고객 PG/MySQL(5432/3306), Prometheus(9090) 인바운드 허용 |

instance role 에 추가할 Bedrock 인라인 정책:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "BedrockInvoke",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*"
  }]
}
```

> `DatabaseAdministrator` 가 이미 커버: rds:* / pi:* / cloudwatch / logs / dynamodb / s3 read.
> 따라서 MCP 도구의 데이터 조회는 추가 권한 없이 동작한다.

---

## 1. EC2 부트스트랩 (docker 설치)

Amazon Linux 2023 기준:

```bash
sudo dnf -y install docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user      # 재로그인 후 sudo 없이 docker 사용
# docker compose v2 플러그인
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -sL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
```

---

## 2. 코드 가져오기 + 설정

```bash
git clone https://github.com/blait/DBAOps-Agent.git dbaops
cd dbaops/deploy/ec2-allinone        # ← 이후 모든 docker compose 명령은 이 디렉토리 안에서 실행

cp .env.example .env
nano .env        # 편집기로 .env 를 연다 (vi 써도 됨)
```

`.env` 에서 채울 값 (최소 AWS_REGION 만 맞으면 동작):

| 키 | 설명 | 필수 |
|---|---|---|
| `AWS_REGION` | EC2/Bedrock 리전 (예: `ap-northeast-2`) | ✅ |
| `BEDROCK_MODEL_ID` | 기본값 그대로 두면 됨 | — |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack 쓸 때만 (§6 참조) | Slack 시 |
| `STREAMLIT_URL` | Slack 메시지의 차트 링크용 (예: `http://<ec2-ip>:8501`) | 선택 |
| `PROMETHEUS_PORT` | Prometheus 외부 노출 포트 (기본 9090) | 프로파일 사용 시 |
| `PG_EXPORTER_DSN` | postgres-exporter 가 붙을 PG DSN | 프로파일 사용 시 |

> `connections.json`(DB·Prometheus 연결정보)은 **지금 안 만들어도 된다.** 첫 기동 후
> Streamlit 의 🔌 연결설정 탭에서 입력하면 자동 생성된다(§4). 볼륨에 저장돼 재시작해도 유지.

---

## 3. 기동 (docker compose 가 처음이라면 이 섹션부터)

### 3-0. docker compose 가 뭘 하나 (개념)

`docker-compose.yml` 파일 한 장에 **기본 4개 서비스**(mcp-router / agent / streamlit / slack-bot)
**+ 선택 Prometheus 스택 4개**(`profiles: prometheus` — prometheus / postgres-exporter /
mysqld-exporter / node-exporter)가 정의돼 있다. `docker compose` 명령은 이 파일을 읽어서
서비스들을 **한 번에** 빌드·실행·중지한다.
하나하나 `docker run` 할 필요 없이 묶음으로 관리하는 도구라고 보면 된다.

- **이미지(image)**: 코드 + 파이썬 + 라이브러리를 통째로 구운 "실행 가능한 스냅샷". `--build` 가 이걸 만든다.
- **컨테이너(container)**: 그 이미지를 실제로 띄운 "실행 중인 프로세스". `up` 이 이걸 띄운다.
- 4개 컨테이너는 자기들끼리 내부 네트워크로 통신한다(`agent` → `mcp-router` 등). 우리가 포트를 신경 쓸 건 Streamlit `8501` 하나뿐 (prometheus 프로파일 사용 시 `9090` 도 추가).

### 3-1. 빌드 + 실행 (한 줄)

```bash
# deploy/ec2-allinone 디렉토리 안에서 실행
docker compose up -d --build
```

이 한 줄이 순서대로 하는 일:
1. `--build` → 4개 서비스(`--profile prometheus` 시 8개)의 **이미지를 빌드**(코드 복사 + 의존성 설치). 처음엔 수 분 걸린다(이후엔 캐시되어 빠름).
2. `up` → 빌드된 이미지로 **4개(프로파일 시 8개) 컨테이너를 기동**.
3. `-d` → **백그라운드(detached)** 로 실행. 터미널을 닫아도 계속 돈다. (`-d` 빼면 로그가 화면에 흐르고, Ctrl+C 누르면 멈춘다.)

> **Slack 없이 먼저 테스트**하려면 slack-bot 만 빼고 3개만 띄운다:
> ```bash
> docker compose up -d --build mcp-router agent streamlit
> ```
> Slack 토큰은 나중에 `.env` 에 넣고 `docker compose up -d --build slack-bot` 로 추가하면 된다.

### 3-2. 정상 기동 확인

```bash
docker compose ps
```
4개(또는 3개) 서비스가 모두 **`Up` / `running`** 으로 보이면 성공. `Exit` / `Restarting` 이면 문제 → 그 서비스 로그를 본다:

```bash
docker compose logs -f agent        # agent 로그를 실시간(-f)으로 (Ctrl+C 로 빠져나옴)
docker compose logs --tail=50 mcp-router   # 최근 50줄만
```

기대되는 정상 로그 예:
- `agent` → `serving on 0.0.0.0:8080`
- `slack-bot` → `Bolt app is running!`
- `streamlit` → `You can now view your Streamlit app`

이제 브라우저로 `http://<ec2-ip>:8501` 접속이 되면 다음(연결 설정) 단계로.

### 3-3. (선택) Prometheus 모니터링 스택

고객 환경에 Prometheus 가 없을 때, 동봉된 스택(prometheus + postgres-exporter +
mysqld-exporter + node-exporter)을 같은 compose 로 띄울 수 있다.

1. `.env` 에 `PG_EXPORTER_DSN` 입력 (postgres-exporter 가 붙을 PG 접속 문자열)
2. MySQL 은 파일 방식 — `cp prometheus/my.cnf.example prometheus/my.cnf` 후 MySQL 접속정보 기입.
   (비밀번호에 특수문자가 있어도 안전하도록 env 대신 파일을 쓴다. 전용 모니터링 유저 권장)
3. 기동:
   ```bash
   docker compose --profile prometheus up -d
   ```
4. Streamlit 🔌 연결설정에서 `PROMETHEUS_URL` 을 `http://prometheus:9090` 으로 입력
5. scrape 대상: `rds-postgres` / `rds-mysql` / `ec2-host`(node-exporter — EC2 호스트 CPU·메모리)

자세한 내용은 [`../../docs/ONBOARDING.md`](../../docs/ONBOARDING.md) §4-4 참조.

---

## 4. 연결 설정 (Streamlit)

1. 브라우저로 `http://<ec2-ip>:8501` 접속
2. **🔌 MCP 연결설정** 탭
3. 사용할 도구 토글 ON + 연결 정보 입력:
   - **Prometheus**: `PROMETHEUS_URL` — 동봉 스택(§3-3)이면 `http://prometheus:9090`, 외부 Prometheus 면 `http://10.0.0.10:9090`
   - **PostgreSQL**: Host/Port/DB + (User·Password) 또는 Secrets Manager ARN.
     접근 모드 `PG_ACCESS_MODE`: `restricted`(기본) | `unrestricted`(EXPLAIN·인덱스 분석 — 읽기전용 계정 필수)
   - **MySQL**: 동일
   - CloudWatch / RDS PI / S3 / aws-api: 추가 입력 없음(instance role 권한 사용)
4. **연결 테스트** 로 각 도구 ✅ 확인 (`SELECT 1` / `up` 수준의 실접속 검증) → **전체 저장**
5. (선택) 인프라 식별자(aurora writer id 등) 입력 — 비우면 에이전트가 describe 로 직접 탐색

저장하면 라우터가 자동 반영(다음 호출부터). 재시작 불필요.

> 연결정보가 어디서 자동으로 오고 무엇만 사람이 입력해야 하는지(특히 DB 비밀번호),
> 권한이 없을 때의 fallback 은 [`../../docs/CONNECTION_INFO.md`](../../docs/CONNECTION_INFO.md) 참조.

---

## 5. 사용

- **Streamlit**: 🤖 DBAOps Agent 탭에서 자연어 질문 — 단일 에이전트가 모든 도구(DB·메트릭·로그·PI)를 직접 사용
- **Slack**: 채널에 봇 초대 후 `@DBAOps 최근 1시간 Aurora CPU 어때?` → 스레드에 바로 답.
  같은 스레드 안에서는 멘션 없이 이어 물어도 맥락을 기억하며 대화가 계속된다.

---

## 6. Slack 앱 설정 (Socket Mode)

공개 엔드포인트가 필요 없다(봇이 Slack 으로 outbound WebSocket 연결).
앱 매니페스트로 한 번에 설정하는 **상세 단계별 가이드는 [`SLACK_SETUP.md`](SLACK_SETUP.md)** 참조.

요약:
1. api.slack.com/apps → From a manifest (SLACK_SETUP.md 의 YAML — `message.*` 이벤트 포함)
2. App Token(`xapp-...`) + Bot Token(`xoxb-...`) 발급
3. `.env` 에 두 토큰 입력 → `docker compose up -d --build slack-bot`
4. 대상 채널에서 `/invite @DBAOps` → `@DBAOps 질문` → 스레드에서 멘션 없이 이어 대화

---

## 7. 자주 쓰는 운영 명령 (deploy/ec2-allinone 안에서)

```bash
# 코드 갱신 후 전체 재빌드·재기동
git pull && docker compose up -d --build

# 특정 서비스 하나만 재빌드 (예: slack-bot 만 수정했을 때 — 빠름)
docker compose up -d --build slack-bot

# 한 서비스만 재시작 (코드 변경 없이, 예: 설정만 다시 읽기)
docker compose restart mcp-router

# 전체 종료 — 컨테이너만 내림. 데이터(연결설정)는 볼륨에 보존됨
docker compose down

# 전체 종료 + 볼륨 삭제 — 연결설정까지 초기화(처음 상태로)
docker compose down -v

# 실시간 로그 보기 (문제 진단 1순위)
docker compose logs -f            # 기본 4개(프로파일 시 8개) 전체
docker compose logs -f agent      # 특정 서비스만
```

> 용어: `down`(=컨테이너 제거, 데이터 유지) vs `down -v`(=볼륨까지 삭제, 연결설정 날아감).
> 평소엔 `down` 만 쓰고, 완전 초기화할 때만 `-v` 를 붙인다.

---

## 트러블슈팅

| 증상 | 확인 |
|---|---|
| 채팅이 "호출할 수 없습니다" | streamlit 의 `AGENT_HTTP_URL` env, agent 컨테이너 상태 |
| 도구 연결 ❌ | 연결설정 값, EC2→DB 보안그룹, `docker compose logs mcp-router` |
| LLM 오류(AccessDenied) | instance role 에 `bedrock:InvokeModel` 있는지, 리전/모델ID |
| stdio 도구가 안 뜸 | 라우터 로그에서 spawn 에러(자격증명/네트워크) |
| Slack 무반응 | 봇 토큰, `app_mention` 구독, 채널 초대, `docker compose logs slack-bot` |
| x86 EC2 에서 agent 빌드 실패 | `docker-compose.yml` 의 agent `platform: linux/arm64` 주석 해제 또는 ARM 인스턴스 사용 |
