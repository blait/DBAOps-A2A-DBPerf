# 공용 베이스 이미지 — 각 MCP 도구가 자기 디렉토리에 Dockerfile 두고
# `FROM dbaops-mcp-base:latest` 식으로 쓰지 않고, 각 Dockerfile 이 직접 베이스를 포함한다.
# 이 파일은 참고용 (각 tool 디렉토리의 Dockerfile 이 source of truth).

FROM public.ecr.aws/lambda/python:3.12-arm64
