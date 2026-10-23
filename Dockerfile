# FinOnto 单镜像部署：前端构建产物由 FastAPI 直接托管，本地浏览器只访问 8000 端口。
FROM node:20-alpine AS client
WORKDIR /build
COPY client/package.json client/package-lock.json ./
RUN npm ci
COPY client/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    FINONTO_DATA_DIR=/app/data
WORKDIR /app
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt
COPY server/ /app/server/
COPY --from=client /build/dist /app/client/dist
RUN mkdir -p /app/data
VOLUME /app/data
EXPOSE 8000
WORKDIR /app/server
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
