"""rds_pi Lambda — Performance Insights top SQL by AAS.

입력: {"db_id", "start", "end", "group_by": "db.sql_tokenized" | "db.wait_event" | ...}
출력: {"top_sql": [{"statement"|"label", "aas"}]}

PI 의 GroupBy.Group 은 dimension 의 prefix 임 (예: "db.sql_tokenized" group 의 dimension 은
db.sql_tokenized.statement / db.sql_tokenized.id / db.sql_tokenized.db_id 등). 그래서
사용자가 흔히 적는 ".statement" 같은 dimension name 을 group 으로 보내면 InvalidArgument.
이 핸들러는 자동으로 group prefix 만 잘라 보낸다.

또한 PI 의 Identifier 는 **DbiResourceId (db-XXXX)** 만 받음. agent 가 흔히 보내는
DBInstanceIdentifier (예: 'dbaops-poc-aurora-pg-writer') 는 NotAuthorized 떨어짐.
'db-' prefix 가 없으면 RDS describe 로 자동 변환한다.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

pi = boto3.client("pi")
rds = boto3.client("rds")


@lru_cache(maxsize=64)
def _resolve_dbi_resource_id(identifier: str) -> str:
    """DBInstanceIdentifier → DbiResourceId. 이미 db- prefix 면 그대로."""
    if not identifier:
        return identifier
    if identifier.startswith("db-"):
        return identifier
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=identifier)
        instances = resp.get("DBInstances") or []
        if instances:
            rid = instances[0].get("DbiResourceId")
            if rid:
                logger.info("resolved %s → %s", identifier, rid)
                return rid
    except Exception as e:  # noqa: BLE001
        logger.warning("describe_db_instances(%s) failed: %s", identifier, e)
    return identifier

_VALID_GROUPS = {
    "db.sql_tokenized": "db.sql_tokenized.statement",
    "db.sql":           "db.sql.statement",
    "db.wait_event":    "db.wait_event.name",
    "db.host":          "db.host.id",
    "db.user":          "db.user.name",
    "db.application":   "db.application.name",
    "db.session_type":  "db.session_type.name",
    "db.query":         "db.query.statement",
}


def _normalize_group(name: str) -> tuple[str, str]:
    """LLM/사용자가 dimension 풀네임을 줘도 group prefix 로 정규화.

    예) 'db.sql_tokenized.statement' → group='db.sql_tokenized', label_dim='db.sql_tokenized.statement'
    """
    if not name:
        return "db.sql_tokenized", "db.sql_tokenized.statement"
    if name in _VALID_GROUPS:
        return name, _VALID_GROUPS[name]
    # dimension full name? truncate after first 2 dotted segments
    parts = name.split(".")
    if len(parts) >= 2:
        prefix = ".".join(parts[:2])
        if prefix in _VALID_GROUPS:
            return prefix, name
    return "db.sql_tokenized", "db.sql_tokenized.statement"


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    group, label_dim = _normalize_group(body.get("group_by") or "db.sql_tokenized")
    db_id = _resolve_dbi_resource_id(body["db_id"])

    resp = pi.get_resource_metrics(
        ServiceType="RDS",
        Identifier=db_id,
        MetricQueries=[
            {
                "Metric": "db.load.avg",
                "GroupBy": {"Group": group, "Limit": 10},
            }
        ],
        StartTime=body["start"],
        EndTime=body["end"],
        PeriodInSeconds=60,
    )
    top: list[dict] = []
    for s in resp.get("MetricList", []):
        dims = s.get("Key", {}).get("Dimensions", {})
        values = s.get("DataPoints") or []
        avg = sum(p["Value"] for p in values) / max(len(values), 1)
        top.append({
            "label":     dims.get(label_dim) or "",
            "statement": dims.get("db.sql_tokenized.statement", ""),
            "dimensions": dims,
            "aas":       avg,
        })
    top.sort(key=lambda x: x["aas"], reverse=True)
    return {"group": group, "top_sql": top[:10]}
