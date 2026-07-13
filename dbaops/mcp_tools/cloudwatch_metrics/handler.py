"""cloudwatch_metrics Lambda — GetMetricData 정규화.

입력: {"namespace", "metric", "dimensions", "start", "end", "stat", "period"}
출력: {"series": [{"ts", "value"}]}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cw = boto3.client("cloudwatch")


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raise ValueError(f"unrecognized timestamp: {value!r}")


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    start = _parse_ts(body["start"])
    end = _parse_ts(body["end"])
    period = int(body.get("period", 60))
    if (end - start).total_seconds() / period > 1440:
        period = max(int((end - start).total_seconds() / 1440), 60)

    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": body["namespace"],
                        "MetricName": body["metric"],
                        "Dimensions": [
                            {"Name": k, "Value": v} for k, v in (body.get("dimensions") or {}).items()
                        ],
                    },
                    "Period": period,
                    "Stat": body.get("stat", "Average"),
                },
                "ReturnData": True,
            }
        ],
        StartTime=start,
        EndTime=end,
        ScanBy="TimestampAscending",
    )
    points = resp.get("MetricDataResults", [{}])[0]
    series = [
        {"ts": ts.isoformat(), "value": float(v)}
        for ts, v in zip(points.get("Timestamps", []), points.get("Values", []))
    ]
    logger.info(
        "ns=%s metric=%s dims=%s period=%d points=%d",
        body["namespace"], body["metric"], body.get("dimensions") or {}, period, len(series),
    )
    return {"series": series}
