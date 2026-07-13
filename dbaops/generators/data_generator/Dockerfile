FROM --platform=linux/arm64 python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# librdkafka 등 system deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends librdkafka1 librdkafka-dev gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY data_generator/requirements.txt ./
RUN pip install -r requirements.txt

COPY data_generator ./data_generator

ENTRYPOINT ["python", "-m", "data_generator.runner"]
