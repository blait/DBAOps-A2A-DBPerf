"""kafka_isr_shrink — producer batch jump + consumer pause 모사.

MSK Serverless 는 ISR/under-replicated 메트릭을 직접 흔들 수 없지만,
producer 가 큰 batch 를 한꺼번에 밀고 consumer 가 멈추면 ConsumerLag 메트릭이 급등한다.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time

from .._kafka import ensure_topic, make_consumer, make_producer

logger = logging.getLogger(__name__)


def _producer_burst(end: float, topic: str, batch_size: int = 5000) -> None:
    producer = make_producer()
    while time.time() < end:
        for _ in range(batch_size):
            msg = {"ts": time.time(), "v": random.random()}
            producer.produce(topic, json.dumps(msg).encode())
        producer.poll(0)
        producer.flush(5.0)
        logger.info("burst %d msgs", batch_size)
        time.sleep(5.0)


def _paused_consumer(end: float, topic: str) -> None:
    """consumer 를 만들고 그냥 sleep — lag 가 누적되도록."""
    c = make_consumer(group_id="dbaops-paused", topics=[topic])
    while time.time() < end:
        time.sleep(5.0)
        # 가끔 한 건만 poll
        c.poll(0.5)
    c.close()


def run(duration_sec: int) -> int:
    if not os.environ.get("MSK_BOOTSTRAP"):
        logger.info("MSK_BOOTSTRAP not set — skipping kafka_isr_shrink")
        return 0
    topic = os.environ.get("KAFKA_TOPIC", "dbaops.orders")
    if not ensure_topic(topic):
        logger.warning("kafka topic '%s' unavailable — skipping", topic)
        return 0
    end = time.time() + duration_sec
    threads = [
        threading.Thread(target=_producer_burst, args=(end, topic), daemon=True),
        threading.Thread(target=_paused_consumer, args=(end, topic), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration_sec + 30)
    logger.info("kafka_isr_shrink finished")
    return 0
