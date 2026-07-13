"""msk_metrics Lambda — CloudWatch AWS/Kafka 메트릭 조회.

입력: {"cluster_arn", "metric", "start", "end", "stat"?, "topic"?, "consumer_group"?, "broker_id"?, "partition"?}
출력: {"series": [...]}

AWS/Kafka 메트릭은 dimension 조합에 따라 노출되는 메트릭이 다르다:
- BytesInPerSec / BytesOutPerSec / MessagesInPerSec
    → Cluster Name + Topic  (Topic 없으면 series 0)
- MaxOffsetLag / SumOffsetLag / EstimatedMaxTimeLag
    → Cluster Name + Consumer Group + Topic
- UnderReplicatedPartitions / GlobalPartitionCount (Provisioned only)
    → Cluster Name (broker level)

핸들러는 메트릭 이름과 인자를 보고 자동으로 적절한 dimension 조합을 만든다.
인자에 topic/consumer_group/partition/broker_id 가 명시되면 그걸 우선 사용.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

cw = boto3.client("cloudwatch")

# 메트릭별 필요 dimension 매핑 (Cluster Name 외 추가로 필요한 것)
_TOPIC_METRICS = {
    "BytesInPerSec", "BytesOutPerSec", "MessagesInPerSec",
    "FetchMessageConversionsPerSec", "ProduceMessageConversionsPerSec",
}
_CG_TOPIC_METRICS = {
    "MaxOffsetLag", "SumOffsetLag", "EstimatedMaxTimeLag",
}


def _build_dimensions(metric: str, cluster_name: str, body: dict) -> list[dict]:
    dims: list[dict] = [{"Name": "Cluster Name", "Value": cluster_name}]

    topic = body.get("topic") or os.environ.get("KAFKA_DEFAULT_TOPIC", "dbaops.orders")
    consumer_group = body.get("consumer_group") or os.environ.get("KAFKA_DEFAULT_CG", "dbaops-paused")
    broker_id = body.get("broker_id")
    partition = body.get("partition")

    if metric in _TOPIC_METRICS:
        dims.append({"Name": "Topic", "Value": topic})
    elif metric in _CG_TOPIC_METRICS:
        dims.append({"Name": "Consumer Group", "Value": consumer_group})
        dims.append({"Name": "Topic", "Value": topic})

    # Optional broker / partition (Provisioned only)
    if broker_id:
        dims.append({"Name": "Broker ID", "Value": str(broker_id)})
    if partition is not None:
        dims.append({"Name": "Partition", "Value": str(partition)})

    return dims


def handler(event: dict, _ctx) -> dict:
    body = event.get("body") or event
    if isinstance(body, str):
        body = json.loads(body)

    cluster_arn = body["cluster_arn"]
    metric = body["metric"]
    # cluster_arn 이 'msk-cluster' placeholder 면 env 의 클러스터 이름 사용
    if cluster_arn.startswith("arn:"):
        cluster_name = cluster_arn.split("/")[-2]
    else:
        cluster_name = os.environ.get("KAFKA_CLUSTER_NAME", "dbaops-poc")

    dims = _build_dimensions(metric, cluster_name, body)
    logger.info("msk_metrics %s: dims=%s", metric, [(d['Name'], d['Value']) for d in dims])

    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "m1",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Kafka",
                        "MetricName": metric,
                        "Dimensions": dims,
                    },
                    "Period": int(body.get("period", 60)),
                    "Stat": body.get("stat", "Average"),
                },
                "ReturnData": True,
            }
        ],
        StartTime=body["start"],
        EndTime=body["end"],
    )
    pts = resp.get("MetricDataResults", [{}])[0]
    series = [
        {"ts": ts.isoformat(), "value": float(v)}
        for ts, v in zip(pts.get("Timestamps", []), pts.get("Values", []))
    ]
    return {
        "series":     series,
        "dimensions": [{"name": d["Name"], "value": d["Value"]} for d in dims],
        "metric":     metric,
        "stat":       body.get("stat", "Average"),
    }
