#!/usr/bin/env bash
# Aurora PG / RDS MySQL 시드.
# 현재 generator (data_generator) 가 첫 부하 시점에 schema/seed 를 자동 생성하므로
# 이 스크립트를 별도로 호출할 필요는 거의 없음. ad-hoc baseline 1회 실행만.
set -euo pipefail

echo "==> baseline 시나리오 1회 실행해 schema/seed 자동 생성"
bash "$(dirname "$0")/demo_up.sh" data-baseline
echo "done. 이후 시나리오는 자유롭게 trigger."
