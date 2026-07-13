# MCP Tools

커스텀 MCP 도구 핸들러 4종. `mcp_router`가 직접 import 하여 호출한다.

| 디렉토리 | 도구 | 설명 |
|---|---|---|
| `rds_performance_insights/` | `rds-pi` | RDS PI top SQL by AAS, wait events |
| `msk_metrics/` | `msk-metrics` | MSK/Kafka CloudWatch 메트릭 조회 |
| `s3_log_fetch/` | `s3-log-fetch` | S3 gzip 로그 byte-range + regex (도구 2개: `s3_log_fetch`, `s3_list_logs`) |
| `aws_api/` | `aws-api` | RDS/EC2/MSK describe + PI dimension + RDS 이벤트 이력/AWS 권고사항/PI 분석 리포트 (sub-tool 11개) |

각 디렉토리에 `handler.py` + `tool_io.json`(입출력 스키마). 호출 규약은 두 갈래 — aws-api 는 `handler({"tool_name": sub, "arguments": args}, None)`, 나머지 3종(rds-pi/msk-metrics/s3-log-fetch)은 `handler({"body": args}, None)`.

이 4종 외 디렉토리(community_*/awslabs_*/cloudwatch_metrics/prometheus_query/sql_readonly 등)는 Lambda 시절 레거시로 현재 라우터가 사용하지 않음.

이 외 6종(community-postgres, community-mysql, community-prometheus, awslabs-cloudwatch, awslabs-aws-api, awslabs-aws-doc)은 오픈소스 MCP 서버를 stdio 로 spawn — `mcp_router/stdio_proxy.py` 참조.
