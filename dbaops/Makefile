SHELL := /bin/bash

REGION ?= ap-northeast-2
ENV    ?= poc
TF_DIR := infra/envs/$(ENV)

.PHONY: help bootstrap verify plan apply deploy-agent demo-up demo-down destroy fmt lint test

help:
	@echo "DBAOps-Agent — make targets"
	@echo "  bootstrap     TF state S3 + DynamoDB lock 부트스트랩"
	@echo "  verify        AgentCore 서울 GA 가드"
	@echo "  plan          terraform plan"
	@echo "  apply         terraform apply"
	@echo "  deploy-agent  agent 컨테이너 빌드 + ECR push + Runtime 갱신"
	@echo "  demo-up       데이터/로그 생성기 ECS 서비스 켜기"
	@echo "  demo-down     생성기 서비스 끄기 (DB도 stop)"
	@echo "  destroy       전체 인프라 제거"
	@echo "  fmt / lint / test  코드 품질"

bootstrap:
	bash scripts/bootstrap.sh

verify:
	bash scripts/verify_agentcore_seoul.sh

plan:
	cd $(TF_DIR) && terraform init -upgrade && terraform plan -out=tfplan

apply:
	cd $(TF_DIR) && terraform apply tfplan

deploy-agent:
	bash scripts/build_agent_image.sh
	python scripts/register_gateway_targets.py

demo-up:
	bash scripts/demo_up.sh

demo-down:
	bash scripts/demo_down.sh

destroy:
	cd $(TF_DIR) && terraform destroy

fmt:
	cd agent && ruff format src tests || true
	cd $(TF_DIR) && terraform fmt -recursive ../../

lint:
	cd agent && ruff check src tests || true

test:
	cd agent && pytest -q || true
