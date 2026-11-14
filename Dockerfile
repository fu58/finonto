# FinOnto · 单阶段镜像（前端产物不需要构建，因为没有前端构建）
#   docker build -t finonto:0.1 .
#   docker run -p 8000:8000 -v finonto-data:/app/server/var finonto:0.1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FINONTO_DATA_DIR=/app/server/var \
    FINONTO_ENV=prod

WORKDIR /app

COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

COPY server /app/server

WORKDIR /app/server
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
