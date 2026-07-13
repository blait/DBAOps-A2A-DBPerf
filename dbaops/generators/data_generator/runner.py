"""Data generator entrypoint — workload 이름을 받아 모듈 dispatch."""

from __future__ import annotations

import importlib
import logging
import os
import sys

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("data_generator")


def main() -> int:
    workload = os.environ.get("WORKLOAD") or (sys.argv[1] if len(sys.argv) > 1 else "baseline")
    duration = int(os.environ.get("DURATION_SEC", "120"))
    logger.info("starting workload=%s duration=%ds", workload, duration)
    mod = importlib.import_module(f"data_generator.workloads.{workload}")
    return int(mod.run(duration_sec=duration))


if __name__ == "__main__":
    raise SystemExit(main())
