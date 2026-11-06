FROM python:3.10-slim

WORKDIR /app
ENV DATA_DIR=/app/data

# sentence-transformers / torch 等依赖在 slim 镜像中需要编译工具
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# 可选依赖：如需 OCR，取消下一行注释
# COPY requirements-optional.txt .
# RUN pip install --no-cache-dir -r requirements-optional.txt

COPY . .

EXPOSE 8002

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
