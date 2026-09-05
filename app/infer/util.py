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


RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf",
            ".rw2", ".dng", ".pef", ".srw"}


def downscale(img, max_side: int = 2000):
    """长边超过 max_side 时等比缩小（打标场景无需全分辨率，OCR/CLIP 大幅提速）。"""
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return img
    s = max_side / m
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)


def imread_any(path: str, reduce_scale: int = None):
    """cv2 直读，失败则走 PIL（含 HEIC/GIF）。返回 BGR ndarray 或 None。

    reduce_scale: JPEG 专用硬件级降采样倍数（IMREAD_REDUCED_COLOR_*），
    解码阶段即省去高分辨率的计算，用于打标这类不需要全分辨率的场景。
    """
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None
    flags = cv2.IMREAD_COLOR
    if reduce_scale in (2, 4, 8):
        flags = cv2.IMREAD_COLOR | cv2.IMREAD_REDUCED_COLOR_2 if reduce_scale == 2             else cv2.IMREAD_COLOR | cv2.IMREAD_REDUCED_COLOR_4 if reduce_scale == 4             else cv2.IMREAD_COLOR | cv2.IMREAD_REDUCED_COLOR_8
    img = cv2.imread(path, flags)
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
