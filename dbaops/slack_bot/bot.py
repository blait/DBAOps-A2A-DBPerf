"""DBAOps Slack 봇 — Socket Mode.

대화형 흐름 (Claude Code 스타일):
  1. @봇 멘션 + 질문 → 곧바로 유연한 단일 에이전트(single)가 응답.
     모드 선택 버튼 없음 — 에이전트가 질문 성격(잡담/조회/원인분석)을 스스로 판단.
  2. 같은 스레드 안에서는 멘션 없이 이어 말해도 같은 세션으로 대화가 계속됨
     (session_id = thread_ts → agent 가 이전 맥락 기억). 되묻기→답변→이어가기 자연스럽게.

Socket Mode 이므로 공개 엔드포인트 불필요 — 봇이 Slack 으로 outbound WebSocket 만 건다.
프라이빗 EC2 + egress 만으로 동작. agent 는 같은 박스의 AGENT_HTTP_URL 로 호출.

env:
  SLACK_BOT_TOKEN   xoxb-...   (chat:write, app_mentions:read, channels:history)
  SLACK_APP_TOKEN   xapp-...   (Socket Mode, connections:write)
  AGENT_HTTP_URL    http://agent:8080/invocations
  STREAMLIT_URL     (선택) 차트 전체 보기 링크
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import agentcore_client
from render import SlackThreadRenderer

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("dbaops-slack")

STREAMLIT_URL = os.environ.get("STREAMLIT_URL", "")
DEFAULT_WINDOW_HOURS = int(os.environ.get("SLACK_DEFAULT_WINDOW_HOURS", "1"))

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# 활성 스레드 집합 — 멘션으로 한 번 대화를 시작한 스레드는 멘션 없이 이어 말해도 응답.
# 봇 재시작 시 초기화(실용상 충분).
_ACTIVE_THREADS: set[str] = set()


def _session_id(thread_ts: str) -> str:
    """Slack 스레드 타임스탬프 → 안정적인 session_id. 같은 스레드 = 같은 대화."""
    return "slk-" + thread_ts.replace(".", "")


def _strip_mention(text: str) -> str:
    """'<@U123> 질문내용' → '질문내용'."""
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def _window() -> dict:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=DEFAULT_WINDOW_HOURS)
    return {"start": start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds")}


# 봇 진행상황/시스템 메시지 프리픽스 — 히스토리 주입 시 걸러냄 (대화 내용 아님).
_STATUS_PREFIXES = ("⏳", "✅", "⚠️", "❌", "⏹️", "💬", "🔧", "📊", "🔎", "🧪", "✍️", "📝")

_CTX_MAX_MSG_CHARS = 700       # 메시지 하나 최대 길이
_CTX_MAX_TOTAL_CHARS = 4000    # 주입 컨텍스트 전체 상한 (최근 대화 우선)


def _thread_context(client, channel: str, thread_ts: str, exclude_ts: str) -> str:
    """agent 세션이 없는(재시작 등) 스레드에서 이전 대화를 복원해 컨텍스트 문자열로.

    conversations.replies 로 스레드 메시지를 읽어 "사용자:/봇:" 대화록을 만든다.
    진행상황 메시지(⏳ 등)와 현재 질문(exclude_ts)은 제외. 실패하면 빈 문자열(주입 생략).
    """
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
    except Exception as e:  # noqa: BLE001
        logger.warning("thread history fetch failed: %s", e)
        return ""
    lines: list[str] = []
    for m in resp.get("messages", []):
        if m.get("ts") == exclude_ts or m.get("subtype"):
            continue
        text = _strip_mention(m.get("text", ""))
        if not text:
            continue
        is_bot = bool(m.get("bot_id"))
        if is_bot and text.startswith(_STATUS_PREFIXES):
            continue                     # 진행상황 메시지는 대화가 아님
        if len(text) > _CTX_MAX_MSG_CHARS:
            text = text[:_CTX_MAX_MSG_CHARS] + "…(생략)"
        lines.append(f"{'봇' if is_bot else '사용자'}: {text}")
    if not lines:
        return ""
    # 전체 상한 초과 시 오래된 것부터 버림 (최근 대화가 더 중요)
    out: list[str] = []
    total = 0
    for ln in reversed(lines):
        if total + len(ln) > _CTX_MAX_TOTAL_CHARS:
            break
        out.append(ln)
        total += len(ln)
    return "\n".join(reversed(out))


@app.event("app_mention")
def on_mention(event, client):
    question = _strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    channel = event["channel"]
    if not question:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="안녕하세요 — DB·인프라 관련해서 뭐든 물어보세요. "
                 "예: `@DBAOps 최근 1시간 Aurora CPU 어때?`")
        return
    _start_chat(client, channel, thread_ts, question, event_ts=event["ts"])


def _run_chat(client, channel: str, thread_ts: str, status_ts: str,
              question: str, event_ts: str) -> None:
    """백그라운드 스레드에서 agent(single) 호출 + Slack 업데이트 (3초 ack 제한 회피).

    Slack 스레드 히스토리를 항상 컨텍스트로 주입 — agent 의 InMemorySaver 는
    컨테이너 재시작 시 날아가므로, 스레드 텍스트가 유일하게 견고한 대화 기록이다.
    (세션이 살아있으면 중복이지만 무해 — 상한 4,000자.)
    """
    free_text = question
    ctx = _thread_context(client, channel, thread_ts, exclude_ts=event_ts)
    if ctx:
        free_text = (
            f"[이전 Slack 스레드 대화 — 참고용]\n{ctx}\n[이전 대화 끝]\n\n"
            f"새 질문: {question}"
        )
        logger.info("thread %s: injected %d chars of history", thread_ts, len(ctx))
    request = {
        "mode": "single",
        "free_text": free_text,
        "time_range": _window(),
        "session_id": _session_id(thread_ts),   # 스레드 = 세션 → agent 가 이전 맥락 기억
    }
    renderer = SlackThreadRenderer(
        client, channel, thread_ts, status_ts, streamlit_url=STREAMLIT_URL or None,
    )
    try:
        for ev in agentcore_client.invoke_stream(request):
            renderer.handle(ev)
    except Exception as e:  # noqa: BLE001
        logger.exception("chat failed")
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"❌ 실행 오류: {e!r}")
        except Exception:  # noqa: BLE001
            pass


def _start_chat(client, channel: str, thread_ts: str, question: str,
                event_ts: str) -> None:
    """진행상황 메시지 생성 + 백그라운드 대화 시작. 스레드를 활성으로 표시."""
    _ACTIVE_THREADS.add(thread_ts)
    status = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text="⏳ 확인 중…",
    )
    threading.Thread(
        target=_run_chat,
        args=(client, channel, thread_ts, status["ts"], question, event_ts),
        daemon=True,
    ).start()


@app.event("message")
def on_thread_message(event, client):
    """활성 스레드 안에서 멘션 없이 이어 말하면 같은 세션으로 대화를 계속한다.

    - 봇/시스템 메시지, 멘션 포함 메시지(app_mention 가 처리), 스레드 밖 메시지는 무시.
    - 멘션으로 시작한 적 없는 스레드는 무시(아무 채널 잡담에 끼어들지 않음).
    """
    if event.get("bot_id") or event.get("subtype"):
        return
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts not in _ACTIVE_THREADS:
        return
    text = event.get("text", "")
    if "<@" in text:                        # 멘션 포함 → app_mention 핸들러가 처리
        return
    question = text.strip()
    if not question:
        return
    _start_chat(client, event["channel"], thread_ts, question, event_ts=event["ts"])


def main() -> None:
    if not agentcore_client.AGENT_HTTP_URL and not agentcore_client.RUNTIME_ARN:
        logger.warning("AGENT_HTTP_URL/AGENTCORE_RUNTIME_ARN 둘 다 미설정 — 호출 실패할 것")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("DBAOps Slack bot starting (Socket Mode)…")
    handler.start()


if __name__ == "__main__":
    main()
