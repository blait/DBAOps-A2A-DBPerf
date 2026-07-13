# Slack 봇 연결 가이드 (Socket Mode)

DBAOps 에이전트를 Slack 에 붙인다. **Socket Mode** 라서 공개 엔드포인트·도메인·인증서가
전혀 필요 없다 — 봇이 Slack 으로 나가는 WebSocket 만 연결한다(프라이빗 EC2 에서 동작).

소요: 약 5분. 토큰 2개 발급 → `.env` 에 넣기 → 컨테이너 1개 기동 → 채널에서 멘션.

---

## 1. Slack 앱 생성 (매니페스트로 한 번에)

1. https://api.slack.com/apps → **Create New App** → **From a manifest**
2. 워크스페이스 선택
3. 아래 YAML 을 붙여넣기 → Create

```yaml
display_information:
  name: DBAOps Agent
  description: DB/인프라 RCA 분석 에이전트
  background_color: "#2c2d30"
features:
  bot_user:
    display_name: DBAOps
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read   # 멘션 수신
      - chat:write          # 메시지 전송
      - files:write         # 차트 PNG 첨부
      - channels:history    # 스레드 내 후속 질문 수신 + 스레드 이력 주입(공개 채널)
      - groups:history      # 〃 (비공개 채널)
settings:
  event_subscriptions:
    bot_events:
      - app_mention         # @DBAOps 멘션 시 호출
      - message.channels    # 스레드 내 멘션 없는 후속 질문(공개 채널)
      - message.groups      # 〃 (비공개 채널)
  interactivity:
    is_enabled: true        # 향후 인터랙션용 — 현재 버튼 UX 없음
  socket_mode_enabled: true # 공개 엔드포인트 불필요
  org_deploy_enabled: false
```

> DM 으로도 대화하려면 scope `im:history`, 이벤트 `message.im` 을 추가한다.

> 회사 워크스페이스가 **앱 승인제**라면 관리자 승인이 필요할 수 있다(Enterprise Grid 등).
> 그 경우 "Request to Install" 후 워크스페이스 관리자 승인을 받는다.

---

## 2. 토큰 2개 발급

### 2-1. App Token (`xapp-...`) — Socket Mode 연결용
1. 좌측 **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**
2. 이름 아무거나(예: `socket`), scope `connections:write` 추가 → Generate
3. 나온 `xapp-...` 복사 → 이게 `SLACK_APP_TOKEN`

### 2-2. Bot Token (`xoxb-...`) — 메시지 전송용
1. 좌측 **OAuth & Permissions** → **Install to Workspace** → 승인
2. 설치 후 나오는 **Bot User OAuth Token** `xoxb-...` 복사 → 이게 `SLACK_BOT_TOKEN`

> 매니페스트로 만들었으면 scope/이벤트는 이미 설정돼 있다. 토큰 2개만 받으면 된다.

---

## 2-3. (이미 만든 앱이면) 후속 질문용 이벤트 추가

처음부터 위 매니페스트로 만들었으면 건너뛴다. **`app_mention` 만 있는 기존 앱**에
스레드 후속 질문(멘션 없이 이어 말하기)을 켜려면 이벤트 2개를 추가한다.

1. 좌측 **Event Subscriptions** → **Subscribe to bot events** 펼치기
2. **Add Bot User Event** (또는 "Find and add an event" 검색창)에서 추가:
   - `message.channels` (공개 채널)
   - `message.groups` (비공개 채널이면)
   - `message.im` (DM 으로도 쓰면)
   - → `channels:history` / `groups:history` / `im:history` 스코프가 **자동으로 붙는다**
3. 하단 **Save Changes**
4. 상단에 뜨는 노란 배너 **reinstall your app** → **Reinstall to Workspace** → 승인
   - 토큰은 그대로 유지된다. `.env` 안 바꿔도 됨.

> `message.*` 를 구독하면 봇이 채널의 모든 메시지를 받지만, 봇은 (a) 스레드 밖,
> (b) 멘션 포함(=`app_mention` 이 처리), (c) 봇 자신의 메시지를 모두 무시한다.
> 실제로 반응하는 건 **"이미 분석을 시작한 스레드 안의 일반 메시지"** 뿐이다.

---

## 3. EC2 에 토큰 넣고 봇 기동

EC2 에 SSH 접속 후:

```bash
cd ~/dbaops/deploy/ec2-allinone

# .env 에 토큰 2줄 추가/수정 (xoxb / xapp)
nano .env
#   SLACK_BOT_TOKEN=xoxb-실제토큰
#   SLACK_APP_TOKEN=xapp-실제토큰
#   STREAMLIT_URL=http://3.91.145.76:8501   # 리포트 차트 링크용(선택)

# slack-bot 컨테이너만 빌드+기동 (나머지 3개는 그대로)
docker compose up -d --build slack-bot

# 로그 확인 — "DBAOps Slack bot starting (Socket Mode)…" + 연결 성공이 떠야 함
docker compose logs -f slack-bot
```

정상이면 로그에 Socket Mode 연결 성공이 보이고, 봇이 워크스페이스에서 🟢 온라인이 된다.

---

## 4. 채널에 초대 + 테스트

```
# 분석할 Slack 채널에서
/invite @DBAOps

# 멘션해서 질문
@DBAOps 최근 1시간 Aurora CPU 어때?
```

흐름:
1. 멘션하면 봇이 **바로 답을 시작**한다 (버튼 없음, 대화형).
2. 스레드에 진행상황 표시 ("⏳ 확인 중…" → "💬 Aurora CPU 메트릭 볼게요" → "✅ 완료")
3. 최종 답변 게시. 차트가 있으면 PNG 첨부.

시간 범위는 기본 **최근 1시간**이며, 질문에 "최근 6시간" 등 기간을 말하면 그것이 우선 적용된다.

### 4-1. 스레드에서 이어 묻기 (대화 연속성)

**같은 스레드 = 같은 세션.** 멘션 없이 그냥 이어 말하면 에이전트가
**이전 대화 맥락을 기억**하며 자연스럽게 이어간다.

```
@DBAOps 최근 1시간 Aurora CPU 어때?   ← 멘션 → 바로 분석 시작
  ↳ 그럼 메모리는?                    ← 멘션 없이 → 같은 세션으로 이어서 답
  ↳ slow query 있었어?                ← 맥락 기억한 채 이어감
```

- 세션 키 = Slack 스레드 타임스탬프(`thread_ts`). 새 멘션(새 스레드)은 새 대화.
- 세션 메모리(InMemorySaver)는 재시작 시 휘발되지만, 봇이 **매 요청마다 Slack 스레드 대화
  이력(최대 4,000자)을 자동 주입**하므로 재시작 후에도 스레드 맥락이 이어진다.

> 2-3 의 `message.*` 이벤트가 켜져 있어야 후속 질문이 동작한다. 안 켜져 있으면
> 스레드에서 멘션 없이 말해도 봇이 반응하지 않는다(멘션은 계속 정상 동작).

---

## 5. 동작 원리 (참고)

```
Slack ──outbound wss(Socket Mode)── [slack-bot 컨테이너]
                                          │ 같은 EC2 내부
                                          │ AGENT_HTTP_URL=http://agent:8080/invocations
                                          ▼
                                     [agent] ── MCP ──> [mcp-router] ──> AWS/DB
```

- 봇은 UI 와 **같은 `agentcore_client.invoke_stream`** 을 공유 — Streamlit 과 완전히 동일한 분석 경로.
- 토큰은 `.env` 에만 두고 git 에 넣지 않는다(`.env` 는 .gitignore 대상).

---

## 6. 트러블슈팅

| 증상 | 확인 |
|---|---|
| 봇이 오프라인 | `docker compose logs slack-bot` — 토큰 오타/만료, App Token scope `connections:write` |
| 멘션 무반응 | Event Subscriptions 에 `app_mention` 구독됐나, 채널에 `/invite` 했나 |
| 스레드 후속질문 무반응 | 2-3 의 `message.channels`(또는 groups/im) 구독 + 재설치했나, 해당 스레드에서 멘션으로 대화를 시작했나 |
| 후속질문이 새 대화처럼 | 스레드 이력 주입 실패 — `channels:history` 스코프 확인, 봇 로그에서 `thread history fetch failed` 검색. 같은 스레드에서 묻고 있나 |
| "실행 오류" | agent 로그(`docker compose logs agent`), `bedrock:InvokeModel` 권한, 라우터 상태 |
| 토큰 갱신 후 | `docker compose up -d slack-bot` 재기동(`.env` 다시 읽음) |

---

## 7. 봇 끄기 / 떼기

```bash
docker compose stop slack-bot     # 잠시 끄기 (다른 3개는 유지)
docker compose rm -sf slack-bot   # 컨테이너 제거
# Slack 쪽: api.slack.com/apps → 앱 삭제 또는 워크스페이스에서 제거
```
