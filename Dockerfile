FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# 카카오 웹훅 모드로 실행 (기본)
ENV SERVER_MODE=kakao
ENV PORT=8000

EXPOSE 8000

CMD ["python", "server.py"]
