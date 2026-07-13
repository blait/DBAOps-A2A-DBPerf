"""prometheus_query Lambda — PromQL /query, /query_range 프록시.

입력:  {"promql": str, "start": iso, "end": iso, "step": str}
출력:  {"series": [{"ts": iso, "value": float}, ...], "labels": {...}}
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "")


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    promql = body["promql"]
    start = body.get("start")
    end = body.get("end")
    step = body.get("step", "30s")

    qs = urllib.parse.urlencode(
        {"query": promql, "start": start, "end": end, "step": step}
    )
    url = f"{PROMETHEUS_URL}/api/v1/query_range?{qs}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())

    series = []
    for s in data.get("data", {}).get("result", []):
        for ts, v in s.get("values", []):
            series.append({"ts": ts, "value": float(v)})
    return {"series": series}
