"""Bedrock AgentCore Gateway / Runtime / Targets 멱등 등록.

전제: terraform apply 가 끝났고 다음 환경변수가 채워져 있다.
  - REGION (default ap-northeast-2)
  - ENV (default poc)

수행 단계:
  1. Terraform output 에서 cognito_user_pool_id, app_client_id, gateway_role_arn,
     runtime_role_arn, ecr_repository_url, prometheus_query_lambda_arn 을 읽는다.
  2. Cognito user pool domain 이 없으면 생성 (JWT discoveryUrl 발급용).
  3. Gateway 멱등 생성 (이름 충돌 시 갱신).
  4. mcp_tools/<tool>/tool_io.json 을 inline tool_schema 로 변환해 Lambda target 등록.
  5. Agent runtime 멱등 생성/갱신 (ECR 이미지가 push 되어 있어야 한다).

호출:
  python scripts/register_gateway_targets.py [--skip-runtime]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("register_gateway_targets")

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "envs" / os.environ.get("ENV", "poc")
TOOLS_DIR = ROOT / "mcp_tools"
REGION = os.environ.get("REGION", "ap-northeast-2")
GATEWAY_NAME = f"dbaops-{os.environ.get('ENV', 'poc')}"
RUNTIME_NAME = f"dbaops_{os.environ.get('ENV', 'poc')}"
COGNITO_DOMAIN_PREFIX = f"dbaops-{os.environ.get('ENV', 'poc')}-{REGION}"


def tf_output() -> dict[str, Any]:
    res = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=TF_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(res.stdout)
    return {k: v["value"] for k, v in raw.items()}


def ensure_cognito_domain(user_pool_id: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    pool = cognito.describe_user_pool(UserPoolId=user_pool_id)["UserPool"]
    if pool.get("Domain"):
        domain = pool["Domain"]
        logger.info("cognito domain exists: %s", domain)
        return domain
    domain = COGNITO_DOMAIN_PREFIX
    cognito.create_user_pool_domain(Domain=domain, UserPoolId=user_pool_id)
    logger.info("created cognito domain %s", domain)
    return domain


def discovery_url(user_pool_id: str) -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration"


def find_gateway(client, name: str) -> dict | None:
    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        for gw in page.get("items", []):
            if gw.get("name") == name:
                return gw
    return None


def wait_gateway_ready(client, gw_id: str, max_wait_sec: int = 120) -> None:
    import time

    elapsed = 0
    while elapsed < max_wait_sec:
        st = client.get_gateway(gatewayIdentifier=gw_id).get("status")
        if st == "READY":
            return
        logger.info("gateway %s status=%s — waiting", gw_id, st)
        time.sleep(5)
        elapsed += 5
    raise TimeoutError(f"gateway {gw_id} not READY in {max_wait_sec}s")


def upsert_gateway(client, role_arn: str, user_pool_id: str, app_client_id: str) -> dict:
    existing = find_gateway(client, GATEWAY_NAME)
    auth_cfg = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url(user_pool_id),
            "allowedClients": [app_client_id],
        }
    }
    proto_cfg = {
        "mcp": {
            "supportedVersions": ["2025-03-26"],
            "instructions": "DBAOps MCP gateway — 6 tools.",
            "searchType": "SEMANTIC",
        }
    }
    if existing:
        gw_id = existing["gatewayId"]
        # 이미 같은 role/auth 라면 update 호출하지 않음 (UPDATING 상태 회피)
        try:
            full = client.get_gateway(gatewayIdentifier=gw_id)
            same_role = full.get("roleArn") == role_arn
            same_clients = (
                full.get("authorizerConfiguration", {})
                .get("customJWTAuthorizer", {})
                .get("allowedClients")
                == [app_client_id]
            )
            if same_role and same_clients:
                logger.info("gateway %s already in desired state — skipping update", GATEWAY_NAME)
                wait_gateway_ready(client, gw_id)
                return full
        except ClientError as e:  # noqa: BLE001
            logger.warning("get_gateway failed: %s — proceeding with update", e)
        logger.info("updating gateway %s (%s)", GATEWAY_NAME, gw_id)
        client.update_gateway(
            gatewayIdentifier=gw_id,
            name=GATEWAY_NAME,
            description="DBAOps PoC gateway",
            roleArn=role_arn,
            protocolType="MCP",
            protocolConfiguration=proto_cfg,
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration=auth_cfg,
        )
        wait_gateway_ready(client, gw_id)
        return client.get_gateway(gatewayIdentifier=gw_id)
    logger.info("creating gateway %s", GATEWAY_NAME)
    created = client.create_gateway(
        name=GATEWAY_NAME,
        description="DBAOps PoC gateway",
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration=proto_cfg,
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration=auth_cfg,
    )
    wait_gateway_ready(client, created["gatewayId"])
    return created


def list_targets(client, gateway_id: str) -> list[dict]:
    paginator = client.get_paginator("list_gateway_targets")
    out = []
    for page in paginator.paginate(gatewayIdentifier=gateway_id):
        out.extend(page.get("items", []))
    return out


_DROP_KEYS = {
    "default", "enum", "format", "minLength", "maxLength", "minimum", "maximum",
    "additionalProperties", "minItems", "maxItems",
    # awslabs MCP 서버는 pydantic 의 JSON Schema 풀스펙을 쓰는데 Gateway 가 거부:
    "title", "oneOf", "allOf",
    "examples", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "pattern", "patternProperties", "uniqueItems", "const",
    "readOnly", "writeOnly", "deprecated",
}


# AgentCore inputSchema 의 valid type 집합. anyOf 풀어 평탄화할 때 필요.
_VALID_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _resolve_refs(node: Any, defs: dict) -> Any:
    """$ref 를 $defs 의 실제 정의로 inline 치환. $defs / $ref 키 자체는 제거.

    pydantic 이 list[Dimension] → items: {$ref: '#/$defs/Dimension'} + $defs 로
    표현하는 형태를 풀어준다.
    """
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            # '#/$defs/<Name>' 형식만 지원
            if ref.startswith("#/$defs/"):
                name = ref[len("#/$defs/"):]
                resolved = defs.get(name) or {}
                # ref 외 다른 키가 같이 있으면 merge
                merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
                return _resolve_refs(merged, defs)
            return {k: v for k, v in node.items() if k != "$ref"}
        return {k: _resolve_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve_refs(x, defs) for x in node]
    return node


def _flatten_anyof(node: dict) -> dict:
    """pydantic 이 Optional[T] 를 'anyOf: [T, null]' 로 표현하는데 Gateway 는 anyOf 거부.
    가장 첫 번째 non-null type 만 남기고 평탄화.
    """
    if "anyOf" in node and isinstance(node["anyOf"], list):
        for branch in node["anyOf"]:
            if isinstance(branch, dict) and branch.get("type") in _VALID_TYPES and branch.get("type") != "null":
                merged = {**branch, **{k: v for k, v in node.items() if k != "anyOf"}}
                return merged
        # fallback — 첫 번째 branch
        first = node["anyOf"][0] if node["anyOf"] else {}
        merged = {**(first if isinstance(first, dict) else {}),
                  **{k: v for k, v in node.items() if k != "anyOf"}}
        return merged
    return node


def _sanitize_inner(node: Any) -> Any:
    if isinstance(node, dict):
        node = _flatten_anyof(node)
        out: dict = {}
        for k, v in node.items():
            if k in _DROP_KEYS:
                continue
            out[k] = _sanitize_inner(v)
        # type 이 빠진 properties 의 leaf 는 default type 부여 (Gateway 가 type 강제)
        if "properties" in out and isinstance(out["properties"], dict):
            for pname, pdef in out["properties"].items():
                if isinstance(pdef, dict) and "type" not in pdef:
                    if "properties" in pdef:
                        pdef["type"] = "object"
                    elif "items" in pdef:
                        pdef["type"] = "array"
                    else:
                        pdef["type"] = "string"
        # items 가 dict 인데 type 빠지면 'object' 부여
        if "items" in out and isinstance(out["items"], dict) and "type" not in out["items"]:
            if "properties" in out["items"]:
                out["items"]["type"] = "object"
            else:
                out["items"]["type"] = "string"
        return out
    if isinstance(node, list):
        return [_sanitize_inner(x) for x in node]
    return node


def _sanitize_schema(node: Any) -> Any:
    """1) $defs / $ref 를 inline 치환  2) Gateway 가 거부하는 키 제거 + anyOf 평탄화."""
    defs: dict = {}
    if isinstance(node, dict) and isinstance(node.get("$defs"), dict):
        defs = node["$defs"]
    resolved = _resolve_refs(node, defs)
    return _sanitize_inner(resolved)


def schema_to_tool_def(spec: dict) -> dict:
    """tool_io.json 을 AgentCore inlinePayload tool 정의로 변환.

    `input_schema` (snake_case, 우리 양식) 와 `inputSchema` (camelCase,
    MCP `tools/list` 응답) 둘 다 지원.
    """
    in_schema  = spec.get("input_schema") or spec.get("inputSchema") or {"type": "object"}
    out_schema = spec.get("output_schema") or spec.get("outputSchema") or {"type": "object"}
    return {
        "name": spec["name"],
        "description": spec.get("description", spec["name"]),
        "inputSchema":  _sanitize_schema(in_schema),
        "outputSchema": _sanitize_schema(out_schema),
    }


def upsert_target(
    client,
    gateway_id: str,
    target_name: str,
    lambda_arn: str,
    tools: list[dict],
):
    cfg = {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {"inlinePayload": tools},
            }
        }
    }
    existing = next(
        (t for t in list_targets(client, gateway_id) if t.get("name") == target_name),
        None,
    )
    if existing:
        tid = existing["targetId"]
        logger.info("updating target %s (%s)", target_name, tid)
        return client.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=tid,
            name=target_name,
            targetConfiguration=cfg,
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
    logger.info("creating target %s", target_name)
    return client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        targetConfiguration=cfg,
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )


def upsert_runtime(client, role_arn: str, ecr_uri: str, gateway_endpoint: str) -> dict | None:
    image_uri = f"{ecr_uri}:latest"
    cfg = {"containerConfiguration": {"containerUri": image_uri}}

    paginator = client.get_paginator("list_agent_runtimes")
    existing = None
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == RUNTIME_NAME:
                existing = rt
                break
        if existing:
            break

    env_vars = {
        "BEDROCK_REGION": REGION,
        "BEDROCK_MODEL_ID": "claude-opus-4-7",
        "GATEWAY_ENDPOINT": gateway_endpoint,
        "TOOL_BUDGET": "32",
    }

    if existing:
        rid = existing["agentRuntimeId"]
        logger.info("updating agent runtime %s", rid)
        return client.update_agent_runtime(
            agentRuntimeId=rid,
            description="DBAOps PoC agent runtime",
            roleArn=role_arn,
            agentRuntimeArtifact=cfg,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env_vars,
        )
    logger.info("creating agent runtime %s", RUNTIME_NAME)
    return client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="DBAOps PoC agent runtime",
        roleArn=role_arn,
        agentRuntimeArtifact=cfg,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=env_vars,
    )


_TOOL_TARGETS = [
    # (target_name, tool_io.json 경로, terraform output key for lambda arn)
    # 우리 PoC 특화 (직접 작성)
    ("rds-pi",               "rds_pi/tool_io.json",               "rds-pi"),
    ("msk-metrics",          "msk_metrics/tool_io.json",          "msk-metrics"),
    ("s3-log-fetch",         "s3_log_fetch/tool_io.json",         "s3-log-fetch"),
    ("aws-api",              "aws_api/tool_io.json",              "aws-api"),
    # 기성 MCP 서버 wrap (awslabs)
    ("awslabs-cloudwatch",   "awslabs_cloudwatch/tool_io.json",   "awslabs-cloudwatch"),
    ("awslabs-aws-doc",      "awslabs_aws_doc/tool_io.json",      "awslabs-aws-doc"),
    ("awslabs-aws-api",      "awslabs_aws_api/tool_io.json",      "awslabs-aws-api"),
    # 기성 MCP 서버 wrap (community)
    ("community-prometheus", "community_prometheus/tool_io.json", "community-prometheus"),
    ("community-postgres",   "community_postgres/tool_io.json",   "community-postgres"),
    ("community-mysql",      "community_mysql/tool_io.json",      "community-mysql"),
]


# 폐기된 target — 등록 시 자동 삭제
_DEPRECATED_TARGETS = ("prometheus-query", "cloudwatch-metrics", "sql-readonly")


def load_tool_specs(spec_path: Path) -> list[dict]:
    """tool_io.json 파일을 읽어 tool 정의 리스트를 반환.
    파일 형식 두 가지를 지원:
      1) 단일 도구: {"name": ..., "input_schema": ..., "output_schema": ...}
      2) 다중 도구: {"tools": [{...}, {...}]}
    """
    raw = json.loads(spec_path.read_text())
    if isinstance(raw, dict) and isinstance(raw.get("tools"), list):
        return [schema_to_tool_def(t) for t in raw["tools"]]
    if isinstance(raw, dict) and "name" in raw:
        return [schema_to_tool_def(raw)]
    raise ValueError(f"unsupported tool_io.json format: {spec_path}")


def get_cognito_client_secret(user_pool_id: str, client_id: str) -> str:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    desc = cognito.describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
    return desc["UserPoolClient"]["ClientSecret"]


def cognito_token_url(domain: str) -> str:
    return f"https://{domain}.auth.{REGION}.amazoncognito.com/oauth2/token"


def upsert_runtime_with_auth(
    client,
    role_arn: str,
    ecr_uri: str,
    gateway_endpoint: str,
    cognito_token_url_value: str,
    cognito_client_id: str,
    cognito_client_secret: str,
    log_bucket: str = "",
    prom_endpoint: str = "",
    prom_instance_id: str = "",
) -> dict | None:
    image_uri = f"{ecr_uri}:latest"
    cfg = {"containerConfiguration": {"containerUri": image_uri}}

    paginator = client.get_paginator("list_agent_runtimes")
    existing = None
    for page in paginator.paginate():
        for rt in page.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == RUNTIME_NAME:
                existing = rt
                break
        if existing:
            break

    env_vars = {
        "BEDROCK_REGION":        REGION,
        "BEDROCK_MODEL_ID":      "global.anthropic.claude-opus-4-7",
        "GATEWAY_ENDPOINT":      gateway_endpoint,
        "TOOL_BUDGET":           "128",
        "DBAOPS_IGNORE_BUDGET":  "1",
        "LOG_LEVEL":             "INFO",
        "COGNITO_TOKEN_URL":     cognito_token_url_value,
        "COGNITO_CLIENT_ID":     cognito_client_id,
        "COGNITO_CLIENT_SECRET": cognito_client_secret,
        "COGNITO_SCOPE":         "dbaops-gateway/invoke",
        # 인프라 컨텍스트 — os/db plan LLM 이 정확한 CW dimensions 를 만들 때 사용
        "INFRA_PROM_INSTANCE_ID":  prom_instance_id or os.environ.get("INFRA_PROM_INSTANCE_ID", ""),
        "INFRA_AURORA_CLUSTER_ID": "dbaops-poc-aurora-pg",
        "INFRA_AURORA_WRITER_ID":  "dbaops-poc-aurora-pg-writer",
        "INFRA_AURORA_READER_ID":  "dbaops-poc-aurora-pg-reader",
        "INFRA_MYSQL_DB_ID":       "dbaops-poc-mysql",
        "INFRA_MSK_CLUSTER_NAME":  "dbaops-poc",
        "INFRA_LOG_BUCKET":        log_bucket or os.environ.get("INFRA_LOG_BUCKET", ""),
    }

    if existing:
        rid = existing["agentRuntimeId"]
        logger.info("updating agent runtime %s", rid)
        return client.update_agent_runtime(
            agentRuntimeId=rid,
            description="DBAOps PoC agent runtime",
            roleArn=role_arn,
            agentRuntimeArtifact=cfg,
            networkConfiguration={"networkMode": "PUBLIC"},
            environmentVariables=env_vars,
        )
    logger.info("creating agent runtime %s", RUNTIME_NAME)
    return client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="DBAOps PoC agent runtime",
        roleArn=role_arn,
        agentRuntimeArtifact=cfg,
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=env_vars,
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-runtime", action="store_true", help="agent runtime 등록은 건너뜀 (이미지 push 전)")
    p.add_argument("--skip-targets", action="store_true", help="Lambda target 등록은 건너뜀 (Lambda 미배포 시)")
    args = p.parse_args(argv)

    outputs = tf_output()
    user_pool_id = outputs["cognito_user_pool_id"]
    app_client_id = outputs["cognito_app_client_id"]
    gateway_role = outputs["agentcore_gateway_role_arn"]
    runtime_role = outputs["agentcore_runtime_role_arn"]
    ecr_uri = outputs["ecr_repository_url"]
    lambda_arns = outputs.get("mcp_lambda_arns", {}) or {}

    domain = ensure_cognito_domain(user_pool_id)

    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    gw = upsert_gateway(ac, gateway_role, user_pool_id, app_client_id)
    gw_id = gw.get("gatewayId") or gw["gatewayIdentifier"]
    gw_url = gw.get("gatewayUrl") or gw.get("mcpEndpoint") or ""
    logger.info("gateway id=%s url=%s", gw_id, gw_url)

    if not args.skip_targets:
        # 폐기된 target 자동 삭제 (prometheus-query / cloudwatch-metrics / sql-readonly).
        existing_targets = list_targets(ac, gw_id)
        for t in existing_targets:
            if t.get("name") in _DEPRECATED_TARGETS:
                tid = t.get("targetId")
                logger.info("deleting deprecated target %s (%s)", t["name"], tid)
                try:
                    ac.delete_gateway_target(gatewayIdentifier=gw_id, targetId=tid)
                except ClientError as e:
                    logger.warning("delete_gateway_target failed: %s", e)

        for target_name, tool_io_path, lambda_key in _TOOL_TARGETS:
            spec_path = TOOLS_DIR / tool_io_path
            arn = lambda_arns.get(lambda_key)
            if not arn:
                logger.warning("lambda for %s not in tf outputs (mcp_lambda_arns) — skipping", target_name)
                continue
            tools = load_tool_specs(spec_path)
            upsert_target(
                ac,
                gw_id,
                target_name=target_name,
                lambda_arn=arn,
                tools=tools,
            )

    if args.skip_runtime:
        logger.info("--skip-runtime 지정 — agent runtime 등록 생략")
    else:
        try:
            client_secret = get_cognito_client_secret(user_pool_id, app_client_id)
            upsert_runtime_with_auth(
                ac,
                runtime_role,
                ecr_uri,
                gw_url,
                cognito_token_url(domain),
                app_client_id,
                client_secret,
                log_bucket=outputs.get("logs_bucket", "") or "",
                prom_endpoint=outputs.get("prometheus_endpoint", "") or "",
                prom_instance_id=outputs.get("prometheus_instance_id", "") or "",
            )
        except ClientError as e:
            logger.error("agent runtime upsert failed: %s", e)
            return 2

    print(json.dumps({"gateway_id": gw_id, "gateway_url": gw_url}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
