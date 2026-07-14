"""표준 Markdown → Slack mrkdwn 변환 (perf slack bot용).

DBAOps slack_bot(dbaops/slack_bot/render.py)의 변환 로직과 동일 — 두 봇의
Slack 표시 방식을 통일한다. 원본 dbaops 패키지는 무수정 원칙이라 import 하지
않고 변환 함수만 가져왔다 (차트 렌더링 부분은 perf에 없어 제외).

Slack 은 표준 MD 를 렌더하지 않고 mrkdwn 을 쓴다:
  **굵게** → *굵게*,  ## 제목 → *제목*,  [a](b) → <b|a>,  표 → 코드블록 정렬.
"""

from __future__ import annotations

import re
import unicodedata

# 일반 코드펜스(```...```)는 보존해야 하므로 변환 시 잠시 빼둔다.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

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
