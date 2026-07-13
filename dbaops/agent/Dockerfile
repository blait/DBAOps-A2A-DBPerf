# syntax=docker/dockerfile:1.6
# AgentCore Runtime / 올인원 EC2 컨테이너.
# AgentCore Runtime 은 ARM64 강제 → build 시 --build-arg TARGETPLATFORM 없이 buildx 가 처리.
# 올인원 EC2(x86 포함)에서는 docker compose 가 호스트 아키텍처로 빌드하도록 platform 미고정.
ARG BASE_PLATFORM=linux/amd64
FROM --platform=${BASE_PLATFORM} python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

EXPOSE 8080

# AgentCore Runtime 은 :8080/ping, :8080/invocations 를 호출한다.
CMD ["python", "-m", "dbaops_agent.runtime_entry"]
