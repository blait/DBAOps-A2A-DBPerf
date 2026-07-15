"""Swarm 모드 뷰 — 카드형 메시지 + tool_call/tool_result 매칭 + streaming 실시간 갱신.

시계열 도구 결과(`series` / Prometheus `result.values` / awslabs `metricDataResults`) 는
표뿐 아니라 라인 차트로도 함께 렌더해 시각적 가시성을 확보한다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterator

import pandas as pd
import streamlit as st


_AGENT_AVATAR = {
    "supervisor":       "🎯",
    "os_specialist":    "🖥️",
    "db_specialist":    "🗄️",
    "log_specialist":   "📜",
    "query_specialist": "🔎",
    "aws_specialist":   "☁️",
    "docs_specialist":  "📚",
    "single_agent":     "🧠",
    "os_metric_agent":  "🖥️",
    "db_metric_agent":  "🗄️",
    "log_agent":        "📜",
    "validation_agent": "🧐",
    "report_agent":     "📝",
}

_ROLE_AVATAR = {
    "human":  "🙋",
    "user":   "🙋",
    "ai":     "🤖",
    "tool":   "🛠️",
    "system": "ℹ️",
}


def _agent_chip(name: str | None) -> str:
    if not name:
        return "🤖 _(unnamed)_"
    icon = _AGENT_AVATAR.get(name, "🤖")
    return f"{icon} `{name}`"


def _is_handoff_tool(name: str | None) -> bool:
    return bool(name) and (name.startswith("transfer_to_") or name.startswith("handoff_to_"))


def _short_args(args: Any, limit: int = 200) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "…"


def _scalar(v: Any, limit: int = 200) -> str:
    """list/dict 도 한 셀에 담을 수 있게 압축 표현."""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        s = str(v)
    else:
        try:
            s = json.dumps(v, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            s = str(v)
    return s if len(s) <= limit else s[:limit] + "…"


def _render_kv_table(target, kv: dict[str, Any]) -> None:
    """단순 dict 를 key/value 2열 dataframe 으로."""
    if not kv:
        target.caption("_(arguments 없음)_")
        return
    rows = [{"key": k, "value": _scalar(v, limit=400)} for k, v in kv.items()]
    target.dataframe(rows, use_container_width=True, hide_index=True)


def _parse_ts(v: Any) -> datetime | None:
    """ISO8601 / unix epoch / datetime 모두 datetime 으로."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        # epoch seconds vs millis
        try:
            ts = float(v)
            if ts > 1e12:  # millis
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


def _render_timeseries_chart(target, points: list[tuple[Any, Any]], *,
                             label: str | None = None,
                             multi_label: str | None = None) -> bool:
    """[(ts, value), ...] 리스트를 line chart 로. 표는 별도. value 가 숫자로 cast 가능한 점만."""
    rows = []
    for ts, v in points:
        t = _parse_ts(ts)
        f = _to_float(v)
        if t is None or f is None:
            continue
        rows.append({"ts": t, "value": f, **({"series": multi_label} if multi_label else {})})
    if not rows:
        return False
    df = pd.DataFrame(rows)
    if multi_label:
        # 복수 시리즈 — pivot
        return False  # multi 는 호출부에서 따로 처리
    df = df.set_index("ts")
    if label:
        df = df.rename(columns={"value": label})
    target.line_chart(df, height=240)
    return True


def _render_multi_timeseries_chart(target, series_dict: dict[str, list[tuple[Any, Any]]]) -> bool:
    """{label: [(ts, value), ...]} 를 한 차트에 여러 line 으로."""
    frames = []
    for label, pts in series_dict.items():
        for ts, v in pts:
            t = _parse_ts(ts)
            f = _to_float(v)
            if t is None or f is None:
                continue
            frames.append({"ts": t, "label": label, "value": f})
    if not frames:
        return False
    df = pd.DataFrame(frames)
    pivot = df.pivot_table(index="ts", columns="label", values="value", aggfunc="mean").sort_index()
    target.line_chart(pivot, height=260)
    return True


def _extract_timeseries_from_obj(obj: Any) -> dict[str, list[tuple[Any, Any]]]:
    """tool result 한 건에서 시계열 series_dict 추출. 비-시계열은 빈 dict."""
    if not isinstance(obj, dict):
        return {}
    out: dict[str, list[tuple[Any, Any]]] = {}

    # 1) 우리 PoC: {series:[{ts,value}], n_points}
    series = obj.get("series")
    if isinstance(series, list) and series and isinstance(series[0], dict) and "ts" in series[0]:
        label = obj.get("metric_name") or obj.get("metric") or obj.get("label") or "series"
        out[str(label)] = [(p.get("ts"), p.get("value")) for p in series]
        return out

    # 2) awslabs cloudwatch get_metric_data
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

    # 3) Prometheus range_query
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


def _gather_timeseries(messages: list[dict]) -> list[dict]:
    """tool result 메시지들을 훑어 시계열 묶음 list 반환.
    각 묶음 = {tool_name, series_dict, tool_call_id}.
    """
    out: list[dict] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        text = m.get("text") or ""
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        sd = _extract_timeseries_from_obj(obj)
        if not sd:
            continue
        out.append({
            "tool_name":    m.get("name") or "?",
            "tool_call_id": m.get("tool_call_id") or "",
            "series":       sd,
        })
    return out


def _render_supervisor_charts(target, messages: list[dict]) -> None:
    """최종 보고 카드에 시계열 차트 섹션 — message history 에서 추출."""
    bundles = _gather_timeseries(messages)
    if not bundles:
        return
    target.markdown("#### 📈 분석에 사용된 시계열")
    for i, b in enumerate(bundles, start=1):
        with target.container(border=True):
            target.caption(f"[{i}] tool=`{b['tool_name']}` · series={len(b['series'])}")
            ok = _render_multi_timeseries_chart(target, b["series"])
            if not ok:
                target.caption("(차트로 그릴 수치가 없음)")


def _flatten_for_table(items: list[Any]) -> list[dict] | None:
    """list 안의 원소들을 보고 dict 리스트로 변환 (가능하면)."""
    if not items:
        return None
    if all(isinstance(x, dict) for x in items):
        out: list[dict] = []
        for x in items:
            out.append({k: _scalar(v, limit=200) for k, v in x.items()})
        return out
    # rows 형태: list[list] + columns 별도
    return None


def _render_result_payload(target, obj: Any) -> bool:
    """tool result JSON object 를 적절히 표 형태로 렌더. 표가 됐으면 True."""
    if not isinstance(obj, dict):
        return False

    # 1) sql_readonly: {row_count, columns: [...], rows: [[...]]}
    cols = obj.get("columns")
    rows = obj.get("rows")
    if isinstance(cols, list) and isinstance(rows, list) and rows and isinstance(rows[0], (list, tuple)):
        data = [
            {c: _scalar(v, limit=300) for c, v in zip(cols, r)}
            for r in rows[:200]
        ]
        meta = []
        if obj.get("row_count") is not None:
            meta.append(f"row_count={obj['row_count']}")
        if len(rows) > 200:
            meta.append(f"표시 {len(data)} / {len(rows)}행")
        if meta:
            target.caption(" · ".join(meta))
        target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 2) explain plan: {plan: "..."}
    if isinstance(obj.get("plan"), str):
        target.code(obj["plan"], language="text", wrap_lines=False)
        if obj.get("row_count") is not None:
            target.caption(f"row_count={obj['row_count']}")
        return True

    # 3) timeseries: {n_points, series: [{ts, value}, ...]}  ← 우리 PoC cloudwatch_metric / msk_metric / rds_pi
    series = obj.get("series")
    if isinstance(series, list) and series and isinstance(series[0], dict) and "ts" in series[0]:
        data = [
            {"ts": _scalar(p.get("ts"), 30), "value": _scalar(p.get("value"))}
            for p in series[:300]
        ]
        meta = []
        if obj.get("n_points") is not None:
            meta.append(f"n_points={obj['n_points']}")
        if len(series) > 300:
            meta.append(f"표시 {len(data)} / {len(series)}점")
        if meta:
            target.caption(" · ".join(meta))
        target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 3-a) awslabs cloudwatch get_metric_data — {metricDataResults: [{id, label, timestamps, values}]}
    mdr = obj.get("metricDataResults") or obj.get("metric_data_results")
    if isinstance(mdr, list) and mdr and isinstance(mdr[0], dict) and \
       ("timestamps" in mdr[0] or "Timestamps" in mdr[0] or "datapoints" in mdr[0] or "Datapoints" in mdr[0]):
        for m in mdr:
            label = m.get("label") or m.get("Label") or m.get("id") or m.get("Id") or "metric"
            dps = m.get("datapoints") or m.get("Datapoints")
            if isinstance(dps, list) and dps:
                pairs = [(d.get("timestamp") or d.get("Timestamp"),
                          d.get("value") if d.get("value") is not None else d.get("Value"))
                         for d in dps if isinstance(d, dict)]
            else:
                ts_list = m.get("timestamps") or m.get("Timestamps") or []
                val_list = m.get("values") or m.get("Values") or []
                pairs = list(zip(ts_list, val_list))
            target.markdown(f"**{label}** · {len(pairs)} pts")
            target.dataframe(
                [{"ts": _scalar(t, 30), "value": _scalar(v)} for t, v in pairs][:300],
                use_container_width=True,
                hide_index=True,
            )
        return True

    # 3-b) Prometheus range_query — {result: [{metric: {...}, values: [[ts, "v"], ...]}]}
    promql_result = None
    if isinstance(obj.get("data"), dict):
        promql_result = obj["data"].get("result")
    elif isinstance(obj.get("result"), list):
        promql_result = obj["result"]
    if isinstance(promql_result, list) and promql_result and isinstance(promql_result[0], dict) and \
       (isinstance(promql_result[0].get("values"), list) or isinstance(promql_result[0].get("value"), list)):
        for item in promql_result:
            metric = item.get("metric") or {}
            label = metric.get("__name__") or ""
            extras = ",".join(f"{k}={v}" for k, v in metric.items() if k != "__name__")
            full_label = f"{label}{{{extras}}}" if extras else (label or "value")
            vals = item.get("values")
            if isinstance(vals, list):
                pts = [(p[0], p[1]) for p in vals if isinstance(p, (list, tuple)) and len(p) >= 2]
            elif isinstance(item.get("value"), (list, tuple)) and len(item["value"]) >= 2:
                pts = [(item["value"][0], item["value"][1])]
            else:
                pts = []
            target.markdown(f"**{full_label}** · {len(pts)} pts")
            if pts:
                target.dataframe(
                    [{"ts": _scalar(t, 30), "value": _scalar(v)} for t, v in pts[:300]],
                    use_container_width=True,
                    hide_index=True,
                )
        return True

    # 4) S3 log fetch: {line_count, lines: [...]}
    lines = obj.get("lines")
    if isinstance(lines, list) and lines and isinstance(lines[0], (str, dict)):
        if isinstance(lines[0], str):
            target.code("\n".join(str(x) for x in lines[:200]), language="text", wrap_lines=False)
            target.caption(f"line_count={obj.get('line_count', len(lines))}"
                           + (" · 잘림" if obj.get("truncated") else ""))
        else:
            data = [{k: _scalar(v, 200) for k, v in (x or {}).items()} for x in lines[:200]]
            target.dataframe(data, use_container_width=True, hide_index=True)
        return True

    # 5) aws-api: 단일 list 키를 가진 dict (db_instances/db_clusters/log_files/alarms/instances/clusters/keys/top_sql/metrics/namespaces)
    list_keys = [k for k, v in obj.items() if isinstance(v, list) and v and isinstance(v[0], (dict, str, int, float))]
    if len(list_keys) == 1:
        key = list_keys[0]
        items = obj[key]
        flat = _flatten_for_table(items)
        if flat:
            count_label = obj.get("count")
            target.caption(f"`{key}`" + (f" · count={count_label}" if count_label is not None else f" · {len(items)}건"))
            target.dataframe(flat, use_container_width=True, hide_index=True)
            return True
        if all(isinstance(x, str) for x in items):
            target.caption(f"`{key}` · {len(items)}건")
            target.dataframe([{key: x} for x in items[:200]], use_container_width=True, hide_index=True)
            return True

    # 6) 다중 list 키 — namespaces + metrics 같이 오는 케이스
    if list_keys and all(isinstance(obj[k], list) for k in list_keys):
        rendered_any = False
        for key in list_keys:
            items = obj[key]
            flat = _flatten_for_table(items)
            if flat:
                target.caption(f"`{key}` · {len(items)}건")
                target.dataframe(flat, use_container_width=True, hide_index=True)
                rendered_any = True
            elif items and all(isinstance(x, str) for x in items):
                target.caption(f"`{key}` · {len(items)}건")
                target.dataframe([{key: x} for x in items[:200]], use_container_width=True, hide_index=True)
                rendered_any = True
        # 스칼라 메타 (count, _truncated, etc) 같이 표시
        scalars = {k: v for k, v in obj.items() if not isinstance(v, list) and not isinstance(v, dict)}
        if scalars:
            _render_kv_table(target, scalars)
        return rendered_any

    return False


_CHART_FENCE_RE = __import__("re").compile(r"```json-chart\s*\n([\s\S]*?)\n```", __import__("re").MULTILINE)


def _render_report(markdown: str, charts: list[dict], messages: list[dict]) -> None:
    """report 에이전트 markdown 을 렌더 — fenced ```json-chart 블록은 차트로 치환.

    chart spec: {"title": str, "source_tool_call_id": str, "metric_filter": [str, ...]?}
    매칭은 source_tool_call_id 우선. 못 찾으면 metric_filter 로 label substring fallback.
    """
    if not markdown:
        return

    # markdown 을 차트 fence 단위로 split — 각 fence 의 블록을 차트로 대체
    rendered_any_chart = False
    pos = 0
    for m in _CHART_FENCE_RE.finditer(markdown):
        # fence 앞부분 markdown
        before = markdown[pos:m.start()].rstrip()
        if before:
            st.markdown(before)
        # fence body parse
        try:
            spec = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            st.warning(f"차트 spec parse 실패: {m.group(1).strip()[:120]}")
            pos = m.end()
            continue
        _render_one_chart(spec, messages)
        rendered_any_chart = True
        pos = m.end()
    # 마지막 fence 뒤 잔여 markdown
    rest = markdown[pos:].strip()
    if rest:
        st.markdown(rest)

    # 차트가 spec 으로 1개도 안 그려졌고 시계열 tool result 가 history 에 있으면 fallback 차트
    if not rendered_any_chart and not charts:
        bundles = _gather_timeseries(messages)
        if bundles:
            st.caption("ℹ️ 리포트에 명시된 차트가 없어 message history 의 시계열을 자동 표시합니다.")
            _render_supervisor_charts(st, messages)


def _resolve_path(obj: Any, path: str) -> Any:
    """Dotted-path resolver — 'top_sql[*].aas' / 'metricDataResults[0].values' / 'series'.

    [*] = 리스트의 모든 요소에 대해 같은 path 적용 (returns list).
    [N] = N 번째 요소.
    """
    if not path or obj is None:
        return obj
    cur: Any = obj
    # tokenize: split on . and [...]
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
            inside = path[i+1:j]
            tokens.append(("idx", inside))
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
            if isinstance(cur, dict):
                cur = cur.get(val)
            else:
                return None
        elif kind == "idx":
            if val == "*":
                if not isinstance(cur, list):
                    return None
                # 남은 토큰을 각 요소에 재귀 적용
                rest_idx = tokens.index((kind, val)) + 1
                rest_tokens = tokens[rest_idx:]
                rest_path = _tokens_to_path(rest_tokens)
                return [_resolve_path(item, rest_path) for item in cur]
            else:
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
            if out:
                out += "."
            out += val
        elif kind == "idx":
            out += f"[{val}]"
    return out


def _find_tool_result(messages: list[dict], tool_call_id: str | None) -> dict | None:
    if not tool_call_id:
        return None
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id:
            try:
                return json.loads(m.get("text") or "{}")
            except Exception:  # noqa: BLE001
                return None
    return None


def _render_one_chart(spec: dict, messages: list[dict]) -> None:
    """chart spec 한 건을 chart_type 에 맞춰 렌더."""
    chart_type = (spec.get("chart_type") or "line").lower()
    title = spec.get("title") or "차트"
    target_id = spec.get("source_tool_call_id")
    obj = _find_tool_result(messages, target_id)

    with st.container(border=True):
        st.markdown(f"**📊 {title}**  ·  type=`{chart_type}`")
        if obj is None:
            st.caption(f"(매칭되는 도구 결과 없음 — source_tool_call_id={target_id or '없음'})")
            return

        try:
            if chart_type in ("line", "area"):
                _chart_line_or_area(spec, obj, area=(chart_type == "area"))
            elif chart_type == "bar":
                _chart_bar(spec, obj)
            elif chart_type == "scatter":
                _chart_scatter(spec, obj)
            elif chart_type == "histogram":
                _chart_histogram(spec, obj)
            elif chart_type == "table":
                _chart_table(spec, obj)
            else:
                st.warning(f"unsupported chart_type: {chart_type}")
        except Exception as e:  # noqa: BLE001
            st.warning(f"차트 렌더 실패: {e}")


def _chart_line_or_area(spec: dict, obj: Any, *, area: bool) -> None:
    metric_filter = spec.get("metric_filter") or []
    series_dict = _extract_timeseries_from_obj(obj)
    if metric_filter and series_dict:
        series_dict = {
            k: v for k, v in series_dict.items()
            if any(f.lower() in k.lower() for f in metric_filter)
        } or series_dict
    if not series_dict:
        st.caption("(시계열 데이터를 추출하지 못했습니다)")
        return
    frames = []
    for label, pts in series_dict.items():
        for ts, v in pts:
            t = _parse_ts(ts)
            f = _to_float(v)
            if t is None or f is None:
                continue
            frames.append({"ts": t, "label": label, "value": f})
    if not frames:
        st.caption("(시계열 값이 비어있거나 숫자로 변환 불가)")
        return
    df = pd.DataFrame(frames)
    pivot = df.pivot_table(index="ts", columns="label", values="value", aggfunc="mean").sort_index()
    if area:
        st.area_chart(pivot, height=260)
    else:
        st.line_chart(pivot, height=260)


def _chart_bar(spec: dict, obj: Any) -> None:
    x_field = spec.get("x_field")
    y_field = spec.get("y_field")
    top_n = spec.get("top_n")
    if not x_field or not y_field:
        st.caption("(bar 차트는 x_field 와 y_field 가 필요합니다)")
        return
    xs = _resolve_path(obj, x_field)
    ys = _resolve_path(obj, y_field)
    if not isinstance(xs, list) or not isinstance(ys, list):
        st.caption(f"(field 결과가 list 가 아님 — x={type(xs).__name__}, y={type(ys).__name__})")
        return
    pairs = []
    for x, y in zip(xs, ys):
        f = _to_float(y)
        if f is None or x is None:
            continue
        label = str(x)[:80]
        pairs.append({"label": label, "value": f})
    if not pairs:
        st.caption("(bar 값이 비어있음)")
        return
    pairs.sort(key=lambda r: r["value"], reverse=True)
    if isinstance(top_n, int) and top_n > 0:
        pairs = pairs[:top_n]
    df = pd.DataFrame(pairs).set_index("label")
    st.bar_chart(df, height=max(220, min(500, 30 * len(df) + 80)))
    with st.expander("📋 데이터 표", expanded=False):
        st.dataframe(pairs, use_container_width=True, hide_index=True)


def _chart_scatter(spec: dict, obj: Any) -> None:
    x_field = spec.get("x_field"); y_field = spec.get("y_field")
    if not x_field or not y_field:
        st.caption("(scatter 차트는 x_field 와 y_field 가 필요합니다)")
        return
    xs = _resolve_path(obj, x_field); ys = _resolve_path(obj, y_field)
    if not isinstance(xs, list) or not isinstance(ys, list):
        st.caption("(field 결과가 list 가 아님)")
        return
    pts = []
    for x, y in zip(xs, ys):
        fx = _to_float(x); fy = _to_float(y)
        if fx is None or fy is None:
            continue
        pts.append({"x": fx, "y": fy})
    if not pts:
        st.caption("(scatter 값이 비어있음)")
        return
    df = pd.DataFrame(pts)
    st.scatter_chart(df, x="x", y="y", height=300)


def _chart_histogram(spec: dict, obj: Any) -> None:
    field = spec.get("field")
    bins = spec.get("bins") or 20
    if not field:
        st.caption("(histogram 은 field 가 필요합니다)")
        return
    raw = _resolve_path(obj, field)
    if not isinstance(raw, list):
        st.caption("(field 결과가 list 가 아님)")
        return
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
        st.caption("(histogram 으로 쓸 숫자가 없음)")
        return
    series = pd.Series(nums, name="value")
    counts = pd.cut(series, bins=int(bins)).value_counts().sort_index()
    df = pd.DataFrame({"bucket": counts.index.astype(str), "count": counts.values}).set_index("bucket")
    st.bar_chart(df, height=240)
    st.caption(f"n={len(nums)}, bins={bins}")


def _chart_table(spec: dict, obj: Any) -> None:
    rows_field = spec.get("rows_field")
    columns = spec.get("columns")
    rows = _resolve_path(obj, rows_field) if rows_field else obj
    if not isinstance(rows, list):
        st.caption(f"(rows_field 결과가 list 가 아님 — rows_field={rows_field!r})")
        return
    if not rows:
        st.caption("(rows 가 비어있음)")
        return
    if columns:
        rows = [{c: r.get(c) if isinstance(r, dict) else r for c in columns} for r in rows]
    flat = [
        {k: _scalar(v, limit=200) for k, v in (r.items() if isinstance(r, dict) else [("value", r)])}
        for r in rows[:200]
    ]
    st.dataframe(flat, use_container_width=True, hide_index=True)
    if len(rows) > 200:
        st.caption(f"표시 200 / 전체 {len(rows)}")


def _render_message(m: dict, *, container=None) -> None:
    """한 메시지 카드 렌더. container 가 주어지면 그 안에 (placeholder.container() 등)."""
    target = container if container is not None else st

    role = m.get("role") or "ai"
    name = m.get("name")
    text = m.get("text") or ""
    tool_calls = m.get("tool_calls") or []
    tool_call_id = m.get("tool_call_id")

    # human
    if role in ("human", "user"):
        with target.chat_message("user", avatar="🙋"):
            target.markdown(text or "_(empty)_")
        return

    # tool result
    if role == "tool":
        with target.chat_message("assistant", avatar="🛠️"):
            header = f"🛠️ tool result · `{name or '?'}`"
            if tool_call_id:
                header += f" · id=`{tool_call_id}`"
            target.caption(header)
            obj: Any = None
            if text:
                try:
                    obj = json.loads(text)
                except Exception:
                    obj = None
            rendered = _render_result_payload(target, obj) if isinstance(obj, dict) else False
            if not rendered:
                if isinstance(obj, list):
                    flat = _flatten_for_table(obj)
                    if flat:
                        target.dataframe(flat, use_container_width=True, hide_index=True)
                        rendered = True
                if not rendered and isinstance(obj, dict):
                    _render_kv_table(target, obj)
                    rendered = True
            if not rendered:
                # plain text 또는 JSON 파싱 실패
                target.code(text or "(empty)", language="text", wrap_lines=False)
            # 원본 JSON 은 expander 로 보존
            if obj is not None:
                with target.expander("raw JSON", expanded=False):
                    target.json(obj, expanded=False)
        return

    # ai
    avatar = _AGENT_AVATAR.get(name, "🤖")
    with target.chat_message("assistant", avatar=avatar):
        if text:
            target.markdown(text)

        for tc in tool_calls:
            tname = tc.get("name") or "?"
            args = tc.get("args") or {}
            if _is_handoff_tool(tname):
                continue   # 핸드오프는 내부 라우팅 — UI에 표시하지 않음
            target.markdown(f"🛠️ **tool_call** · `{tname}`")
            if isinstance(args, dict):
                _render_kv_table(target, args)
            else:
                target.code(_short_args(args, limit=2000), language="json")


# ───────────────────────── 비스트리밍 (기존 호환) ─────────────────────────


def render(result: dict, request: dict | None = None) -> None:
    """이미 받아둔 결과(dict)를 자연스러운 형태로 렌더 — 답변 본문 + 차트,
    사고 과정(도구 호출 로그)은 접이식."""
    if "error" in result:
        st.error(result["error"])
        return

    msgs = result.get("messages") or []
    if not msgs:
        st.info("메시지 없음.")
        return

    # 최종 답변: report_agent 메시지 우선, 없으면 마지막 자연어 ai 메시지
    report_msg = next((m for m in reversed(msgs) if m.get("name") == "report_agent"), None)
    if report_msg:
        _render_report(report_msg.get("text") or "", report_msg.get("_charts") or [], msgs)
    else:
        last_ai = next(
            (m for m in reversed(msgs)
             if m.get("role") == "ai" and not m.get("tool_calls") and (m.get("text") or "").strip()),
            None,
        )
        if last_ai:
            st.markdown(last_ai["text"])
            _render_supervisor_charts(st, msgs)

    # 검증 결과 한 줄 (있을 때만)
    v = next((m.get("_validation") for m in msgs if m.get("_validation")), None)
    if v is not None:
        st.caption("🧐 검증 통과" if v.get("passed") else f"🧐 검증 이슈 {len(v.get('issues') or [])}건 → 재분석 수행")

    with st.expander("🧠 사고 과정", expanded=False):
        for m in msgs:
            if m.get("name") in ("validation_agent", "report_agent"):
                continue
            _render_message(m)


# ───────────────────────── Streaming ─────────────────────────


def render_stream(events: Iterator[dict], request: dict | None = None) -> dict:
    """invoke_stream() 의 NDJSON 이벤트를 자연스러운 채팅 형태로 렌더.

    고정 UI(메트릭 카드·핸드오프 시퀀스·specialist 라벨) 없이:
      - 진행 중: 상태 한 줄 + 접이식 '🧠 사고 과정'(도구 호출/결과 로그)
      - 완료: 최종 답변 본문 + 차트 이미지
    반환 dict 는 비스트리밍 render() 입력과 동일 구조 (messages/handoffs/final/aborted).
    """
    status_box = st.empty()
    status_box.caption("⏳ 분석 시작…")

    # 사고 과정(도구 호출·중간 발화)은 접어서 — 원하는 사람만 펼쳐본다
    thought_exp = st.expander("🧠 사고 과정", expanded=False)

    messages: list[dict] = []
    handoffs: list[str] = []
    aborted: str | None = None
    final_active: str | None = None
    err: str | None = None
    reported = False
    n_tools = 0

    answer_box = st.container()  # 최종 답변이 놓일 자리

    for ev in events:
        t = ev.get("type")

        if t == "start":
            reason = ev.get("reasoning")
            status_box.caption(f"⏳ {reason}" if reason else "⏳ 분석 중…")
        elif t == "handoff":
            handoffs.append(ev.get("agent") or "?")   # 결과 dict 호환용 — UI 표시는 안 함
        elif t == "message":
            msg = ev.get("message") or {}
            messages.append(msg)
            role = msg.get("role")
            text = (msg.get("text") or "").strip()
            tcs = msg.get("tool_calls") or []
            if role == "ai" and tcs:
                n_tools += len(tcs)
                # 도구 직전 예고 문장이 있으면 그대로 상태로 (Claude Code 식 진행 중계)
                if text:
                    status_box.caption(f"💬 {text[:200]}")
                else:
                    names = ", ".join(tc.get("name", "?") for tc in tcs)
                    status_box.caption(f"🛠️ {names} 확인 중… (도구 {n_tools}회)")
                with thought_exp:
                    if text:
                        st.markdown(text)
                    for tc in tcs:
                        st.caption(f"🛠️ `{tc.get('name','?')}` · {_short_args(tc.get('args') or {})}")
            elif role == "tool":
                with thought_exp:
                    st.caption(f"↩︎ `{msg.get('name') or 'tool'}` 결과 수신")
            elif role == "ai" and text:
                # 자연어 중간 발화도 사고 과정에 쌓아둠 (최종 답변은 done/report에서)
                with thought_exp:
                    st.markdown(text)
        elif t == "stage":
            stage = ev.get("stage", "?")
            label = {"domain": "분석", "analyze": "분석", "single": "분석",
                     "validation": "검증", "validate": "검증",
                     "revise": "재분석", "report": "리포트 작성", "route": "경로 판정"}.get(stage, stage)
            status_box.caption(f"🔄 {label} 완료")
        elif t == "validation":
            passed = ev.get("passed", True)
            issues = ev.get("issues") or []
            with thought_exp:
                if passed:
                    st.caption("🧐 검증 통과")
                else:
                    st.caption(f"🧐 검증 이슈 {len(issues)}건 — 재분석")
            messages.append({"role": "ai", "name": "validation_agent", "text": "", "tool_calls": [],
                             "_validation": {"passed": passed, "issues": issues}})
        elif t == "report":
            md = ev.get("markdown") or ""
            charts = ev.get("charts") or []
            with answer_box:
                _render_report(md, charts, messages)
            messages.append({"role": "ai", "name": "report_agent", "text": md, "tool_calls": [],
                             "_charts": charts})
            reported = True
        elif t == "abort":
            aborted = ev.get("reason")
            status_box.warning(f"⚠️ 중단: {aborted}")
        elif t == "error":
            err = ev.get("error")
            status_box.error(f"❌ {err}")
            break
        elif t == "done":
            final_active = ev.get("final_active_agent")
            status_box.caption(f"✅ 완료 · 도구 {n_tools}회 사용" if n_tools else "✅ 완료")
            if not reported:
                last_ai = next(
                    (m for m in reversed(messages)
                     if m.get("role") == "ai" and not m.get("tool_calls") and (m.get("text") or "").strip()),
                    None,
                )
                if last_ai and last_ai.get("text"):
                    with answer_box:
                        st.markdown(last_ai["text"])
                        _render_supervisor_charts(st, messages)

    return {
        "messages": messages,
        "handoffs": handoffs,
        "final_active_agent": final_active,
        "aborted": aborted,
        **({"error": err} if err else {}),
    }
