"""模型管理器：懒加载 + 引用计数 + 空闲自动卸载（释放内存）。

所有 ONNX/RapidOCR 会话统一从这里拿；pipeline 在任务结束后调用
touch() 之外的 unload_idle()，或直接 unload_all()。
"""
import gc
import os
import threading
import time

import numpy as np
import onnxruntime as ort

from .. import config

_lock = threading.RLock()
_sessions = {}          # name -> {"sess":..., "used": ts, "meta": {...}}
_last_use = 0.0
_idle_timer = None

IDLE_UNLOAD_SEC = 600   # 空闲 10 分钟后自动卸载全部模型

INTRA_THREADS = int(os.environ.get("SP_ORT_THREADS", "2"))


def _so():
    so = ort.SessionOptions()
    so.intra_op_num_threads = INTRA_THREADS
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    return so


def _load_yolo(name: str):
    path = _resolve_model_path(name)
    sess = ort.InferenceSession(path, sess_options=_so(),
                                providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    shape = inp.shape
    size = 640
    try:
        if isinstance(shape[2], int) and shape[2] > 0:
            size = shape[2]
    except Exception:
        pass
    return {"sess": sess, "input_name": inp.name, "size": size,
            "output_names": [o.name for o in sess.get_outputs()]}


def _load_clip(name: str):
    path = _resolve_model_path(name)
    sess = ort.InferenceSession(path, sess_options=_so(),
                                providers=["CPUExecutionProvider"])
    from tokenizers import Tokenizer
    # 分词器优先用与模型同名文件（如 cnclip.onnx → cnclip_tokenizer.json）
    tok_path = path[:-5] + "_tokenizer.json"
    if not os.path.isfile(tok_path):
        tok_path = os.path.join(os.path.dirname(path), "tokenizer.json")
    tok = Tokenizer.from_file(tok_path)
    from .clip import _profile
    return {"sess": sess, "tok": tok,
            "inputs": {i.name for i in sess.get_inputs()},
            "profile": _profile(name)}


def _load_ocr():
    from rapidocr_onnxruntime import RapidOCR
    return {"ocr": RapidOCR()}


_LOADERS = {"yolo": _load_yolo, "clip": _load_clip, "ocr": _load_ocr}


def _resolve_model_path(name: str) -> str:
    for base in (config.MODEL_DIR_USER, config.MODEL_DIR_BUILTIN):
        p = os.path.join(base, name)
        if os.path.isfile(p):
            return p
        if os.path.isfile(os.path.join(base, "clip", name)):
            return os.path.join(base, "clip", name)
    raise FileNotFoundError(f"模型文件不存在: {name} "
                            f"(查找于 {config.MODEL_DIR_USER} / {config.MODEL_DIR_BUILTIN})")


def resolve_clip_model(name: str) -> str:
    """配置的 CLIP 模型不存在时，回退到可用的模型（优先 cnclip.onnx）。

    防止旧配置指向已被清理的模型文件导致 CLIP 引擎全量失败。
    """
    try:
        _resolve_model_path(name)
        return name
    except FileNotFoundError:
        pass
    for base in (config.MODEL_DIR_USER, config.MODEL_DIR_BUILTIN):
        cd = os.path.join(base, "clip")
        if os.path.isfile(os.path.join(cd, "cnclip.onnx")):
            return "cnclip.onnx"
        if os.path.isdir(cd):
            for fn in sorted(os.listdir(cd)):
                if fn.endswith(".onnx"):
                    return fn
    raise FileNotFoundError("clip 目录下没有任何可用的 CLIP 模型")


def get(kind: str, name: str = None):
    """获取（并按需加载）一个模型会话。kind: yolo|clip|ocr"""
    global _last_use
    with _lock:
        _last_use = time.time()
        key = kind if kind == "ocr" else f"{kind}:{name}"
        ent = _sessions.get(key)
        if ent is None:
            if kind == "yolo":
                ent = {"sess": None, **_load_yolo(name)}
            elif kind == "clip":
                ent = {"sess": None, **_load_clip(name)}
            else:
                ent = _load_ocr()
            ent["used"] = time.time()
            _sessions[key] = ent
        ent["used"] = time.time()
        return ent


def loaded() -> list:
    with _lock:
        return list(_sessions.keys())


def unload(key: str = None):
    """卸载指定会话或全部，释放内存。"""
    global _last_use
    with _lock:
        keys = [key] if key else list(_sessions.keys())
        for k in keys:
            ent = _sessions.pop(k, None)
            if ent:
                ent.clear()
        gc.collect()
        _last_use = time.time()


def unload_all():
    unload(None)


def idle_unload_loop(stop_event: threading.Event):
    """后台线程：空闲超时自动卸载全部模型。"""
    global _last_use
    _last_use = time.time()
    while not stop_event.wait(30):
        with _lock:
            due = bool(_sessions) and time.time() - _last_use > IDLE_UNLOAD_SEC
        if due:
            unload_all()


def image_embed_dim() -> int:
    return 512
