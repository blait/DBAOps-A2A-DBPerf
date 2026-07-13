############################################
# IAM module — MCP Lambda 공통 실행 역할
############################################
# PoC: 모든 MCP 도구가 이 base 역할을 공유. 권한은 도구별로 필요한 최대 합집합.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mcp_lambda_base" {
  name               = "dbaops-${var.environment}-mcp-lambda-base"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "mcp_lambda_basic" {
  role       = aws_iam_role.mcp_lambda_base.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "mcp_lambda_vpc" {
  role       = aws_iam_role.mcp_lambda_base.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# awslabs aws-api-mcp 의 call_aws 가 임의 AWS CLI read 호출 — ReadOnlyAccess 부여.
# READ_OPERATIONS_ONLY=true 로 mutation 은 핸들러 레벨에서 한 번 더 차단.
resource "aws_iam_role_policy_attachment" "mcp_lambda_readonly" {
  role       = aws_iam_role.mcp_lambda_base.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "mcp_lambda_runtime" {
  role = aws_iam_role.mcp_lambda_base.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricData",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:DescribeAlarmHistory"
        ]
        Resource = "*"
      },
      {
        # awslabs cloudwatch-mcp 의 Logs Insights 도구 — describe / start / get / stop / list
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeQueryDefinitions",
          "logs:StartQuery",
          "logs:GetQueryResults",
          "logs:StopQuery",
          "logs:ListLogAnomalyDetectors",
          "logs:ListAnomalies",
          "logs:GetLogEvents",
          "logs:FilterLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeVolumes",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSubnets",
          "ec2:DescribeVpcs"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "rds:DescribeDBInstances",
          "rds:DescribeDBClusters",
          "rds:DescribeDBLogFiles",
          "rds:DownloadDBLogFilePortion",
          "rds:DescribeDBParameters",
          "rds:DescribeDBClusterParameters"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "pi:GetResourceMetrics",
          "pi:DescribeDimensionKeys",
          "pi:GetDimensionKeyDetails",
          "pi:GetResourceMetadata"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "kafka:GetBootstrapBrokers",
          "kafka:DescribeCluster*",
          "kafka:ListClusters*"
        ]
        Resource = "*"
      }
    ]
  })
}
