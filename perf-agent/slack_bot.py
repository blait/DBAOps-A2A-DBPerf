"""
slack_bot.py - Perf 에이전트 전용 Slack 봇 (Socket Mode).

DBAOps의 slack_bot(dbaops/slack_bot/bot.py, @dbaagent)과 별개의 앱/프로세스로,
perf A2A 서버(:9100)에 질문을 전달한다. 채널에서 @perfagent(앱 display_name)로 멘션.

  Slack ──wss(Socket Mode)── [이 봇] ──A2A──▶ dbperf-a2a :9100 (LangGraph perf)

- 같은 스레드 = 같은 세션 (thread_ts → A2A context_id → LangGraph thread_id)
- 스레드 안 후속 질문은 멘션 없이도 응답 (한번 멘션으로 시작한 스레드만)
- 봇 display_name은 Slack 앱 설정이 결정 — 코드는 이름과 무관 (user ID 멘션 처리)

env (dbaops와 분리):
  PERF_SLACK_BOT_TOKEN  xoxb-...   (perfagent 앱)
  PERF_SLACK_APP_TOKEN  xapp-...
  PERF_A2A_URL          http://127.0.0.1:9100
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import Message, Part, Role, TextPart
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("perf-slack")

PERF_A2A_URL = os.environ.get("PERF_A2A_URL", "http://127.0.0.1:9100")
A2A_TIMEOUT = int(os.environ.get("A2A_CLIENT_TIMEOUT", "600"))

app = App(token=os.environ["PERF_SLACK_BOT_TOKEN"])

# 멘션으로 대화를 시작한 스레드 — 이후 멘션 없는 후속 질문에도 응답
_ACTIVE_THREADS: set[str] = set()


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


async def _ask_perf(question: str, context_id: str) -> str:
    async with httpx.AsyncClient(timeout=A2A_TIMEOUT) as hc:
        card = await A2ACardResolver(httpx_client=hc, base_url=PERF_A2A_URL).get_agent_card()
        client = ClientFactory(ClientConfig(httpx_client=hc, streaming=False)).create(card)
        msg = Message(role=Role.user, parts=[Part(TextPart(text=question))],
                      message_id=uuid.uuid4().hex, context_id=context_id)

        def texts(parts):
            return [getattr(getattr(p, "root", p), "text", None)
                    for p in (parts or []) if getattr(getattr(p, "root", p), "text", None)]

        chunks: list[str] = []
        async for ev in client.send_message(msg):
            if isinstance(ev, tuple):
                task = ev[0]
                for art in (getattr(task, "artifacts", None) or []):
                    chunks += texts(getattr(art, "parts", None))
                status = getattr(task, "status", None)
                if status and getattr(status, "message", None):
                    chunks += texts(getattr(status.message, "parts", None))
            else:
                chunks += texts(getattr(ev, "parts", None))
        return "\n".join(chunks).strip() or "(응답 없음)"


def _post_chunked(client, channel: str, thread_ts: str, text: str) -> None:
    """Slack 메시지 길이 제한(4000자) 대응 — 마크다운 유지한 채 분할.

    에이전트 리포트는 표준 마크다운(## 헤더, **굵게**)이라 Block Kit의
    `markdown` 블록으로 보내야 제대로 렌더링된다. 블록 미지원 워크스페이스나
    렌더 실패(invalid_blocks 등) 시 기존 plain text로 폴백.
    """
    limit = 3800
    while text:
        cut = text[:limit]
        if len(text) > limit:
            nl = cut.rfind("\n")
            if nl > limit // 2:
                cut = cut[:nl]
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                blocks=[{"type": "markdown", "text": cut}],
                text=cut[:150],  # 알림 미리보기/블록 실패 폴백용
            )
        except Exception:  # noqa: BLE001 — invalid_blocks 등
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=cut)
        text = text[len(cut):].lstrip("\n")


def _run_chat(client, channel: str, thread_ts: str, question: str) -> None:
    context_id = "slk-" + thread_ts.replace(".", "")
    status = client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text="⏳ 쿼리 성능 분석 중… (도구 호출 포함 수 분 걸릴 수 있어요)")
    try:
        answer = asyncio.run(_ask_perf(question, context_id))
        _post_chunked(client, channel, thread_ts, answer)
        client.chat_update(channel=channel, ts=status["ts"], text="✅ 분석 완료")
    except Exception as e:  # noqa: BLE001
        logger.exception("perf chat failed")
        try:
            client.chat_update(channel=channel, ts=status["ts"], text=f"❌ 실행 오류: {e!r}")
        except Exception:  # noqa: BLE001
            pass


@app.event("app_mention")
def on_mention(body, client):
    ev = body["event"]
    channel = ev["channel"]
    thread_ts = ev.get("thread_ts") or ev["ts"]
    question = _strip_mention(ev.get("text", ""))
    if not question:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                text="질문을 함께 적어주세요.\n예: `mysql-poc 풀스캔 많은 테이블 봐줘` / "
                                     "`SQL Server 블로킹 확인` / `pg-test 상위 쿼리`")
        return
    _ACTIVE_THREADS.add(thread_ts)
    threading.Thread(target=_run_chat, args=(client, channel, thread_ts, question),
                     daemon=True).start()


@app.event("message")
def on_message(body, client):
    """활성 스레드 안의 멘션 없는 후속 질문 처리. 그 외 메시지는 전부 무시."""
    ev = body.get("event", {})
    if ev.get("bot_id") or ev.get("subtype"):
        return
    thread_ts = ev.get("thread_ts")
    text = ev.get("text", "")
    if not thread_ts or thread_ts not in _ACTIVE_THREADS:
        return
    if "<@" in text:          # 멘션 포함 → app_mention 핸들러가 처리
        return
    question = text.strip()
    if question:
        threading.Thread(target=_run_chat,
                         args=(client, ev["channel"], thread_ts, question),
                         daemon=True).start()


def main():
    logger.info("Perf Slack bot starting (Socket Mode)… → %s", PERF_A2A_URL)
    SocketModeHandler(app, os.environ["PERF_SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
