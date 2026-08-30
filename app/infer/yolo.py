"""YOLOv8 ONNX 推理：letterbox 预处理 + NMS，返回 [(class_id, score)]。"""
import cv2
import numpy as np

from . import registry
from ..coco_zh import COCO_ZH

# 类别 id -> (英文名, 中文名)（COCO-80 官方英文）
COCO_EN = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
    "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
    "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake",
    "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
    "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


def _letterbox(img: np.ndarray, size: int):
    h, w = img.shape[:2]
    scale = min(size / h, size / w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, np.uint8)
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas[top:top + nh, left:left + nw] = resized
    return canvas, scale, left, top


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float = 0.55) -> list:
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_thr]
    return keep


def detect(image_bgr: np.ndarray, model_name: str, conf_thr: float = 0.35,
           max_det: int = 20) -> list:
    """返回 [(class_id, score)]，按置信度降序。"""
    ent = registry.get("yolo", model_name)
    sess, input_name, size = ent["sess"], ent["input_name"], ent["size"]
    h0, w0 = image_bgr.shape[:2]
    canvas, scale, left, top = _letterbox(image_bgr, size)
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = rgb.transpose(2, 0, 1)[None]
    out = sess.run(None, {input_name: x})[0]        # (1, 84, N) 或 (1, N, 85)
    if out.ndim == 3 and out.shape[1] < out.shape[2]:
        out = out.transpose(0, 2, 1)                # -> (1, N, 84)
    pred = out[0]
    if pred.shape[1] == 85:                         # yolov5 风格含 objectness
        obj = pred[:, 4:5]
        pred = np.concatenate([pred[:, :4], pred[:, 5:] * obj], axis=1)
    scores = pred[:, 4:]
    cls_ids = scores.argmax(axis=1)
    confs = scores.max(axis=1)
    mask = confs >= conf_thr
    if not mask.any():
        return []
    confs, cls_ids, boxes_all = confs[mask], cls_ids[mask], pred[mask, :4]
    # xywh(letterbox) -> xyxy(原图)
    cx, cy, bw, bh = boxes_all[:, 0], boxes_all[:, 1], boxes_all[:, 2], boxes_all[:, 3]
    x1 = (cx - bw / 2 - left) / scale
    y1 = (cy - bh / 2 - top) / scale
    x2 = (cx + bw / 2 - left) / scale
    y2 = (cy + bh / 2 - top) / scale
    boxes = np.stack([x1, y1, x2, y2], axis=1)
    keep = _nms(boxes, confs)
    results = [(int(cls_ids[i]), float(confs[i])) for i in keep
               if 0 <= int(cls_ids[i]) < 80]
    results.sort(key=lambda r: -r[1])
    return results[:max_det]


def detect_file(path: str, model_name: str, conf_thr: float = 0.35,
                max_det: int = 20) -> list:
    from .util import imread_any
    img = imread_any(path)
    if img is None:
        raise ValueError(f"无法读取图片: {path}")
    return detect(img, model_name, conf_thr, max_det)


def labels(cls_id: int, language: str = "bilingual") -> tuple:
    """返回 (显示名, normalized_name)"""
    en = COCO_EN[cls_id] if 0 <= cls_id < len(COCO_EN) else str(cls_id)
    zh = COCO_ZH[cls_id] if 0 <= cls_id < len(COCO_ZH) else en
    if language == "en":
        return en, f"{en} {zh}".lower()
    if language == "zh":
        return zh, f"{zh} {en}".lower()
    return zh, f"{en} {zh}".lower()
