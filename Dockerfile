FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn[standard]==0.30.6" httpx==0.27.2

COPY app/ app/

EXPOSE 8080

CMD ["uvicorn", "app.main:APP", "--host", "0.0.0.0", "--port", "8080"]