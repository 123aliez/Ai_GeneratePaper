# 平台运行镜像：FastAPI + Celery worker + dispatcher 共用。
# 引擎依赖（smolagents/openai/anthropic）由宿主机仓库装好；本镜像只补平台层依赖。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 平台层依赖（与 requirements.txt 合并）
COPY requirements.platform.txt /app/requirements.platform.txt
RUN pip install --no-cache-dir -r requirements.platform.txt

# 整个仓库（agents/ + server/ + run.py + config.py + skills/ 等）
COPY . /app

# 默认起 API；worker/dispatcher 通过 command 覆盖
EXPOSE 8000
CMD ["uvicorn", "server.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
