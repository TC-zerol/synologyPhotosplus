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


def find_ea_thumb(photo_path: str) -> str:
    """查找群晖 Photos 为该照片生成的缩略图（@eaDir/<名>/SYOPHOTO_THM*）。

    这是 Synology Photos 自己的缓存，不新建任何文件。命名有历史变体
    （SYOPHOTO_THM_M/L/S、含 bate 后缀等），取其中最大的那张。
    """
    d = os.path.dirname(photo_path)
    ea = os.path.join(d, "@eaDir", os.path.basename(photo_path))
    if not os.path.isdir(ea):
        return ""
    best, best_size = "", -1
    for fn in os.listdir(ea):
        if not fn.upper().startswith("SYOPHOTO_THM"):
            continue
        if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        p = os.path.join(ea, fn)
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if sz > best_size:
            best, best_size = p, sz
    return best


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
    if os.path.splitext(path)[1].lower() in RAW_EXTS:
        return _imread_raw(path)
    _ensure_heif()
    last_err = None
    for hdr_to_8bit in (False, True):   # 第二次尝试：HDR(10bit) 降为 8bit
        try:
            from PIL import Image, ImageOps
            if hdr_to_8bit:
                import pillow_heif
                im_heif = pillow_heif.open_heif(path, convert_hdr_to_8bit=True)
                im = Image.frombytes("RGB" if im_heif.mode == "RGB" else im_heif.mode,
                                     im_heif.size, bytes(im_heif.data))
                im = ImageOps.exif_transpose(im)
            else:
                im = Image.open(path)
                im = ImageOps.exif_transpose(im)
            im = im.convert("RGB")
            return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
        except Exception as e:
            last_err = e
    # 编码器不被支持（典型：iOS 17 的 AV1-HEIF，报 No 'hvcC' box）时，
    # 退回群晖已生成的 @eaDir 缩略图参与分析——分辨率对打标足够
    global last_read_note, last_read_error
    last_read_note = ""
    if os.path.splitext(path)[1].lower() in (".heic", ".heif", ".avif"):
        ea = find_ea_thumb(path)
        if ea:
            img2 = cv2.imread(ea, cv2.IMREAD_COLOR)
            if img2 is not None:
                last_read_note = (f"{path}: 编码不受支持（{last_err}），"
                                  f"已用群晖缩略图替代原图分析")
                return img2
    last_read_error = f"{path}: {last_err}" if last_err else f"{path}: 未知原因"
    return None


# 读取状态备忘（pipeline 记日志用）
last_read_error = ""
last_read_note = ""
