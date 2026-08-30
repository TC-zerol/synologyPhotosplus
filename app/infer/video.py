"""视频抽帧 + 目标检测（可选功能）。"""
import cv2

from . import yolo


def detect_video(path: str, model_name: str, conf_thr: float = 0.35,
                 sample_interval: float = 5.0, max_frames: int = 10) -> list:
    """均匀抽样帧跑检测，返回合并后的 [(class_id, score)]。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError(f"无法打开视频: {path}")
    merged = {}
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return []
        step = max(1, int(round(fps * max(0.5, sample_interval))))
        picked = 0
        idx = 0
        while picked < max_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total - 1))
            ok, frame = cap.read()
            if not ok:
                break
            for cls_id, score in yolo.detect(frame, model_name, conf_thr):
                if cls_id not in merged or score > merged[cls_id]:
                    merged[cls_id] = score
            picked += 1
            idx += step
    finally:
        cap.release()
    results = sorted(merged.items(), key=lambda kv: -kv[1])
    return results
