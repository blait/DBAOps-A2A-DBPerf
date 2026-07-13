"""s3_log_fetch Lambda — gz 로그 byte-range + regex 매칭.

두 가지 동작:
1) 파일 fetch (기본):   {"bucket", "key", "byte_range"?, "regex"?, "max_lines": 5000}
                        → {"lines": [..], "truncated": bool}
2) 파일 listing:        {"bucket", "prefix", "max_keys"?: 100, "since_minutes"?: int}
                        → {"objects": [{"key", "size", "last_modified"}, ...], "count", "is_truncated"}

`prefix` 가 있으면 listing 모드. 둘 다 있으면 fetch 우선.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")


def _list(bucket: str, prefix: str, max_keys: int = 100,
          since_minutes: int | None = None) -> dict:
    """S3 prefix 아래 객체 목록 — log specialist 가 어떤 key 가 존재하는지 먼저 탐색하기 위함."""
    kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": min(max_keys, 1000)}
    cutoff: datetime | None = None
    if since_minutes:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))

    objects: list[dict] = []
    is_truncated = False
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents") or []:
            lm = obj.get("LastModified")
            if cutoff and lm and lm < cutoff:
                continue
            objects.append({
                "key": obj.get("Key"),
                "size": obj.get("Size"),
                "last_modified": lm.astimezone(timezone.utc).isoformat(timespec="seconds") if lm else None,
            })
            if len(objects) >= max_keys:
                is_truncated = True
                break
        if len(objects) >= max_keys:
            break

    # 최신 객체가 위로 오게 정렬 — 분석 대상이 보통 최신
    objects.sort(key=lambda o: o.get("last_modified") or "", reverse=True)
    return {"objects": objects, "count": len(objects), "is_truncated": is_truncated}


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    bucket = body.get("bucket")
    if not bucket:
        return {"error": "missing bucket"}

    # listing 모드: prefix 가 있고 key 가 없을 때
    if body.get("prefix") and not body.get("key"):
        return _list(
            bucket,
            body["prefix"],
            max_keys=int(body.get("max_keys", 100)),
            since_minutes=body.get("since_minutes"),
        )

    key = body.get("key")
    if not key:
        return {"error": "missing key (or use prefix for listing)"}

    byte_range = body.get("byte_range")
    regex = body.get("regex")
    max_lines = int(body.get("max_lines", 5000))

    kwargs: dict = {"Bucket": bucket, "Key": key}
    if byte_range:
        kwargs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"

    obj = s3.get_object(**kwargs)
    raw = obj["Body"].read()
    if key.endswith(".gz"):
        raw = gzip.decompress(raw)

    pattern = re.compile(regex) if regex else None
    out: list[str] = []
    truncated = False
    for line in io.BytesIO(raw):
        if pattern and not pattern.search(line.decode("utf-8", errors="replace")):
            continue
        out.append(line.decode("utf-8", errors="replace").rstrip())
        if len(out) >= max_lines:
            truncated = True
            break
    return {"lines": out, "truncated": truncated}
