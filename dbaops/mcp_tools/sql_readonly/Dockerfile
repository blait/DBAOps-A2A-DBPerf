FROM public.ecr.aws/lambda/python:3.12-arm64
WORKDIR /var/task
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY handler.py ./
CMD ["handler.handler"]
