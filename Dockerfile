# ---- 阶段 1：构建 React 前端 ----
FROM node:22-bookworm-slim AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund --registry=https://registry.npmmirror.com
COPY frontend/ ./
RUN npm run build

# ---- 阶段 2：FastAPI 后端 + 静态资源托管 ----
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY backend/app ./app
COPY data/sample ./data/sample
COPY --from=frontend /fe/dist ./static

ENV STATIC_DIR=/app/static \
    DATA_DIR=/app/data/sample \
    AUTO_SEED=true \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
