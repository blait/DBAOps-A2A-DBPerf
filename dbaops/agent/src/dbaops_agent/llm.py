"""Bedrock Opus 4.7 LLM 클라이언트."""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_aws import ChatBedrockConverse


@lru_cache(maxsize=1)
def get_llm() -> ChatBedrockConverse:
    region = os.environ.get("BEDROCK_REGION", "ap-northeast-2")
    model_id = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7")
    # Opus 4.7 등 일부 모델은 temperature 파라미터를 거부 → 옵션으로만 보냄.
    use_temp = os.environ.get("BEDROCK_USE_TEMPERATURE", "0") == "1"
    kwargs = {"model": model_id, "region_name": region, "max_tokens": 4096}
    if use_temp:
        kwargs["temperature"] = 0.0
    return ChatBedrockConverse(**kwargs)
