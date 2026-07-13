"""Kafka producer/consumer 헬퍼 — IAM SASL/OAUTHBEARER."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _common_iam_config() -> dict[str, Any]:
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    return {
        "bootstrap.servers": os.environ["MSK_BOOTSTRAP"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanism": "OAUTHBEARER",
        "client.id": os.environ.get("KAFKA_CLIENT_ID", "dbaops-generator"),
        # confluent-kafka 가 oauth_cb 콜백을 호출하도록 둠
        "sasl.oauthbearer.method": "default",
        # AWS region 으로 오버라이드
        "sasl.oauthbearer.config": f"region={region}",
    }


def _oauth_cb(_config):
    """AWS MSK IAM SASL token 콜백 — aws_msk_iam_sasl_signer 사용."""
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider  # type: ignore

    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
    return token, expiry_ms / 1000.0


def make_producer():
    from confluent_kafka import Producer

    cfg = _common_iam_config()
    cfg["oauth_cb"] = _oauth_cb
    return Producer(cfg)


def make_consumer(group_id: str, topics: list[str]):
    from confluent_kafka import Consumer

    cfg = _common_iam_config()
    cfg.update(
        {
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "oauth_cb": _oauth_cb,
        }
    )
    c = Consumer(cfg)
    c.subscribe(topics)
    return c


def ensure_topic(topic: str, *, num_partitions: int = 3, replication_factor: int = 2,
                 timeout_sec: float = 30.0) -> bool:
    """topic 이 없으면 만든다. 이미 있으면 True 반환, 만들면 True, 실패하면 False.

    MSK Serverless 는 auto.create.topics.enable 이 OFF — admin 으로 만들어야 한다.
    """
    if not os.environ.get("MSK_BOOTSTRAP"):
        logger.info("ensure_topic skip — MSK_BOOTSTRAP not set")
        return False

    try:
        from confluent_kafka.admin import AdminClient, NewTopic
    except Exception as e:  # noqa: BLE001
        logger.warning("kafka admin import failed: %s", e)
        return False

    cfg = _common_iam_config()
    cfg["oauth_cb"] = _oauth_cb
    admin = AdminClient(cfg)

    try:
        md = admin.list_topics(timeout=timeout_sec)
    except Exception as e:  # noqa: BLE001
        logger.warning("admin.list_topics failed: %s", e)
        return False

    if topic in (md.topics or {}):
        logger.info("topic %s already exists", topic)
        return True

    new = NewTopic(topic, num_partitions=num_partitions, replication_factor=replication_factor)
    futures = admin.create_topics([new], operation_timeout=timeout_sec, request_timeout=timeout_sec)
    fut = futures.get(topic)
    if fut is None:
        return False
    try:
        fut.result(timeout=timeout_sec)
        logger.info("created topic %s (partitions=%d rf=%d)", topic, num_partitions, replication_factor)
        return True
    except Exception as e:  # noqa: BLE001
        # AlreadyExists 류는 성공 취급
        msg = str(e)
        if "AlreadyExists" in msg or "TopicExistsException" in msg:
            logger.info("topic %s already existed (race)", topic)
            return True
        logger.warning("create_topic %s failed: %s", topic, e)
        return False
