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

# 模型补齐：本地已带的模型（sha256 校验通过）直接跳过、不联网；
# 只有 GitHub 克隆等缺失场景才按 sha256 从 HF 下载。
# 构建时网络不通也没关系：不阻塞构建，容器首次启动会自动后台下载。
# 国内网络可加 --build-arg HF_ENDPOINT=https://hf-mirror.com
ARG HF_ENDPOINT=""
ENV HF_ENDPOINT=${HF_ENDPOINT}
COPY scripts/download_models.py /app/scripts/download_models.py
RUN python /app/scripts/download_models.py --all --dest /app/app/models || \
    echo "WARN: 构建时未能下载模型（网络原因），容器首次启动将自动后台下载"

VOLUME /config
EXPOSE 47310

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
