FROM --platform=linux/arm64 python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY log_generator/requirements.txt ./
RUN pip install -r requirements.txt
COPY log_generator ./log_generator
ENTRYPOINT ["python", "-m", "log_generator.runner"]
