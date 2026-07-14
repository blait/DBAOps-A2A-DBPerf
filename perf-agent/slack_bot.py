"""
slack_bot.py - Perf 에이전트 전용 Slack 봇 (Socket Mode).

DBAOps의 slack_bot(@dbaagent)과 별개의 앱/프로세스로, perf 서버(:9100)의
**POST /invocations NDJSON 스트림**을 소비한다 — dbaops 봇과 동일한
SlackThreadRenderer 를 재사용해 진행상황 갱신·mrkdwn 변환·차트 PNG 첨부까지 동일 UX.

  Slack ──wss(Socket Mode)── [이 봇] ──NDJSON──▶ dbperf-a2a :9100 (/invocations)
                                              (A2A 는 peer 에이전트용, 같은 프로세스)

- 같은 스레드 = 같은 세션 (thread_ts → session_id → LangGraph thread_id)
- 스레드 안 후속 질문은 멘션 없이도 응답 (한번 멘션으로 시작한 스레드만)
- 봇 display_name은 Slack 앱 설정이 결정 — 코드는 이름과 무관 (user ID 멘션 처리)

env (dbaops와 분리):
  PERF_SLACK_BOT_TOKEN  xoxb-...   (perfagent 앱)
  PERF_SLACK_APP_TOKEN  xapp-...
  PERF_A2A_URL          http://127.0.0.1:9100
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# dbaops slack_bot 의 렌더러/차트 재사용 (mrkdwn 변환·표·차트 PNG 전부 검증된 코드)
_DBAOPS_SLACK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "dbaops", "slack_bot")
sys.path.insert(0, os.path.abspath(_DBAOPS_SLACK))
from render import SlackThreadRenderer  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("perf-slack")

PERF_A2A_URL = os.environ.get("PERF_A2A_URL", "http://127.0.0.1:9100")
STREAM_TIMEOUT = int(os.environ.get("A2A_CLIENT_TIMEOUT", "600"))
STREAMLIT_URL = os.environ.get("PERF_STREAMLIT_URL", "")

app = App(token=os.environ["PERF_SLACK_BOT_TOKEN"])

# 멘션으로 대화를 시작한 스레드 — 이후 멘션 없는 후속 질문에도 응답
_ACTIVE_THREADS: set[str] = set()


def _strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>", "", text or "").strip()


def _iter_stream(question: str, session_id: str):
    """perf 서버 /invocations NDJSON 스트림 → 이벤트 dict 순회."""
    payload = {"request": {"free_text": question, "session_id": session_id, "stream": True}}
    with httpx.stream("POST", f"{PERF_A2A_URL}/invocations", json=payload,
                      timeout=STREAM_TIMEOUT) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _run_chat(client, channel: str, thread_ts: str, status_ts: str, question: str) -> None:
    session_id = "slk-" + thread_ts.replace(".", "")
    renderer = SlackThreadRenderer(client, channel, thread_ts, status_ts,
                                   streamlit_url=STREAMLIT_URL or None)
    try:
        for ev in _iter_stream(question, session_id):
            renderer.handle(ev)
    except Exception as e:  # noqa: BLE001
        logger.exception("perf chat failed")
        try:
            client.chat_update(channel=channel, ts=status_ts, text=f"❌ 실행 오류: {e!r}")
        except Exception:  # noqa: BLE001
            pass


def _start_chat(client, channel: str, thread_ts: str, question: str) -> None:
    _ACTIVE_THREADS.add(thread_ts)
    status = client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text="⏳ 쿼리 성능 분석 시작…")
    threading.Thread(target=_run_chat,
                     args=(client, channel, thread_ts, status["ts"], question),
                     daemon=True).start()


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
    _start_chat(client, channel, thread_ts, question)


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
        _start_chat(client, ev["channel"], thread_ts, question)


def main():
    logger.info("Perf Slack bot starting (Socket Mode, streaming)… → %s", PERF_A2A_URL)
    SocketModeHandler(app, os.environ["PERF_SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
