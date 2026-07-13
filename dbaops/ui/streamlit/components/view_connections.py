"""🔌 MCP 연결설정 탭 — 올인원 EC2 에서 각 MCP 도구의 연결 정보를 편집.

connections.json(라우터와 공유, 기본 /data/connections.json)을 read/write 하고,
mcp-router 의 /healthz?tool=<target> 로 연결 테스트를 수행한다.

핵심 UX:
- 페이지 진입 시 instance role 로 AWS 리소스(RDS/EC2/S3/MSK/Secret)를 자동 탐색해
  거의 모든 입력을 **드롭박스**로 제공. 권한이 없으면 조용히 직접입력으로 fallback.
- 도구를 카테고리(DB/메트릭/로그/AWS)로 그룹화. 상단에 상태 대시보드.
"""

from __future__ import annotations

import json
import os
import urllib.request

import streamlit as st

CONNECTIONS_PATH = os.environ.get(
    "DBAOPS_CONNECTIONS_PATH",
    os.path.join(os.environ.get("DBAOPS_DATA_DIR", "/data"), "connections.json"),
)
ROUTER_HEALTH_URL = os.environ.get("MCP_ROUTER_HEALTH_URL", "http://mcp-router:9000/healthz")

ALL_TARGETS = [
    "rds-pi", "msk-metrics", "s3-log-fetch", "aws-api",
    "community-postgres", "community-mysql", "community-prometheus",
    "awslabs-cloudwatch", "awslabs-aws-doc", "awslabs-aws-api",
]

# 카테고리별 그룹 (UI 그룹화)
_CATEGORIES = [
    ("🗄️ 데이터베이스 분석", ["community-postgres", "community-mysql", "rds-pi"]),
    ("📊 인프라 메트릭",      ["community-prometheus", "awslabs-cloudwatch", "msk-metrics"]),
    ("📜 로그 분석",          ["s3-log-fetch"]),
    ("☁️ AWS 범용 도구",      ["aws-api", "awslabs-aws-api", "awslabs-aws-doc"]),
]

_TARGET_META: dict[str, dict] = {
    "community-prometheus": {"label": "Prometheus (self-hosted)",
                             "desc": "node_exporter 등 호스트 메트릭. PromQL 쿼리."},
    "community-postgres":   {"label": "PostgreSQL / Aurora PG",
                             "desc": "read-only SQL / EXPLAIN / health 분석."},
    "community-mysql":      {"label": "MySQL / RDS MySQL",
                             "desc": "read-only SELECT / EXPLAIN / slow_log."},
    "awslabs-cloudwatch":   {"label": "CloudWatch",
                             "desc": "메트릭 / 알람 / Logs Insights. instance role 사용."},
    "awslabs-aws-doc":      {"label": "AWS Documentation",
                             "desc": "AWS 공식 문서 검색·조회 (외부)."},
    "awslabs-aws-api":      {"label": "AWS API CLI (read-only)",
                             "desc": "임의 read-only AWS CLI 명령."},
    "rds-pi":               {"label": "RDS Performance Insights",
                             "desc": "top SQL by AAS. instance role 의 pi:* 사용."},
    "msk-metrics":          {"label": "MSK / Kafka 메트릭",
                             "desc": "AWS/Kafka CloudWatch 메트릭."},
    "s3-log-fetch":         {"label": "S3 로그 조회",
                             "desc": "S3 gzip 로그 byte-range + regex."},
    "aws-api":              {"label": "AWS API 묶음 (read-only)",
                             "desc": "RDS/EC2/MSK describe + PI dimension."},
}

# 추가 설정이 필요 없는(instance role 만으로 동작) 도구
_NO_CONFIG = {"awslabs-cloudwatch", "awslabs-aws-doc", "awslabs-aws-api",
              "rds-pi", "s3-log-fetch", "aws-api"}

_INFRA_FIELDS = [
    ("aurora_cluster_id", "Aurora/PG 클러스터 ID", "clusters"),
    ("aurora_writer_id",  "Aurora/PG writer 인스턴스 ID", "instances"),
    ("aurora_reader_id",  "Aurora/PG reader 인스턴스 ID", "instances"),
    ("mysql_db_id",       "MySQL 인스턴스 ID", "instances"),
    ("prom_instance_id",  "Prometheus 호스트 EC2 instance-id", "ec2_ids"),
    ("msk_cluster_name",  "MSK 클러스터 이름", "msk"),
    ("log_bucket",        "로그 S3 버킷명", "s3_buckets"),
]

_REGIONS = [
    "ap-northeast-2", "ap-northeast-1", "ap-southeast-1", "ap-southeast-2", "ap-south-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    "eu-west-1", "eu-west-2", "eu-central-1",
]

_BEDROCK_MODELS = [
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-7",
    "us.anthropic.claude-sonnet-4-6",
    "apac.anthropic.claude-sonnet-4-6",
]

_SSLMODES = ["require", "prefer", "disable", "verify-ca", "verify-full"]


# ─────────────────────────── 저장소 / 라우터 ───────────────────────────

def _load() -> dict:
    try:
        with open(CONNECTIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONNECTIONS_PATH) or ".", exist_ok=True)
    tmp = CONNECTIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONNECTIONS_PATH)


def _health(target: str | None = None, verify: bool = False) -> dict:
    """라우터 healthz 조회.

    verify=False: stdio 세션이 떴는지(tools/list)만 — 빠름. 상단 '연결 상태 확인'용.
    verify=True : DB/Prometheus 는 실제 probe 쿼리로 진짜 연결을 확인 — '연결 테스트' 버튼용.
                  (MCP 서버는 DB 없이도 tools/list 가 되므로 verify 없이는 오설정을 못 잡음)
    """
    params = []
    if target:
        params.append(f"tool={target}")
    if verify:
        params.append("verify=1")
    url = ROUTER_HEALTH_URL + ("?" + "&".join(params) if params else "")
    try:
        with urllib.request.urlopen(url, timeout=50) as resp:
            return json.loads(resp.read()).get("targets", {})
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


# ─────────────────── AWS 리소스 자동 탐색 (instance role) ───────────────────
# 각 sub-discover 는 독립 try/except — 권한 없으면 해당 항목만 빈 목록, 전체는 계속.

def _discover_aws(region: str) -> dict:
    out: dict = {
        "pg": [], "mysql": [], "clusters": [], "instances": [],
        "ec2": [], "s3_buckets": [], "msk": [], "errors": {},
    }
    try:
        import boto3
    except Exception as e:  # noqa: BLE001
        out["errors"]["boto3"] = str(e)
        return out

    # RDS 인스턴스
    try:
        rds = boto3.client("rds", region_name=region)
        for page in rds.get_paginator("describe_db_instances").paginate():
            for db in page.get("DBInstances", []):
                engine = (db.get("Engine") or "").lower()
                ep = db.get("Endpoint") or {}
                host, dbid = ep.get("Address"), db.get("DBInstanceIdentifier")
                if dbid:
                    out["instances"].append(dbid)
                if not host:
                    continue
                item = {"db_id": dbid, "host": host, "port": str(ep.get("Port") or ""),
                        "engine": engine, "version": db.get("EngineVersion"),
                        "label": f"{dbid}  ·  {engine} {db.get('EngineVersion') or ''}".strip()}
                if "postgres" in engine:
                    out["pg"].append(item)
                elif "mysql" in engine:
                    out["mysql"].append(item)
    except Exception as e:  # noqa: BLE001
        out["errors"]["rds_instances"] = str(e)

    # RDS 클러스터 (Aurora writer/reader 엔드포인트)
    try:
        rds = boto3.client("rds", region_name=region)
        for page in rds.get_paginator("describe_db_clusters").paginate():
            for c in page.get("DBClusters", []):
                engine, cid = (c.get("Engine") or "").lower(), c.get("DBClusterIdentifier")
                if cid:
                    out["clusters"].append(cid)
                for role, host in (("writer", c.get("Endpoint")), ("reader", c.get("ReaderEndpoint"))):
                    if not host:
                        continue
                    item = {"db_id": cid, "host": host, "port": str(c.get("Port") or ""),
                            "engine": engine, "version": c.get("EngineVersion"),
                            "label": f"{cid} [{role}]  ·  {engine}"}
                    if "postgres" in engine:
                        out["pg"].append(item)
                    elif "mysql" in engine:
                        out["mysql"].append(item)
    except Exception as e:  # noqa: BLE001
        out["errors"]["rds_clusters"] = str(e)

    # EC2 (Prometheus 호스트 후보)
    try:
        ec2 = boto3.client("ec2", region_name=region)
        for page in ec2.get_paginator("describe_instances").paginate():
            for r in page.get("Reservations", []):
                for inst in r.get("Instances", []):
                    if (inst.get("State") or {}).get("Name") != "running":
                        continue
                    tags = {t["Key"]: t["Value"] for t in (inst.get("Tags") or [])}
                    iid, ip = inst.get("InstanceId"), inst.get("PrivateIpAddress")
                    name = tags.get("Name", "")
                    out["ec2"].append({"id": iid, "ip": ip, "name": name,
                                       "label": f"{name or iid}  ·  {ip}"})
    except Exception as e:  # noqa: BLE001
        out["errors"]["ec2"] = str(e)

    # S3 버킷
    try:
        s3 = boto3.client("s3", region_name=region)
        for b in s3.list_buckets().get("Buckets", []):
            out["s3_buckets"].append(b["Name"])
    except Exception as e:  # noqa: BLE001
        out["errors"]["s3"] = str(e)

    # MSK 클러스터
    try:
        kafka = boto3.client("kafka", region_name=region)
        try:
            pages = kafka.get_paginator("list_clusters_v2").paginate()
        except Exception:  # noqa: BLE001
            pages = [kafka.list_clusters_v2()]
        for page in pages:
            for c in page.get("ClusterInfoList", []):
                if c.get("ClusterName"):
                    out["msk"].append(c["ClusterName"])
    except Exception as e:  # noqa: BLE001
        out["errors"]["msk"] = str(e)

    return out


def _bedrock_models(region: str) -> list[str]:
    """정적 목록 + (가능하면) list_inference_profiles 동적 보강."""
    models = list(_BEDROCK_MODELS)
    try:
        import boto3
        b = boto3.client("bedrock", region_name=region)
        for p in b.get_paginator("list_inference_profiles").paginate():
            for ip in p.get("inferenceProfileSummaries", []):
                pid = ip.get("inferenceProfileId")
                if pid and ("opus" in pid or "sonnet" in pid) and pid not in models:
                    models.append(pid)
    except Exception:  # noqa: BLE001
        pass
    return models


# ─────────────────────────── 위젯 헬퍼 ───────────────────────────

def _select_or_text(label: str, options: list[str], current: str, key: str,
                    help: str | None = None, placeholder: str = "") -> str:
    """탐색된 options 가 있으면 드롭박스(+직접입력), 없으면 text_input.

    options 의 항목은 value 와 동일(문자열). host 처럼 label≠value 인 경우는 _instance_picker.
    """
    options = [o for o in options if o]
    if not options:
        return st.text_input(label, value=current, key=key, help=help, placeholder=placeholder)

    DIRECT = "✏️ 직접 입력"
    NONE = "— 선택 안 함 —"
    choices = [NONE, DIRECT] + options
    if current and current in options:
        idx = choices.index(current)
    elif current:
        idx = 1  # 직접 입력 모드
    else:
        idx = 0
    sel = st.selectbox(label, choices, index=idx, key=f"{key}__sel", help=help)
    if sel == DIRECT:
        return st.text_input(f"↳ {label} 직접 입력", value=current, key=f"{key}__txt",
                             placeholder=placeholder, label_visibility="collapsed")
    if sel == NONE:
        return ""
    return sel


def _instance_picker(label: str, instances: list[dict], key: str) -> dict | None:
    """RDS 인스턴스 드롭박스 → 선택된 {host, port, db_id, engine} 또는 None."""
    if not instances:
        return None
    labels = ["— 직접 입력 —"] + [o["label"] for o in instances]
    sel = st.selectbox(label, labels, key=key)
    if sel == "— 직접 입력 —":
        return None
    return next((o for o in instances if o["label"] == sel), None)


def _badge(hstat: dict) -> str:
    if not hstat:
        return ""
    if hstat.get("ok") is True:
        return f"🟢 {hstat.get('tools', 0)} tools"
    if hstat.get("ok") is False:
        return "🔴 연결 실패"
    return ""


def _text_with_prefill(label: str, value: str, base_key: str, prefilled: bool,
                       kind: str = "text", placeholder: str = "") -> str:
    """prefill 된 값은 위젯 key 에 값 해시를 섞어 강제 리렌더(드롭박스 선택 즉시 반영)."""
    wkey = base_key + (f"__{hash(str(value)) & 0xffff}" if prefilled else "")
    if kind == "password":
        return st.text_input(label, value=value, type="password", key=wkey, placeholder=placeholder)
    return st.text_input(label, value=value, key=wkey, placeholder=placeholder)


# ─────────────────────────── 도구 카드 ───────────────────────────

def _render_tool_card(target: str, cfg: dict, disc: dict, secrets: list[str],
                      health: dict) -> dict:
    meta = _TARGET_META[target]
    cur = cfg["tools"].get(target, {})
    hstat = health.get(target, {}) if isinstance(health, dict) else {}
    needs_config = target not in _NO_CONFIG
    # 디폴트는 "사용"(ON). 저장된 값이 있으면 그 값을 따른다.
    default_on = cur.get("enabled", True)
    badge = _badge(hstat)
    title = f"{'🟢' if default_on else '⚪'} {meta['label']}   ·   `{target}`" + (f"   {badge}" if badge else "")

    # 설정이 필요한 도구(DB/Prometheus/MSK)만 펼치고, instance-role 도구는 접어둔다.
    with st.expander(title, expanded=bool(default_on) and needs_config):
        st.caption(meta["desc"])
        enabled = st.toggle("이 도구 사용", value=default_on, key=f"en__{target}")
        conf: dict = {"enabled": enabled}

        if not enabled:
            st.caption("⏸️ 비활성 — 켜면 설정 항목이 나타납니다.")
            return conf

        # ── DB: PostgreSQL / MySQL ──
        if target in ("community-postgres", "community-mysql"):
            is_pg = target == "community-postgres"
            opts = disc.get("pg" if is_pg else "mysql", [])
            HK = "PG_HOST" if is_pg else "MYSQL_HOST"
            PK = "PG_PORT" if is_pg else "MYSQL_PORT"
            DK = "PG_DBNAME" if is_pg else "MYSQL_DB"
            UK = "PG_USER" if is_pg else "MYSQL_USER"
            PWK = "PG_PASSWORD" if is_pg else "MYSQL_PASSWORD"
            SK = "PG_SECRET_ARN" if is_pg else "MYSQL_SECRET_ARN"

            picked = _instance_picker("① RDS 인스턴스 선택 (자동 탐색)", opts, key=f"pick__{target}")
            host_val = picked["host"] if picked else cur.get(HK, "")
            port_val = picked["port"] if picked else cur.get(PK, str(5432 if is_pg else 3306))
            if picked:
                st.success(f"✅ {picked['db_id']} → `{picked['host']}:{picked['port']}`")

            c1, c2 = st.columns([3, 1])
            with c1:
                conf[HK] = _text_with_prefill("② Host", host_val, f"h__{target}", bool(picked),
                                              placeholder="xxx.rds.amazonaws.com")
            with c2:
                conf[PK] = _text_with_prefill("Port", str(port_val), f"p__{target}", bool(picked))
            conf[DK] = st.text_input("③ Database", value=cur.get(DK, "postgres" if is_pg else "mysql"),
                                     key=f"db__{target}")

            st.markdown("**④ 인증 방식**")
            has_secret = bool(cur.get(SK))
            mode = st.radio("auth", ["🔑 Secrets Manager", "👤 User / Password 직접"],
                            index=0 if (has_secret or secrets) else 1,
                            horizontal=True, label_visibility="collapsed", key=f"auth__{target}")
            if mode.startswith("🔑"):
                conf[SK] = _select_or_text("Secret (ARN/이름)", secrets, cur.get(SK, ""),
                                           key=f"sec__{target}",
                                           placeholder="arn:aws:secretsmanager:...:secret:DB...")
            else:
                ac1, ac2 = st.columns(2)
                with ac1:
                    conf[UK] = st.text_input("User", value=cur.get(UK, ""), key=f"u__{target}")
                with ac2:
                    conf[PWK] = st.text_input("Password", value=cur.get(PWK, ""), type="password",
                                              key=f"pw__{target}")

            if is_pg:
                cur_ssl = cur.get("PG_SSLMODE", "require")
                conf["PG_SSLMODE"] = st.selectbox(
                    "⑤ SSL mode", _SSLMODES,
                    index=_SSLMODES.index(cur_ssl) if cur_ssl in _SSLMODES else 0,
                    key=f"ssl__{target}")

        # ── Prometheus ──
        elif target == "community-prometheus":
            ec2 = disc.get("ec2", [])
            url_val = cur.get("PROMETHEUS_URL", "")
            if ec2:
                labels = ["— 직접 입력 —"] + [f"{o['label']}  → :9090" for o in ec2]
                sel = st.selectbox("Prometheus 호스트 (EC2 자동 탐색)", labels, key=f"prom_ec2__{target}")
                if sel != "— 직접 입력 —":
                    chosen = ec2[labels.index(sel) - 1]
                    url_val = f"http://{chosen['ip']}:9090"
                    st.success(f"✅ {chosen['label']} → `{url_val}`")
                    conf["PROMETHEUS_URL"] = _text_with_prefill(
                        "Prometheus URL", url_val, f"prurl__{target}", True)
                else:
                    conf["PROMETHEUS_URL"] = st.text_input(
                        "Prometheus URL", value=url_val, key=f"prurl2__{target}",
                        placeholder="http://10.0.1.5:9090")
            else:
                conf["PROMETHEUS_URL"] = st.text_input(
                    "Prometheus URL", value=url_val, key=f"prurl3__{target}",
                    placeholder="http://10.0.1.5:9090")

        # ── MSK metrics ──
        elif target == "msk-metrics":
            conf["KAFKA_CLUSTER_NAME"] = _select_or_text(
                "MSK Cluster Name", disc.get("msk", []), cur.get("KAFKA_CLUSTER_NAME", ""),
                key=f"msk__{target}")
            mc1, mc2 = st.columns(2)
            with mc1:
                conf["KAFKA_DEFAULT_TOPIC"] = st.text_input(
                    "기본 Topic", value=cur.get("KAFKA_DEFAULT_TOPIC", ""), key=f"topic__{target}")
            with mc2:
                conf["KAFKA_DEFAULT_CG"] = st.text_input(
                    "기본 Consumer Group", value=cur.get("KAFKA_DEFAULT_CG", ""), key=f"cg__{target}")

        # ── 추가 설정 불필요 ──
        elif target in _NO_CONFIG:
            st.caption("➕ 추가 연결 정보 불필요 — EC2 instance role 권한으로 동작합니다.")

        # 빈 문자열 필드 제거
        conf = {k: v for k, v in conf.items() if k == "enabled" or v}

        # 카드별 연결 테스트
        if st.button("🔌 연결 테스트", key=f"test__{target}"):
            cfg["tools"][target] = conf
            _save(cfg)
            with st.spinner("실제 연결 확인 중…"):
                res = _health(target, verify=True)  # 실제 probe 쿼리로 진짜 접속 확인
            st.session_state["_mcp_health"] = {**health, **(res if isinstance(res, dict) else {})}
            one = res.get(target, {}) if isinstance(res, dict) else {}
            if one.get("ok"):
                tail = " · 실접속 확인" if one.get("verified") else ""
                st.success(f"✅ 연결 성공 — {one.get('tools', 0)} tools{tail}")
            else:
                st.error(f"❌ {one.get('error', res.get('_error', 'unknown'))}")

    return conf


# ─────────────────────────── 메인 ───────────────────────────

def render() -> None:
    cfg = _load()
    cfg.setdefault("aws_region", os.environ.get("AWS_REGION", "ap-northeast-2"))
    cfg.setdefault("bedrock_model_id",
                   os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-4-7"))
    cfg.setdefault("tools", {})
    cfg.setdefault("infra_context", {})

    st.markdown("### 🔌 MCP 연결 설정")
    st.caption("분석 도구가 붙을 대상(DB·Prometheus·AWS)을 설정합니다. "
               "AWS 리소스는 자동 탐색되어 드롭박스로 선택할 수 있습니다.")

    # 자동 탐색 (1회 캐싱)
    if "_aws_disc" not in st.session_state:
        with st.spinner("AWS 리소스 자동 탐색 중…"):
            st.session_state["_aws_disc"] = _discover_aws(cfg["aws_region"])
    if "_bedrock_models" not in st.session_state:
        st.session_state["_bedrock_models"] = _bedrock_models(cfg["aws_region"])
    disc = st.session_state["_aws_disc"]
    health = st.session_state.get("_mcp_health", {})
    secrets = st.session_state.get("_secrets_list", [])

    # ── 상태 대시보드 ──
    enabled_n = sum(1 for t in ALL_TARGETS if cfg["tools"].get(t, {}).get("enabled"))
    ok_n = sum(1 for t, s in health.items()
               if isinstance(s, dict) and s.get("ok") is True) if isinstance(health, dict) else 0
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("활성 도구", f"{enabled_n} / {len(ALL_TARGETS)}")
    d2.metric("연결 OK", ok_n if health and not health.get("_error") else "—")
    d3.metric("탐색된 DB", f"{len(disc.get('pg', [])) + len(disc.get('mysql', []))}")
    d4.metric("리전", cfg["aws_region"])

    # ── 상단 액션바 ──
    a1, a2, a3 = st.columns(3)
    if a1.button("🔄 연결 상태 확인", use_container_width=True):
        st.session_state["_mcp_health"] = _health()
        st.rerun()
    if a2.button("🔍 AWS 리소스 다시 탐색", use_container_width=True):
        st.session_state["_aws_disc"] = _discover_aws(cfg["aws_region"])
        st.session_state.pop("_bedrock_models", None)
        st.rerun()
    if a3.button("🔑 Secret 목록 불러오기", use_container_width=True,
                 help="Secrets Manager 의 secret 을 자격증명 드롭박스로"):
        try:
            import boto3
            sm = boto3.client("secretsmanager", region_name=cfg["aws_region"])
            names = []
            for page in sm.get_paginator("list_secrets").paginate():
                names += [s.get("ARN") or s.get("Name") for s in page.get("SecretList", [])]
            st.session_state["_secrets_list"] = [n for n in names if n]
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.warning(f"Secret 목록 실패(권한 확인): {e}")

    if isinstance(health, dict) and health.get("_error"):
        st.warning(f"라우터 상태 조회 실패: {health['_error']}")
    if disc.get("errors"):
        with st.expander(f"⚠️ 일부 탐색 권한 없음 ({len(disc['errors'])}) — 직접 입력으로 대체됩니다"):
            for k, v in disc["errors"].items():
                st.caption(f"`{k}`: {v}")

    st.divider()

    # ── 전역 설정 ──
    with st.container(border=True):
        st.markdown("##### ⚙️ 전역 설정")
        g1, g2 = st.columns(2)
        regions = _REGIONS if cfg["aws_region"] in _REGIONS else [cfg["aws_region"], *_REGIONS]
        cfg["aws_region"] = g1.selectbox("AWS Region", regions,
                                         index=regions.index(cfg["aws_region"]))
        models = st.session_state["_bedrock_models"]
        if cfg["bedrock_model_id"] not in models:
            models = [cfg["bedrock_model_id"], *models]
        cfg["bedrock_model_id"] = g2.selectbox("Bedrock Model", models,
                                               index=models.index(cfg["bedrock_model_id"]),
                                               help="bedrock:InvokeModel 권한 필요")

    # ── 도구 상세 (카테고리별) ──
    new_tools: dict[str, dict] = {}
    for cat_label, targets in _CATEGORIES:
        st.markdown(f"#### {cat_label}")
        for target in targets:
            new_tools[target] = _render_tool_card(target, cfg, disc, secrets, health)

    # ── 인프라 식별자 (드롭박스화) ──
    st.markdown("#### 🏷️ 인프라 식별자")
    with st.container(border=True):
        st.caption("분석 프롬프트가 참조하는 식별자. 비워두면 에이전트가 describe 로 직접 찾습니다.")
        ic = cfg["infra_context"]
        src_map = {
            "clusters": disc.get("clusters", []),
            "instances": disc.get("instances", []),
            "ec2_ids": [o["id"] for o in disc.get("ec2", []) if o.get("id")],
            "msk": disc.get("msk", []),
            "s3_buckets": disc.get("s3_buckets", []),
        }
        new_ic: dict = {}
        cols = st.columns(2)
        for i, (key, label, source) in enumerate(_INFRA_FIELDS):
            with cols[i % 2]:
                new_ic[key] = _select_or_text(label, src_map.get(source, []),
                                              ic.get(key, ""), key=f"ic__{key}")
        new_ic = {k: v for k, v in new_ic.items() if v}

    # ── 하단 저장 ──
    st.divider()
    s1, s2 = st.columns([3, 1])
    if s1.button("💾 전체 저장 후 적용", type="primary", use_container_width=True):
        cfg["tools"] = new_tools
        cfg["infra_context"] = new_ic
        _save(cfg)
        st.session_state["_mcp_health"] = _health()
        st.success("저장 완료 — 라우터가 즉시 반영합니다.")
        st.rerun()
    if s2.button("🔌 전체 연결 테스트", use_container_width=True):
        cfg["tools"] = new_tools
        _save(cfg)
        with st.spinner("모든 도구 실제 연결 확인 중…"):
            st.session_state["_mcp_health"] = _health(verify=True)  # 실제 probe 포함
        st.rerun()
