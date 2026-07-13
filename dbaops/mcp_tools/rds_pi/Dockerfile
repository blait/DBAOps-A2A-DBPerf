FROM public.ecr.aws/lambda/python:3.12-arm64
WORKDIR /var/task
RUN pip install --no-cache-dir boto3
COPY handler.py ./
CMD ["handler.handler"]
