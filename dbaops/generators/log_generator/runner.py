"""log generator entrypoint — pg / mysql / kafka 라인을 S3 (gz) + CW Logs 송출.

env:
  SOURCE: postgres | mysql | kafka
  MODE: baseline | burst
  DURATION_SEC: 수행 시간
  RATE: baseline=1 line/s, burst=200 line/min 기본 (LINES_PER_SEC 로 직접 지정 가능)
  S3_BUCKET: 출력 버킷 (없으면 S3 송출 스킵)
  S3_PREFIX: 키 접두 (default 'logs')
  CW_LOG_GROUP: CWLogs 로그 그룹명 (없으면 CW 송출 스킵)
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import time
from datetime import datetime, timezone

import boto3

from .templates import line_for

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("log_generator")

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
BUFFER_FLUSH_SEC = 30


def _rate(mode: str) -> float:
    if "LINES_PER_SEC" in os.environ:
        return float(os.environ["LINES_PER_SEC"])
    return 200 / 60 if mode == "burst" else 1.0


def _s3_key(source: str, prefix: str) -> str:
    now = datetime.now(timezone.utc)
    return f"{prefix}/{source}/{now:%Y/%m/%d/%H}/{now:%Y%m%dT%H%M%S}-{os.getpid()}.log.gz"


def _flush_to_s3(s3_client, bucket: str, key: str, lines: list[str]) -> None:
    body = io.BytesIO()
    with gzip.GzipFile(fileobj=body, mode="wb") as gz:
        gz.write(("\n".join(lines) + "\n").encode("utf-8"))
    body.seek(0)
    s3_client.put_object(Bucket=bucket, Key=key, Body=body.read())
    logger.info("flushed %d lines → s3://%s/%s", len(lines), bucket, key)


def _ensure_log_stream(cw_client, group: str, stream: str) -> None:
    try:
        cw_client.create_log_group(logGroupName=group)
    except cw_client.exceptions.ResourceAlreadyExistsException:
        pass
    try:
        cw_client.create_log_stream(logGroupName=group, logStreamName=stream)
    except cw_client.exceptions.ResourceAlreadyExistsException:
        pass


def _flush_to_cw(cw_client, group: str, stream: str, events: list[dict]) -> None:
    if not events:
        return
    cw_client.put_log_events(logGroupName=group, logStreamName=stream, logEvents=events)


def main() -> int:
    source = os.environ.get("SOURCE", "postgres")
    mode = os.environ.get("MODE", "baseline")
    duration = int(os.environ.get("DURATION_SEC", "120"))
    rate = _rate(mode)
    interval = 1.0 / max(rate, 0.001)

    bucket = os.environ.get("S3_BUCKET")
    prefix = os.environ.get("S3_PREFIX", "logs")
    cw_group = os.environ.get("CW_LOG_GROUP")
    cw_stream = os.environ.get("CW_LOG_STREAM", f"{source}-{os.getpid()}")

    logger.info("source=%s mode=%s rate=%.3f/s duration=%ds", source, mode, rate, duration)

    s3 = boto3.client("s3", region_name=REGION) if bucket else None
    cw = boto3.client("logs", region_name=REGION) if cw_group else None
    if cw and cw_group:
        _ensure_log_stream(cw, cw_group, cw_stream)

    end = time.time() + duration
    buf_lines: list[str] = []
    buf_events: list[dict] = []
    last_flush = time.time()

    while time.time() < end:
        line = line_for(source, mode)
        buf_lines.append(line)
        buf_events.append({"timestamp": int(time.time() * 1000), "message": line[:262144]})
        time.sleep(interval)

        if time.time() - last_flush >= BUFFER_FLUSH_SEC:
            if s3 and bucket and buf_lines:
                _flush_to_s3(s3, bucket, _s3_key(source, prefix), buf_lines)
                buf_lines = []
            if cw and cw_group and buf_events:
                _flush_to_cw(cw, cw_group, cw_stream, buf_events)
                buf_events = []
            last_flush = time.time()

    if s3 and bucket and buf_lines:
        _flush_to_s3(s3, bucket, _s3_key(source, prefix), buf_lines)
    if cw and cw_group and buf_events:
        _flush_to_cw(cw, cw_group, cw_stream, buf_events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
