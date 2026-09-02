#!/usr/bin/env python3
"""下载识别模型（GitHub 克隆后构建镜像时自动补齐，git 不跟踪二进制模型）。

- 支持 HF_ENDPOINT 环境变量切换镜像（国内网络可用 https://hf-mirror.com）
- 每个文件下载后做 sha256 校验，不一致自动重试下一个源
- 已存在且校验通过的文件直接跳过（重复构建不重复下载）

用法：python scripts/download_models.py [--all]
  默认只下载默认档位所需的模型；--all 额外下载可选项（yolov8s 等）
"""
import argparse
import hashlib
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_DIR = os.path.join(os.path.dirname(HERE), "app", "models")

HF_ENDPOINTS = [
    os.environ.get("HF_ENDPOINT", "").rstrip("/"),
    "https://huggingface.co",
    "https://hf-mirror.com",
]
HF_ENDPOINTS = [e for e in HF_ENDPOINTS if e]

# (相对路径, [候选下载 URL], sha256, 是否可选)
FILES = [
    ("yolov8n.onnx",
     ["kshitijjjjjjjjjjjjjjjj/yolov8n-coco-onnx/resolve/main/yolov8n.onnx"],
     "013a98f3bc0264a3d793ef29ccbd178ceb0bbb86bc12ff3510273ce85b1c4526",
     False),
    ("clip/cnclip.onnx",
     ["Xenova/chinese-clip-vit-large-patch14/resolve/main/onnx/model_quantized.onnx"],
     "a7a037f1589048636a9e0bfb90cb0eaee0743cfbb5164b06c2f881950cb6b9dc",
     False),
    ("clip/cnclip_tokenizer.json",
     ["Xenova/chinese-clip-vit-large-patch14/resolve/main/tokenizer.json"],
     "7dfbf1966ebf99d471c3796e9b457329d2b2182b817e144f1e904b957745c839",
     False),
    ("yolov8s.onnx",
     ["orirdx/yolov8s-coco-onnx/resolve/main/coco_yolov8s.onnx"],
     "ae692a2cd19059a9e4b76427ecefe910d430acf89e3291906743af007b119d5b",
     True),
]


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "synology-photosplus"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="包含可选模型")
    ap.add_argument("--dest", default=DEFAULT_MODEL_DIR,
                    help="模型保存目录（默认仓库内 app/models）")
    args = ap.parse_args()
    want_all = args.all
    model_dir = args.dest
    failed = []
    for rel, urls, sha, optional in FILES:
        if optional and not want_all:
            continue
        dest = os.path.join(model_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.isfile(dest) and sha256_of(dest) == sha:
            print(f"[skip] {rel} 已存在且校验通过")
            continue
        ok = False
        for ep in HF_ENDPOINTS:
            url = f"{ep}/{urls[0]}"
            tmp = dest + ".part"
            try:
                print(f"[down] {url}")
                fetch(url, tmp)
                if sha256_of(tmp) != sha:
                    raise ValueError("sha256 校验失败")
                os.replace(tmp, dest)
                print(f"[ok]   {rel} ({os.path.getsize(dest) // (1 << 20)} MB)")
                ok = True
                break
            except Exception as e:
                print(f"[warn] 源失败: {e}")
                if os.path.isfile(tmp):
                    os.remove(tmp)
        if not ok:
            failed.append(rel)
    if failed:
        print("\n以下模型下载失败，请检查网络或手动放置文件：")
        for rel in failed:
            print(f"  - app/models/{rel}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
