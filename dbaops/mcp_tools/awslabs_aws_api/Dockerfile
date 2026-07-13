FROM public.ecr.aws/lambda/python:3.12-arm64
WORKDIR /var/task

# Step 1: aws-api-mcp 의 모든 deps 설치 (awscli==1.45.6 → botocore==1.43.6 고정).
RUN pip install --no-cache-dir 'awslabs.aws-api-mcp-server>=1.3.36'

# Step 2: run-mcp-servers-with-aws-lambda 와 deps.
#   이 라이브러리의 aiobotocore>=3.4.0 가 botocore<1.43.1 강제. awscli 1.45.6 의
#   botocore==1.43.6 와 충돌하므로, aiobotocore 만 --no-deps 로 설치하고
#   나머지 deps (aws-lambda-powertools, anyio, mcp-proxy-for-aws, aiohttp 등) 는 정상 설치.
RUN pip install --no-cache-dir --no-deps 'aiobotocore>=3.4.0' \
 && pip install --no-cache-dir --no-deps 'run-mcp-servers-with-aws-lambda>=0.5.19' \
 && pip install --no-cache-dir \
        'aws-lambda-powertools>=3.26.0' \
        'mcp-proxy-for-aws>=1.2.0' \
        'anyio>=4.13.0' \
        'aiohttp' 'aioitertools' 'wrapt'

COPY handler.py ./
CMD ["handler.handler"]
