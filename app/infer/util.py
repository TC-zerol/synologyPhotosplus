"""图像读取工具：jpg/png/webp/bmp/tiff/gif/heic 等统一读为 BGR ndarray。"""
import os

import cv2
import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif",
              ".tiff", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts",
              ".3gp", ".wmv", ".webm"}

_heif_ready = False


def _ensure_heif():
    global _heif_ready
    if not _heif_ready:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except Exception:
            pass  # HEIC 不可用时由调用方按读取失败处理
        _heif_ready = True


def imread_any(path: str):
    """cv2 直读，失败则走 PIL（含 HEIC/GIF）。返回 BGR ndarray 或 None。"""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is not None:
        return img
    _ensure_heif()
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    except Exception:
        return None
