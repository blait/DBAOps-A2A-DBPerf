"""agent 이벤트 스트림 → Slack 스레드 메시지 변환.

agentcore_client.invoke_stream 이 yield 하는 이벤트(start/stage/message/validation/
report/done/error)를 받아 Slack 스레드를 실시간 업데이트한다.

- 텍스트 리포트: 표준 Markdown → Slack mrkdwn 변환(표는 코드블록/레코드).
- 차트: report 의 json-chart 스펙 + tool 결과 데이터 → matplotlib PNG → files_upload.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata

import charts as chart_renderer

logger = logging.getLogger(__name__)

# report markdown 의 ```json-chart {스펙} ``` — 스펙 본문 캡처
_CHART_SPEC = re.compile(r"```json-chart\s*\n(.*?)\n```", re.DOTALL)
# report markdown 의 ```json-chart ...``` 펜스 제거용(텍스트에서 차트블록 삭제)
_CHART_FENCE = re.compile(r"```json-chart\s*\n.*?\n```", re.DOTALL)
# 일반 코드펜스(```...```)는 보존해야 하므로 변환 시 잠시 빼둔다.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

_STAGE_LABEL = {
    "domain": "🔎 도메인 분석",
    "validation": "🧪 검증",
    "revise": "✍️ 보정",
    "report": "📝 리포트 생성",
}


def strip_charts(markdown: str) -> tuple[str, int]:
    """json-chart 펜스를 제거하고 (정리된 텍스트, 제거된 차트 수) 반환."""
    charts = _CHART_FENCE.findall(markdown or "")
    cleaned = _CHART_FENCE.sub("", markdown or "").strip()
    return cleaned, len(charts)


# ─────────────────── 표준 Markdown → Slack mrkdwn ───────────────────
# Slack 은 표준 MD 를 렌더하지 않고 mrkdwn 을 쓴다:
#   **굵게** → *굵게*,  ## 제목 → *제목*,  [a](b) → <b|a>,  표 → 코드블록 정렬.

# 코드블록 안에서 폭 계산을 깨뜨리는 이모지 → 1칸 ASCII 로 치환.
_CELL_EMOJI = {"✅": "Y", "❌": "N", "⚠️": "!", "🟢": "Y", "🔴": "N", "✔️": "Y", "✖️": "N"}
_MAX_CELL = 22    # 셀 최대 폭 (넘으면 …로 줄여 표가 화면을 안 넘게)
_MAX_TABLE_W = 72  # 표 전체 폭 상한 — 넘으면 레코드 리스트로 폴백(모바일/좁은 화면 대비)


def _dw(s: str) -> int:
    """문자열의 monospace 표시폭 (CJK/이모지 wide=2, 그 외 1)."""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(s: str, width: int) -> str:
    """표시폭 기준 좌측 정렬 패딩."""
    return s + " " * max(0, width - _dw(s))


def _truncate_dw(s: str, max_w: int) -> str:
    """표시폭 기준 truncate (… 포함)."""
    if _dw(s) <= max_w:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > max_w - 1:
            break
        out += ch
        w += cw
    return out + "…"


def _clean_cell(s: str) -> str:
    """셀 정제 — mrkdwn 잔재 제거 + 이모지 치환 (truncate 는 표 렌더 시점에)."""
    for e, a in _CELL_EMOJI.items():
        s = s.replace(e, a)
    s = s.replace("`", "").replace("**", "").strip()
    s = s.replace("️", "")  # VS16 등 변형 셀렉터 잔흔 제거
    return s


def _md_table_to_code(block: str) -> str:
    """| a | b | 형태의 MD 표를 monospace 코드블록으로 정렬 변환 (Slack 은 표 미지원).

    - 이모지는 폭 계산을 깨므로 Y/N/! 로 치환
    - 넓거나 컬럼 많은 표는 레코드 리스트로 폴백(정보 보존, 화면 안 넘침)
    - 코드블록 표는 셀 폭 상한(_MAX_CELL)으로 truncate
    """
    lines = [ln for ln in block.splitlines() if ln.strip()]
    rows = []
    for ln in lines:
        if re.match(r"^\s*\|?\s*[:\- ]+\|[:\-| ]*$", ln):  # 구분선(---|---) 건너뜀
            continue
        cells = [_clean_cell(c) for c in ln.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return block
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]

    # 폴백 판정은 truncate 전 원본 폭으로 (정보가 잘리는 표보다 레코드가 낫다)
    raw_widths = [max(_dw(r[i]) for r in rows) for i in range(ncol)]
    raw_total = sum(raw_widths) + 2 * (ncol - 1)
    if (raw_total > _MAX_TABLE_W or ncol >= 5) and len(rows) > 1:
        return _rows_to_records(rows)

    # 코드블록 표: 셀 truncate 후 폭 재계산
    trows = [[_truncate_dw(c, _MAX_CELL) for c in r] for r in rows]
    widths = [max(_dw(r[i]) for r in trows) for i in range(ncol)]
    out = []
    for ri, r in enumerate(trows):
        out.append("  ".join(_pad(c, widths[i]) for i, c in enumerate(r)).rstrip())
        if ri == 0:  # 헤더 밑줄
            out.append("  ".join("-" * widths[i] for i in range(ncol)))
    return "```\n" + "\n".join(out) + "\n```"


def _rows_to_records(rows: list[list[str]]) -> str:
    """넓은 표 → 행별 레코드 블록. 첫 컬럼을 제목, 나머지는 'key: val' 로.

    예) • database-1
          버전: 14.22 · 클래스: db.m5.xlarge · PI: N
    """
    header = rows[0]
    out = []
    for r in rows[1:]:
        title = r[0] if r else ""
        pairs = [f"{header[i]}: {r[i]}" for i in range(1, len(header)) if i < len(r) and r[i]]
        out.append(f"• *{title}*")
        if pairs:
            out.append("    " + " · ".join(pairs))
    return "\n".join(out)


def _convert_tables(text: str) -> str:
    """연속된 표 라인 블록을 찾아 코드블록으로 치환."""
    lines = text.splitlines()
    out, buf = [], []

    def flush():
        if buf:
            out.append(_md_table_to_code("\n".join(buf)))
            buf.clear()

    for ln in lines:
        if "|" in ln and ln.strip().startswith("|"):
            buf.append(ln)
        else:
            flush()
            out.append(ln)
    flush()
    return "\n".join(out)


def md_to_mrkdwn(text: str) -> str:
    """표준 Markdown 을 Slack mrkdwn 으로 변환. 코드펜스는 보존."""
    if not text:
        return ""
    # 1) 코드펜스 보호 (placeholder 로 치환)
    fences: list[str] = []

    def _stash(m):
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    text = _CODE_FENCE.sub(_stash, text)

    # 2) 표 변환 (코드블록 산출 → 다시 stash 안 해도 됨, 변환 후 그대로 둠)
    text = _convert_tables(text)

    # 3) 헤더 ##/### → *굵게* (한 줄)
    text = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", text, flags=re.MULTILINE)
    # 4) **굵게**/__굵게__ → *굵게*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    # 5) [텍스트](url) → <url|텍스트>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    # 6) 불릿 -, * → •
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)

    # 7) 코드펜스 복원
    def _restore(m):
        return fences[int(m.group(1))]

    text = re.sub(r"\x00FENCE(\d+)\x00", _restore, text)
    return text


def truncate(text: str, limit: int = 2900) -> str:
    """Slack 텍스트 블록 한도(3000자) 대비 안전 truncate."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(생략)"


def chunk_for_slack(text: str, limit: int = 2900) -> list[str]:
    """긴 본문을 Slack 메시지 한도(3000자) 이하 여러 조각으로 — 코드블록/문단 경계 우선."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        piece = (para + "\n\n")
        if len(cur) + len(piece) > limit:
            if cur:
                chunks.append(cur.rstrip())
                cur = ""
            # 단일 문단이 한도 초과면 강제 분할
            while len(piece) > limit:
                chunks.append(piece[:limit])
                piece = piece[limit:]
        cur += piece
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


class SlackThreadRenderer:
    """한 분석 요청에 대한 Slack 스레드 업데이트 핸들러.

    say/client 는 slack_bolt 가 주입. channel/thread_ts 로 진행 메시지를 갱신한다.
    """

    def __init__(self, client, channel: str, thread_ts: str, status_ts: str,
                 streamlit_url: str | None = None) -> None:
        self.client = client
        self.channel = channel
        self.thread_ts = thread_ts
        self.status_ts = status_ts          # 진행상황을 갱신할 메시지 ts
        self.streamlit_url = streamlit_url
        self._last_stage = ""
        self._tool_calls = 0
        self._reported = False          # report 이벤트로 본문을 이미 게시했는지
        self._last_ai_text = ""         # single 모드: 마지막 ai 본문(최종 답변 후보)
        self._tool_results: dict[str, object] = {}  # tool_call_id → parsed obj (차트 데이터)
        self._tcid_to_name: dict[str, str] = {}     # tool_call_id → tool name 매핑

    def _update_status(self, text: str) -> None:
        try:
            self.client.chat_update(channel=self.channel, ts=self.status_ts, text=text)
        except Exception:  # noqa: BLE001
            pass

    def _post(self, text: str) -> None:
        try:
            self.client.chat_postMessage(
                channel=self.channel, thread_ts=self.thread_ts, text=text,
            )
        except Exception:  # noqa: BLE001
            pass

    def _post_report(self, markdown: str) -> None:
        """markdown → mrkdwn 변환 후 길면 여러 메시지로 분할 게시."""
        body = md_to_mrkdwn(markdown)
        parts = chunk_for_slack(body)
        if not parts:
            self._post("_(빈 리포트)_")
            return
        for p in parts:
            self._post(p)

    def _emit_report(self, markdown: str, charts_meta: list[dict]) -> None:
        """리포트를 차트 위치 그대로 게시: [앞 텍스트] → [차트 PNG] → [뒤 텍스트].

        에이전트가 차트를 넣은 자리(```json-chart``` 블록)에 PNG 가 끼이도록,
        markdown 을 차트 블록 기준으로 분할해 텍스트와 차트를 순서대로 emit 한다.
        """
        segments = self._split_by_charts(markdown)
        total_specs = sum(1 for kind, _ in segments if kind == "chart")
        uploaded = 0
        for kind, payload in segments:
            if kind == "text":
                if payload.strip():
                    self._post_report(payload)
            else:  # chart spec
                png = self._render_spec(payload)
                if png:
                    self._upload_png(payload.get("title") or f"chart-{uploaded+1}", png)
                    uploaded += 1
        # 스펙이 markdown 에 없고 charts 이벤트 배열만 온 경우(드묾) 보강
        if total_specs == 0 and charts_meta:
            for spec in charts_meta:
                if not isinstance(spec, dict):
                    continue
                total_specs += 1
                png = self._render_spec(spec)
                if png:
                    self._upload_png(spec.get("title") or f"chart-{uploaded+1}", png)
                    uploaded += 1
        if total_specs > uploaded:
            miss = total_specs - uploaded
            if self.streamlit_url:
                self._post(f"📊 차트 {miss}개는 데이터 매칭 실패 — 전체는 {self.streamlit_url}")
            else:
                self._post(f"📊 차트 {miss}개는 Streamlit UI 에서 확인하세요.")

    def _split_by_charts(self, markdown: str) -> list[tuple[str, object]]:
        """markdown 을 [("text", str) | ("chart", spec_dict)] 시퀀스로 분할 (등장 순서 보존)."""
        out: list[tuple[str, object]] = []
        pos = 0
        for m in _CHART_SPEC.finditer(markdown or ""):
            before = (markdown or "")[pos:m.start()]
            if before.strip():
                out.append(("text", before))
            try:
                spec = json.loads(m.group(1).strip())
                out.append(("chart", spec))
            except json.JSONDecodeError:
                pass  # 깨진 스펙은 버림
            pos = m.end()
        rest = (markdown or "")[pos:]
        if rest.strip():
            out.append(("text", rest))
        return out

    def _render_spec(self, spec: dict) -> bytes | None:
        try:
            return chart_renderer.render_chart_png(spec, self._tool_results)
        except Exception as e:  # noqa: BLE001
            logger.warning("chart render error: %s", e)
            return None

    def _upload_png(self, title: str, png: bytes) -> None:
        try:
            self.client.files_upload_v2(
                channel=self.channel,
                thread_ts=self.thread_ts,
                filename=f"{title[:40]}.png".replace("/", "_"),
                title=title,
                content=png,
                initial_comment=f"📊 {title}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("files_upload failed: %s", e)

    def handle(self, ev: dict) -> None:
        etype = ev.get("type")

        if etype == "start":
            self._update_status("⏳ 확인 중…")

        elif etype == "stage":
            stage = ev.get("stage", "")
            label = _STAGE_LABEL.get(stage, stage)
            status = ev.get("status", "")
            self._update_status(f"{label} {'완료' if status == 'completed' else '진행 중'} "
                                f"(tool calls: {self._tool_calls})")

        elif etype == "message":
            msg = ev.get("message") or {}
            role = msg.get("role")
            if role == "tool":
                # 차트가 참조할 tool 결과를 tool_call_id 로 보관.
                tcid = msg.get("tool_call_id")
                txt = msg.get("text") or ""
                if tcid and txt:
                    try:
                        self._tool_results[tcid] = json.loads(txt)
                    except json.JSONDecodeError:
                        pass
            elif role == "ai":
                tcs = msg.get("tool_calls") or []
                for tc in tcs:
                    self._tool_calls += 1
                    tcid = tc.get("id")
                    if tcid:
                        self._tcid_to_name[tcid] = tc.get("name", "")
                text = (msg.get("text") or "").strip()
                if tcs:
                    # 도구 호출 직전 예고 문장(preamble)이 있으면 그걸 진행상황으로 보여준다
                    # — Claude Code 식 "뭘 확인할지 한 문장". 없으면 도구명을 표시.
                    if text:
                        self._update_status(f"💬 {text[:280]}")
                    else:
                        names = ", ".join(tc.get("name", "?") for tc in tcs)
                        self._update_status(f"🔧 `{names}` 확인 중…")
                elif text:
                    # tool_call 없는 ai 메시지 = 자연어 답변 → single 모드 최종답변 후보
                    self._last_ai_text = msg["text"]

        elif etype == "validation":
            passed = ev.get("passed")
            issues = ev.get("issues") or []
            if passed:
                self._update_status("✅ 검증 통과")
            else:
                kinds = ", ".join(i.get("kind", "?") for i in issues)
                self._update_status(f"⚠️ 검증 이슈: {kinds} — 보정 중")

        elif etype == "report":
            self._emit_report(ev.get("markdown", ""), ev.get("charts") or [])
            self._reported = True

        elif etype == "done":
            # single 모드는 report 이벤트가 없으므로 마지막 ai 본문을 최종 답변으로 게시.
            # single 답변에도 json-chart 블록이 포함될 수 있어 동일하게 차트 처리.
            if not self._reported and self._last_ai_text.strip():
                self._emit_report(self._last_ai_text, [])
                self._reported = True
            # 진행상황 메시지를 결과에 어울리게 마무리(도구 안 썼으면 카운트 노출 안 함).
            if self._tool_calls:
                self._update_status(f"✅ 완료 · 도구 {self._tool_calls}회 사용")
            else:
                self._update_status("✅ 완료")

        elif etype == "abort":
            self._post(f"⏹️ 중단: {ev.get('reason', 'unknown')}")

        elif etype == "error":
            self._post(f"❌ 오류: {ev.get('error', 'unknown')}")
