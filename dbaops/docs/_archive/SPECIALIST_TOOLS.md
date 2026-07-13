# Specialist · MCP · Tool Catalog

DBAOps 분석 swarm 의 6 specialist 가 사용하는 MCP 서버 / Lambda / 도구 매핑.

## 전체 구조

```
사용자 요청
   ↓
🎯 supervisor  (langgraph-supervisor)
   ↓ transfer_to_<specialist>
   ↓
specialist 6명
   ↓ tool 호출 (LangChain @tool wrapper → MCPClient → Gateway → Lambda → MCP server)
   ↓
Gateway Lambda target 10개
   ↓
(우리 PoC 직접 작성 4개) + (awslabs 기성 MCP wrap 3개) + (커뮤니티 MCP wrap 3개)
```

`agent/src/dbaops_agent/tools/mcp_tools.py` 의 `*_TOOLS` 그룹이 곧 specialist 의 도구 셋.
`agent/src/dbaops_agent/swarm_graph.py` 의 `_build_graph()` 가 specialist 6명 생성.

---

## 🎯 supervisor

라우팅 결정만 담당 — 도구 직접 호출 없음. `langgraph-supervisor.create_supervisor()` 가 자동 생성한 `transfer_to_<specialist>` 도구로 6 specialist 중 하나에게 제어권 위임.

system prompt: `_SUPERVISOR_PROMPT` (`agent/src/dbaops_agent/swarm_graph.py:218`)

---

## 🖥️ os_specialist — 호스트/인프라 메트릭

**역할**: CPU·메모리·디스크·네트워크 추세/이상치. PromQL (node_exporter) + CloudWatch (AWS/EC2, AWS/RDS).

**도구 4개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `prometheus_query(query, time?)` | `community-prometheus___execute_query` | `dbaops-poc-community-prometheus` | community: pab1it0/prometheus-mcp-server |
| `prometheus_range_query(query, start, end, step)` | `community-prometheus___execute_range_query` | `dbaops-poc-community-prometheus` | community: pab1it0/prometheus-mcp-server |
| `cloudwatch_metric(namespace, metric_name, start_time, end_time, dimensions, statistic, period)` | `awslabs-cloudwatch___get_metric_data` | `dbaops-poc-awslabs-cloudwatch` | awslabs: cloudwatch-mcp-server |
| (위 두 wrapper 가 attach 된 노드. CW 도 db_specialist 와 공유) | | | |

**자주 쓰는 PromQL**:
- CPU 사용률: `100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) * 100`
- 메모리 바이트: `node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes`
- 디스크 IO: `rate(node_disk_io_time_seconds_total[5m])`
- 네트워크 RX: `rate(node_network_receive_bytes_total[5m])`

**자주 쓰는 CloudWatch dimensions**:
- EC2: `[{"Name":"InstanceId","Value":"<prom_instance_id>"}]`
- Aurora writer: `[{"Name":"DBInstanceIdentifier","Value":"dbaops-poc-aurora-pg-writer"}]`
- MySQL: `[{"Name":"DBInstanceIdentifier","Value":"dbaops-poc-mysql"}]`

---

## 🗄️ db_specialist — DBMS / Kafka 내부

**역할**: PG `pg_stat_*`, MySQL `performance_schema` + `mysql.slow_log`, RDS Performance Insights, MSK CloudWatch.

**도구 9개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `pg_execute_sql(sql)` | `community-postgres___execute_sql` | `dbaops-poc-community-postgres` | community: crystaldba/postgres-mcp (restricted RO) |
| `pg_analyze_db_health(health_type)` | `community-postgres___analyze_db_health` | `dbaops-poc-community-postgres` | community |
| `pg_get_top_queries(sort_by, limit)` | `community-postgres___get_top_queries` | `dbaops-poc-community-postgres` | community (pg_stat_statements 기반) |
| `pg_list_schemas()` | `community-postgres___list_schemas` | `dbaops-poc-community-postgres` | community |
| `pg_list_objects(schema_name, object_type)` | `community-postgres___list_objects` | `dbaops-poc-community-postgres` | community |
| `mysql_query(sql)` | `community-mysql___mysql_query` | `dbaops-poc-community-mysql` | community: benborla/mcp-server-mysql (Node, RO default) |
| `rds_performance_insights(db_id, start, end, group_by)` | `rds-pi___rds_performance_insights` | `dbaops-poc-rds-pi` | **우리 PoC** (PI group prefix 자동 정규화) |
| `msk_metric(cluster_arn, metric, start, end, stat, topic, consumer_group, period)` | `msk-metrics___msk_metrics` | `dbaops-poc-msk-metrics` | **우리 PoC** (메트릭별 dimension 자동 wiring) |
| `cloudwatch_metric(...)` | `awslabs-cloudwatch___get_metric_data` | `dbaops-poc-awslabs-cloudwatch` | awslabs (RDS 메트릭 조회용 공유) |

**관측 인프라 default (모두 켜짐 — OFF 가정 금지)**:
- MySQL: `performance_schema=ON`, `slow_query_log=ON`, `long_query_time=0.3`, `log_output=TABLE`, `log_queries_not_using_indexes=ON`
- Aurora PG: `pg_stat_statements` 로드, `log_min_duration_statement=500`, `log_lock_waits=ON`, `auto_explain.log_min_duration=500` (`log_analyze=ON`)

**MSK 메트릭별 자동 dimension**:
- `BytesInPerSec` / `BytesOutPerSec` / `MessagesInPerSec` → Cluster Name + Topic
- `MaxOffsetLag` / `SumOffsetLag` / `EstimatedMaxTimeLag` → Cluster Name + Consumer Group + Topic
- `UnderReplicatedPartitions` → Cluster Name (broker level)

---

## 📜 log_specialist — 로그 패턴 / RCA

**역할**: S3 .gz 로그 분류 + RDS 엔진 로그 + CloudWatch Logs Insights.

**도구 6개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `s3_list_logs(bucket, prefix, since_minutes, max_keys)` | `s3-log-fetch___s3_list_logs` | `dbaops-poc-s3-log-fetch` | **우리 PoC** (listing-first 강제) |
| `s3_log_fetch(bucket, key, regex, max_lines)` | `s3-log-fetch___s3_log_fetch` | `dbaops-poc-s3-log-fetch` | **우리 PoC** (gz 자동 디코딩 + regex) |
| `aws_describe_db_log_files(db_instance_identifier, filename_contains)` | `aws-api___describe_db_log_files` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_download_db_log_file_portion(db_instance_identifier, log_file_name, lines, regex, marker)` | `aws-api___download_db_log_file_portion` | `dbaops-poc-aws-api` | **우리 PoC** (자동 페이지네이션 + regex) |
| `cloudwatch_describe_log_groups(log_group_name_prefix, max_items)` | `awslabs-cloudwatch___describe_log_groups` | `dbaops-poc-awslabs-cloudwatch` | awslabs |
| `cloudwatch_execute_log_insights_query(log_group_names, query_string, start_time, end_time, limit)` | `awslabs-cloudwatch___execute_log_insights_query` | `dbaops-poc-awslabs-cloudwatch` | awslabs |

**자주 쓰는 regex**:
- PG: `deadlock|FATAL|too many connections|still waiting`
- MySQL: `\[ERROR\]|InnoDB|Query_time`
- Kafka: `ISR|ERROR|Could not append`

**자주 쓰는 Logs Insights 쿼리**:
```
fields @timestamp, @message
| filter @message like /ERROR/
| stats count() by bin(1m)
```

---

## 🔎 query_specialist — EXPLAIN / 인덱스

**역할**: 풀스캔 / Nested Loop / Sort / Hash join / 임시 테이블 식별 + 인덱스/리라이팅 권고.

**도구 5개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `pg_explain_query(sql, hypothetical_indexes)` | `community-postgres___explain_query` | `dbaops-poc-community-postgres` | community: postgres-mcp (가상 인덱스 시뮬레이션 가능) |
| `pg_analyze_workload_indexes(max_index_size_mb)` | `community-postgres___analyze_workload_indexes` | `dbaops-poc-community-postgres` | community |
| `pg_execute_sql(sql)` | `community-postgres___execute_sql` | `dbaops-poc-community-postgres` | community (인덱스 메타 조회) |
| `mysql_explain(sql, analyze)` | `community-mysql___mysql_query` | `dbaops-poc-community-mysql` | community (EXPLAIN ANALYZE wrapper) |
| `mysql_query(sql)` | `community-mysql___mysql_query` | `dbaops-poc-community-mysql` | community (인덱스 메타 조회) |

**원칙**: SQL 텍스트가 question 에 명시 안 됐으면 **즉시 거절** → supervisor 가 db_specialist 로 다시 라우팅 (mysql.slow_log 등에서 SQL 가져옴).

---

## ☁️ aws_specialist — AWS 인프라 메타 + read-only fallback

**역할**: "지금 인프라가 어떻게 생겼는가" 답. 시계열 메트릭은 db/os 영역.

**도구 11개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `aws_describe_rds_instances(db_instance_identifier, max_records)` | `aws-api___describe_rds_instances` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_describe_rds_clusters(db_cluster_identifier)` | `aws-api___describe_rds_clusters` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_describe_db_log_files(db_instance_identifier, filename_contains)` | `aws-api___describe_db_log_files` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_download_db_log_file_portion(...)` | `aws-api___download_db_log_file_portion` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_list_msk_clusters()` | `aws-api___list_msk_clusters` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_describe_ec2_instances(instance_ids, tag_name_contains, max)` | `aws-api___describe_ec2_instances` | `dbaops-poc-aws-api` | **우리 PoC** |
| `aws_describe_pi_dimensions(dbi_resource_id, metric, group_by, start, end)` | `aws-api___describe_pi_dimensions` | `dbaops-poc-aws-api` | **우리 PoC** (group prefix 정규화) |
| `cloudwatch_get_active_alarms(max_items)` | `awslabs-cloudwatch___get_active_alarms` | `dbaops-poc-awslabs-cloudwatch` | awslabs |
| `cloudwatch_get_alarm_history(alarm_name, max_items)` | `awslabs-cloudwatch___get_alarm_history` | `dbaops-poc-awslabs-cloudwatch` | awslabs |
| `aws_call_cli(cli_command)` | `awslabs-aws-api___call_aws` | `dbaops-poc-awslabs-aws-api` | awslabs (READ_OPERATIONS_ONLY=true) |
| `aws_suggest_cli(query)` | `awslabs-aws-api___suggest_aws_commands` | `dbaops-poc-awslabs-aws-api` | awslabs |

**거절 규칙**: 시계열 추세 요청 (예: "BytesIn 추이") 이 오면 한 줄 거절 → supervisor 가 db/os 로 재라우팅.

---

## 📚 docs_specialist — AWS 공식 문서

**역할**: AWS 서비스 default / 한도 / 동작 / 용어 사실 확인.

**도구 3개**:

| LangChain wrapper | MCP server (Gateway target) | Lambda 함수 | 출처 |
|---|---|---|---|
| `aws_doc_search(search_phrase, limit)` | `awslabs-aws-doc___search_documentation` | `dbaops-poc-awslabs-aws-doc` | awslabs: aws-documentation-mcp-server |
| `aws_doc_read(url, max_length, start_index)` | `awslabs-aws-doc___read_documentation` | `dbaops-poc-awslabs-aws-doc` | awslabs |
| `aws_doc_recommend(url)` | `awslabs-aws-doc___recommend` | `dbaops-poc-awslabs-aws-doc` | awslabs |

**작업 패턴**:
1. `aws_doc_search` 로 후보 URL 1~3개 (정확한 검색어가 핵심).
2. `aws_doc_read` 로 본문 fetch (markdown).
3. 사용자 질문에 직접 답하는 부분만 인용해 짧게.

---

## Lambda · MCP server 매핑 정리

10개 Gateway Lambda target 의 출처 한눈에:

| Gateway target | Lambda 함수명 | MCP server | 우리 PoC / 기성 |
|---|---|---|---|
| `rds-pi` | `dbaops-poc-rds-pi` | (자체 boto3 wrap, single tool) | 우리 |
| `msk-metrics` | `dbaops-poc-msk-metrics` | (자체 boto3 wrap, single tool) | 우리 |
| `s3-log-fetch` | `dbaops-poc-s3-log-fetch` | (자체 boto3 wrap, 2 tools) | 우리 |
| `aws-api` | `dbaops-poc-aws-api` | (자체 boto3 dispatch, 7 tools) | 우리 |
| `awslabs-cloudwatch` | `dbaops-poc-awslabs-cloudwatch` | `awslabs.cloudwatch-mcp-server` (19 tools) | awslabs |
| `awslabs-aws-doc` | `dbaops-poc-awslabs-aws-doc` | `awslabs.aws-documentation-mcp-server` (4 tools) | awslabs |
| `awslabs-aws-api` | `dbaops-poc-awslabs-aws-api` | `awslabs.aws-api-mcp-server` (2 tools) | awslabs |
| `community-prometheus` | `dbaops-poc-community-prometheus` | `prometheus-mcp-server` pab1it0 (6 tools) | community |
| `community-postgres` | `dbaops-poc-community-postgres` | `postgres-mcp` crystaldba (9 tools) | community |
| `community-mysql` | `dbaops-poc-community-mysql` | `@benborla29/mcp-server-mysql` (1 tool) | community |

**기성 MCP server 가 stdio 인데 어떻게 Gateway 에 붙었나**: `awslabs/run-model-context-protocol-servers-with-aws-lambda` 의 `BedrockAgentCoreGatewayTargetHandler` + `StdioServerAdapterRequestHandler` 가 매 Lambda invoke 마다 stdio MCP 서버를 자식 프로세스로 spawn → JSON-RPC 변환.

---

## 환경 변수 / 인증

각 Lambda 가 받는 핵심 env (terraform 으로 자동 주입):

| Lambda | env vars | 인증 |
|---|---|---|
| rds-pi | (없음) | IAM `pi:*` |
| msk-metrics | `KAFKA_CLUSTER_NAME`, `KAFKA_DEFAULT_TOPIC`, `KAFKA_DEFAULT_CG` | IAM `cloudwatch:GetMetricData` |
| s3-log-fetch | (없음) | IAM `s3:GetObject/ListBucket` |
| aws-api | (없음) | IAM `rds:Describe*`, `ec2:Describe*`, `kafka:*`, `pi:*` |
| awslabs-cloudwatch | `AWS_REGION` | IAM `cloudwatch:*`, `logs:*` (Insights 포함) |
| awslabs-aws-doc | `AWS_DOCUMENTATION_PARTITION=aws` | 없음 (public docs) |
| awslabs-aws-api | `READ_OPERATIONS_ONLY=true`, `AWS_API_MCP_WORKING_DIR=/tmp/aws-api-mcp` | IAM `ReadOnlyAccess` |
| community-prometheus | `PROMETHEUS_URL` (terraform output) | (없음 — VPC 직통) |
| community-postgres | `PG_HOST`, `PG_DBNAME`, `PG_SECRET_ARN`, `PG_PORT` | IAM `secretsmanager:GetSecretValue` |
| community-mysql | `MYSQL_HOST`, `MYSQL_DB`, `MYSQL_SECRET_ARN`, `MYSQL_PORT` | IAM `secretsmanager:GetSecretValue` |

---

## 변경 이력

- 2026-05-15: 초기 PoC — 7 Lambda 직접 작성 (prometheus-query / cloudwatch-metrics / rds-pi / sql-readonly / msk-metrics / s3-log-fetch / aws-api).
- 2026-05-17: aws-api Lambda 신설 (9 read-only AWS API 함수). msk_metric / rds_pi / s3_log_fetch dimension·정규화 버그 수정.
- 2026-05-18: 기성 MCP 서버 도입.
  - 폐기: prometheus-query, cloudwatch-metrics, sql-readonly Lambda
  - 신규: awslabs cloudwatch-mcp / aws-documentation-mcp / aws-api-mcp + community pab1it0 prometheus / crystaldba postgres / benborla mysql
  - aws-api Lambda 의 list_cloudwatch_alarms / list_metric_namespaces 제거 (cloudwatch-mcp 가 더 풍부)
  - docs_specialist 신설 (6번째 specialist)
  - swarm_graph.py 의 supervisor 도구 카탈로그 갱신
