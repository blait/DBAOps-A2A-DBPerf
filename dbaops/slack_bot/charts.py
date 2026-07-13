"""json-chart 스펙 + tool 결과 데이터 → matplotlib PNG (Slack 첨부용).

Streamlit(view_swarm.py)의 검증된 차트 로직을 matplotlib 로 포팅한다.
- 데이터 추출(_resolve_path / _extract_timeseries_from_obj / _parse_ts / _to_float)은 동일
- st.*_chart 대신 matplotlib 으로 PNG bytes 생성 → Slack files_upload_v2

지원 chart_type: line, area, bar, scatter, histogram, table.
각 함수는 PNG bytes 또는 None(데이터 없음) 반환.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# matplotlib 는 headless(Agg) 백엔드로 — 서버에 디스플레이 없음.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
from matplotlib import font_manager  # noqa: E402


def _setup_korean_font() -> None:
    """차트 한글(제목/라벨)이 □ 로 깨지지 않게 설치된 한글 폰트를 등록.

    컨테이너에 fonts-nanum 등이 있으면 사용. 없으면 경고만(영문은 정상).
    """
    candidates = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        # Amazon Linux 2023 (google-noto-sans-cjk-ttc-fonts) — vanilla EC2 배포
        "/usr/share/fonts/google-noto-sans-cjk-fonts/NotoSansCJK-Regular.ttc",
    ]
    import glob as _glob
    candidates += _glob.glob("/usr/share/fonts/**/NotoSansCJK*.ttc", recursive=True)
    for path in candidates:
        try:
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            logger.info("chart font: %s", name)
            return
        except Exception:  # noqa: BLE001
            continue
    plt.rcParams["axes.unicode_minus"] = False
    logger.warning("한글 폰트 미발견 — 차트 한글이 깨질 수 있음(영문은 정상)")


_setup_korean_font()


# ───────────────────────── 값/시간 파싱 (view_swarm.py 와 동일) ─────────────────────────

def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        try:
            ts = float(v)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.utcfromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            try:
                return datetime.utcfromtimestamp(float(s))
            except ValueError:
                return None
    return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_timeseries_from_obj(obj: Any) -> dict[str, list[tuple[Any, Any]]]:
    """tool result 한 건에서 시계열 series_dict 추출 (PoC/cloudwatch/prometheus)."""
    if not isinstance(obj, dict):
        return {}
    out: dict[str, list[tuple[Any, Any]]] = {}

    series = obj.get("series")
    if isinstance(series, list) and series and isinstance(series[0], dict) and "ts" in series[0]:
        label = obj.get("metric_name") or obj.get("metric") or obj.get("label") or "series"
        out[str(label)] = [(p.get("ts"), p.get("value")) for p in series]
        return out

    mdr = obj.get("metricDataResults") or obj.get("metric_data_results")
    if isinstance(mdr, list) and mdr and isinstance(mdr[0], dict):
        for m in mdr:
            label = m.get("label") or m.get("Label") or m.get("id") or m.get("Id") or "metric"
            # awslabs cloudwatch-mcp 형식: datapoints=[{timestamp,value}]
            dps = m.get("datapoints") or m.get("Datapoints")
            if isinstance(dps, list) and dps:
                pts = [(d.get("timestamp") or d.get("Timestamp"),
                        d.get("value") if d.get("value") is not None else d.get("Value"))
                       for d in dps if isinstance(d, dict)]
                if pts:
                    out[str(label)] = pts
                continue
            # boto3 GetMetricData 형식: timestamps[]/values[]
            ts_list = m.get("timestamps") or m.get("Timestamps") or []
            val_list = m.get("values") or m.get("Values") or []
            if ts_list:
                out[str(label)] = list(zip(ts_list, val_list))
        if out:
            return out

    promql_result = None
    if isinstance(obj.get("data"), dict):
        promql_result = obj["data"].get("result")
    elif isinstance(obj.get("result"), list):
        promql_result = obj["result"]
    if isinstance(promql_result, list):
        for item in promql_result:
            if not isinstance(item, dict):
                continue
            metric = item.get("metric") or {}
            base = metric.get("__name__") or ""
            extras = ",".join(f"{k}={v}" for k, v in metric.items() if k != "__name__")
            label = f"{base}{{{extras}}}" if extras else (base or "value")
            vals = item.get("values")
            if isinstance(vals, list):
                out[label] = [(p[0], p[1]) for p in vals if isinstance(p, (list, tuple)) and len(p) >= 2]
        if out:
            return out
    return out


# ───────────────────────── dotted-path resolver (view_swarm.py 와 동일) ─────────────────────────

def _resolve_path(obj: Any, path: str) -> Any:
    if not path or obj is None:
        return obj
    cur: Any = obj
    tokens = []
    buf = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == ".":
            if buf:
                tokens.append(("key", buf)); buf = ""
            i += 1
        elif c == "[":
            if buf:
                tokens.append(("key", buf)); buf = ""
            j = path.find("]", i)
            if j < 0:
                return None
            tokens.append(("idx", path[i + 1:j]))
            i = j + 1
        else:
            buf += c
            i += 1
    if buf:
        tokens.append(("key", buf))

    for kind, val in tokens:
        if cur is None:
            return None
        if kind == "key":
            cur = cur.get(val) if isinstance(cur, dict) else None
            if cur is None and not isinstance(obj, dict):
                return None
        elif kind == "idx":
            if val == "*":
                if not isinstance(cur, list):
                    return None
                rest_idx = tokens.index((kind, val)) + 1
                rest_path = _tokens_to_path(tokens[rest_idx:])
                return [_resolve_path(item, rest_path) for item in cur]
            try:
                n = int(val)
            except ValueError:
                return None
            if not isinstance(cur, list) or n >= len(cur) or n < -len(cur):
                return None
            cur = cur[n]
    return cur


def _tokens_to_path(tokens: list) -> str:
    out = ""
    for kind, val in tokens:
        if kind == "key":
            out += ("." if out else "") + val
        elif kind == "idx":
            out += f"[{val}]"
    return out


# ───────────────────────── matplotlib 렌더 ─────────────────────────

_W, _H, _DPI = 9, 4.2, 130  # 가로형 — Slack 에서 보기 좋게


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _chart_line_or_area(spec: dict, obj: Any, area: bool) -> bytes | None:
    metric_filter = spec.get("metric_filter") or []
    sd = _extract_timeseries_from_obj(obj)
    if metric_filter and sd:
        sd = {k: v for k, v in sd.items()
              if any(f.lower() in k.lower() for f in metric_filter)} or sd
    if not sd:
        return None
    fig, ax = plt.subplots(figsize=(_W, _H))
    plotted = False
    for label, pts in sd.items():
        xs, ys = [], []
        for ts, v in pts:
            t = _parse_ts(ts); f = _to_float(v)
            if t is None or f is None:
                continue
            xs.append(t); ys.append(f)
        if not xs:
            continue
        pairs = sorted(zip(xs, ys), key=lambda p: p[0])
        xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
        if area:
            ax.fill_between(xs, ys, alpha=0.3, label=label[:40])
            ax.plot(xs, ys, linewidth=1.2)
        else:
            ax.plot(xs, ys, linewidth=1.5, label=label[:40], marker=".", markersize=3)
        plotted = True
    if not plotted:
        plt.close(fig)
        return None
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend(fontsize=8, loc="best")
    ax.grid(True, alpha=0.3)
    ax.set_title(spec.get("title") or "", fontsize=11)
    fig.autofmt_xdate()
    return _fig_to_png(fig)


def _chart_bar(spec: dict, obj: Any) -> bytes | None:
    xf, yf = spec.get("x_field"), spec.get("y_field")
    if not xf or not yf:
        return None
    xs, ys = _resolve_path(obj, xf), _resolve_path(obj, yf)
    if not isinstance(xs, list) or not isinstance(ys, list):
        return None
    pairs = []
    for x, y in zip(xs, ys):
        # y 가 리스트(예: 인스턴스별 datapoints[*].value)면 평균내 단일 막대값으로.
        if isinstance(y, list):
            nums = [_to_float(v) for v in y]
            nums = [n for n in nums if n is not None]
            f = sum(nums) / len(nums) if nums else None
        else:
            f = _to_float(y)
        if f is None or x is None:
            continue
        pairs.append((str(x)[:40], f))
    if not pairs:
        return None
    pairs.sort(key=lambda r: r[1], reverse=True)
    top_n = spec.get("top_n")
    if isinstance(top_n, int) and top_n > 0:
        pairs = pairs[:top_n]
    labels = [p[0] for p in pairs]; vals = [p[1] for p in pairs]
    fig, ax = plt.subplots(figsize=(_W, max(_H, 0.4 * len(pairs) + 1)))
    ax.barh(range(len(labels)), vals, color="#4c78a8")
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.grid(True, axis="x", alpha=0.3)
    ax.set_title(spec.get("title") or "", fontsize=11)
    return _fig_to_png(fig)


def _chart_scatter(spec: dict, obj: Any) -> bytes | None:
    xf, yf = spec.get("x_field"), spec.get("y_field")
    if not xf or not yf:
        return None
    xs, ys = _resolve_path(obj, xf), _resolve_path(obj, yf)
    if not isinstance(xs, list) or not isinstance(ys, list):
        return None
    px, py = [], []
    for x, y in zip(xs, ys):
        fx, fy = _to_float(x), _to_float(y)
        if fx is None or fy is None:
            continue
        px.append(fx); py.append(fy)
    if not px:
        return None
    fig, ax = plt.subplots(figsize=(_W, _H))
    ax.scatter(px, py, alpha=0.6, color="#4c78a8")
    ax.grid(True, alpha=0.3)
    ax.set_xlabel(xf, fontsize=8); ax.set_ylabel(yf, fontsize=8)
    ax.set_title(spec.get("title") or "", fontsize=11)
    return _fig_to_png(fig)


def _chart_histogram(spec: dict, obj: Any) -> bytes | None:
    field = spec.get("field")
    bins = int(spec.get("bins") or 20)
    if not field:
        return None
    raw = _resolve_path(obj, field)
    if not isinstance(raw, list):
        return None
    nums = []
    for v in raw:
        f = _to_float(v) if not isinstance(v, dict) else None
        if f is None and isinstance(v, dict):
            for vv in v.values():
                f = _to_float(vv)
                if f is not None:
                    break
        if f is not None:
            nums.append(f)
    if not nums:
        return None
    fig, ax = plt.subplots(figsize=(_W, _H))
    ax.hist(nums, bins=bins, color="#4c78a8", edgecolor="white")
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_title(spec.get("title") or "", fontsize=11)
    return _fig_to_png(fig)


def _chart_table(spec: dict, obj: Any) -> bytes | None:
    # 표는 Slack 에서 텍스트(코드블록)로 이미 잘 나오므로 PNG 로는 안 만든다.
    return None


_RENDERERS = {
    "line": lambda s, o: _chart_line_or_area(s, o, area=False),
    "area": lambda s, o: _chart_line_or_area(s, o, area=True),
    "bar": _chart_bar,
    "scatter": _chart_scatter,
    "histogram": _chart_histogram,
    "table": _chart_table,
}


def _spec_fits(spec: dict, chart_type: str, obj: Any) -> bool:
    """이 obj 가 spec 의 chart_type 으로 그릴 데이터를 갖고 있는지 (폴백 매칭용)."""
    try:
        if chart_type in ("line", "area"):
            return bool(_extract_timeseries_from_obj(obj))
        if chart_type in ("bar", "scatter"):
            xs = _resolve_path(obj, spec.get("x_field") or "")
            ys = _resolve_path(obj, spec.get("y_field") or "")
            return isinstance(xs, list) and isinstance(ys, list) and bool(xs) and bool(ys)
        if chart_type == "histogram":
            return isinstance(_resolve_path(obj, spec.get("field") or ""), list)
    except Exception:  # noqa: BLE001
        return False
    return False


def _metric_filter_matches(spec: dict, obj: Any) -> bool:
    """spec 의 metric_filter/title 키워드가 obj 의 metric 이름에 포함되는지."""
    keywords = list(spec.get("metric_filter") or [])
    title = spec.get("title") or ""
    if not keywords and title:
        keywords = [w for w in re.split(r"[\s/·\-_()（）]", title)
                    if len(w) > 2 and w.isascii()]
    if not keywords:
        return True
    sd = _extract_timeseries_from_obj(obj) if isinstance(obj, dict) else {}
    all_labels = " ".join(sd.keys()).lower() if sd else ""
    return any(k.lower() in all_labels for k in keywords)


def render_chart_png(spec: dict, tool_results: dict[str, Any]) -> bytes | None:
    """chart spec → PNG bytes. 데이터 없거나 table 이면 None.

    tool_results: {tool_call_id: parsed_obj}

    source_tool_call_id 정확 매칭을 우선하되, single 모드처럼 에이전트가 id 를
    지어내 매칭 실패하면 chart_type 에 맞는 데이터를 가진 tool 결과로 폴백한다.
    폴백 시 metric_filter/title 키워드 매칭 + 최근(마지막) 결과를 우선한다.
    """
    chart_type = (spec.get("chart_type") or "line").lower()
    renderer = _RENDERERS.get(chart_type)
    if renderer is None:
        return None

    obj = tool_results.get(spec.get("source_tool_call_id"))
    candidates: list[Any] = []
    if obj is not None and _spec_fits(spec, chart_type, obj):
        candidates.append(obj)

    # 폴백: 최근 결과 우선(reversed) + metric_filter 매칭을 상위로
    fallback_matched: list[Any] = []
    fallback_unmatched: list[Any] = []
    for o in reversed(list(tool_results.values())):
        if o is obj or not _spec_fits(spec, chart_type, o):
            continue
        if _metric_filter_matches(spec, o):
            fallback_matched.append(o)
        else:
            fallback_unmatched.append(o)
    candidates.extend(fallback_matched)
    candidates.extend(fallback_unmatched)

    if obj is not None and obj not in candidates:
        candidates.append(obj)

    for cand in candidates:
        if cand is None:
            continue
        try:
            png = renderer(spec, cand)
        except Exception as e:  # noqa: BLE001
            logger.warning("chart render failed (%s): %s", chart_type, e)
            png = None
        if png:
            return png
    return None
