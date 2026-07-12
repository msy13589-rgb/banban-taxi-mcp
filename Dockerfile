# 반반택시 MCP — PlayMCP in KC 배포용 (레퍼런스 검증 구조)
FROM python:3.11-slim

WORKDIR /app

ENV PORT=8080
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

EXPOSE 8080

# KAKAO_REST_API_KEY 는 배포 시 런타임 환경변수(비밀값)로 주입
CMD ["python", "server.py"]
