#!/bin/sh
# Synology Photos+ 构建脚本：干净的逐行构建输出（禁用 buildkit 进度刷屏）
# 用法：sudo ./build.sh            （构建并启动）
#       sudo ./build.sh --build-arg HF_ENDPOINT=https://hf-mirror.com   （国内镜像）
cd "$(dirname "$0")" || exit 1
export BUILDKIT_PROGRESS=plain
export DOCKER_BUILDKIT=1
exec docker compose up -d --build "$@"
