# DBAOps Slack 봇 — Socket Mode. agentcore_client 는 UI 와 공유.
FROM python:3.12-slim

# 차트 PNG 의 한글(제목/라벨)용 폰트 (fontconfig 로 fc-cache 제공)
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-nanum fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY slack_bot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 봇 코드 + 차트 렌더러 + UI 의 agentcore_client(invoke_stream) 재사용
COPY slack_bot/bot.py slack_bot/render.py slack_bot/charts.py /app/
COPY ui/streamlit/agentcore_client.py /app/agentcore_client.py

ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
