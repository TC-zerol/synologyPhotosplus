FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# opencv-headless 需要 libglib2.0；onnxruntime 需要 libgomp1
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/requirements.txt /tmp/requirements.txt
# rapidocr 以 --no-deps 安装，避免拉入依赖 GUI 的 opencv-python；
# 其真实依赖已在 requirements.txt 中手动列出
RUN pip install -r /tmp/requirements.txt \
    && pip install --no-deps rapidocr-onnxruntime==1.3.24

# 保持包结构：/app/run.py + /app/app/…（run.py 以 "app.main:app" 导入）
COPY run.py /app/run.py
COPY app /app/app

VOLUME /config
EXPOSE 47310

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
