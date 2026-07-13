FROM python:3.12-slim

# pymssql 빌드 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ freetds-dev curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# 기본: perf A2A 서버. compose에서 command 오버라이드로
# a2a_ops_server.py / streamlit 도 같은 이미지로 기동한다.
CMD ["python", "a2a_perf_server.py"]
