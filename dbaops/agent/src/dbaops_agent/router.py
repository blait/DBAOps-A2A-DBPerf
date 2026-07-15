"""router.py — 요청 성격 판정: 대화(single) vs 보고서 파이프라인(pipeline).

perf 에이전트의 route 노드와 동일한 정책을 dbaops 쪽에 적용한다.
mode=auto (또는 미지정) 요청이 들어오면 LLM 1호출로:
  - SINGLE            → single 그래프 (빠른 대화형 ReAct)
  - REPORT <domain>   → pipeline 그래프 (domain → validation → revise → report)

pipeline은 domain이 필수이므로 REPORT 판정 시 domain(os_metric|db_metric|log)도
함께 고른다. 판정 실패/모호하면 안전하게 single로 폴백.
"""

from __future__ import annotations

import logging

from .llm import get_llm
from .pipeline_graph import domain_keys

logger = logging.getLogger(__name__)

_ROUTER_SYSTEM = """You are a request router for a DB/infra RCA agent.
Decide if the user wants a FORMAL REPORT (audited, structured deliverable) or a normal chat answer.

Choose REPORT only when the user explicitly asks for a report/summary document, an audit,
a comprehensive analysis to share/keep ("보고서", "리포트로", "정리해서 문서로", "종합 분석해서
보고", "감사용", "경영진에게"), or asks for validated findings.
Everything else — questions, live checks, metric lookups, chart requests, casual chat — is SINGLE.

If REPORT, also pick the closest domain:
  os_metric — host/OS/EC2 metrics (CPU, memory, disk, network)
  db_metric — database performance (Aurora PG, RDS MySQL, PI, Kafka/MSK)
  log       — log analysis (RDS/S3/CloudWatch logs, error hunting)

Reply with EXACTLY one line:
SINGLE
or
REPORT <domain>"""


def decide(free_text: str) -> tuple[str, str | None]:
    """(mode, domain) 반환 — mode는 'single'|'pipeline', domain은 pipeline일 때만."""
    try:
        resp = get_llm().invoke([
            {"role": "system", "content": _ROUTER_SYSTEM},
            {"role": "user", "content": (free_text or "")[:2000]},
        ])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        text = text.strip().upper()
        if text.startswith("REPORT"):
            parts = text.split()
            domain = parts[1].lower() if len(parts) > 1 else "db_metric"
            if domain not in domain_keys():
                domain = "db_metric"
            logger.info("router: pipeline/%s", domain)
            return "pipeline", domain
    except Exception:  # noqa: BLE001
        logger.exception("router failed — falling back to single")
    logger.info("router: single")
    return "single", None
