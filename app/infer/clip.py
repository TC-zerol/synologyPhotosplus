"""CLIP 零样本打标 + 512 维图像向量（用于语义搜图）。"""
import json
import os
import threading

import numpy as np

from . import registry

_lock = threading.Lock()
_txt_cache = {"key": None, "names": None, "norm": None, "vocab_ver": None}

VOCAB_BUILTIN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "vocab.json")


def _profile(model_name: str) -> dict:
    """按模型文件名识别档位：中文CLIP(cnvitl) 或标准CLIP。"""
    low = (model_name or "").lower()
    if "cnclip" in low:
        return {"kind": "cnclip", "size": 224,
                "mean": (0.48145466, 0.4578275, 0.40821073),
                "std": (0.26862954, 0.26130258, 0.27577711),
                "template": "一张{}的照片", "pad": 0, "text_len": 52,
                "prompt_side": "zh"}
    return {"kind": "clip", "size": 224,
            "mean": (0.48145466, 0.4578275, 0.40821073),
            "std": (0.26862954, 0.26130258, 0.27577711),
            "template": "a photo of {}", "pad": 49407, "text_len": 77,
            "prompt_side": "en"}


def load_vocab() -> dict:
    """内置词表 + 用户覆盖（/config/vocab.json）。带 _mtime 指纹用于缓存失效。"""
    from .. import config
    data = json.load(open(VOCAB_BUILTIN, "r", encoding="utf-8"))
    user_path = os.path.join(config.CONFIG_DIR, "vocab.json")
    mtime = os.path.getmtime(VOCAB_BUILTIN)
    if os.path.isfile(user_path):
        mtime = os.path.getmtime(user_path)
        try:
            user = json.load(open(user_path, "r", encoding="utf-8"))
            if isinstance(user, dict) and isinstance(user.get("tags"), list):
                data["tags"] = user["tags"]
                data["version"] = user.get("version", data.get("version", 1))
        except Exception:
            pass
    tags = [t for t in data["tags"]
            if isinstance(t, dict) and t.get("zh") and t.get("en")]
    return {"version": data.get("version", 1), "tags": tags, "_mtime": mtime}


def _encode_text(sess_ent, texts: list, prof: dict) -> np.ndarray:
    tok = sess_ent["tok"]
    pad, L = prof["pad"], prof["text_len"]
    rows = []
    for t in texts:
        ids = tok.encode(t).ids[:L]
        ids = ids + [pad] * (L - len(ids))
        rows.append(ids)
    arr = np.array(rows, dtype=np.int64)
    feed = {"input_ids": arr, "attention_mask": (arr != pad).astype(np.int64)}
    if "pixel_values" in sess_ent["inputs"]:
        feed["pixel_values"] = np.zeros(
            (1, 3, prof["size"], prof["size"]), np.float32)
    emb = sess_ent["sess"].run(["text_embeds"], feed)[0]
    return emb / (np.linalg.norm(emb, axis=-1, keepdims=True) + 1e-9)


def text_matrix(vocab: dict, clip_model: str):
    """词表 -> (rows, norm_matrix)，row = (zh, en)。带缓存。"""
    with _lock:
        key = (f"{clip_model}:{vocab['version']}:{len(vocab['tags'])}:"
               f"{vocab.get('_mtime', 0)}")
        if _txt_cache["key"] == key:
            return _txt_cache["names"], _txt_cache["norm"]
        ent = registry.get("clip", clip_model)
        prof = ent["profile"]
        # CLIP 系用英文提示词；中文CLIP 用中文提示词（各自训练分布最优）
        prompts = [prof["template"].format(t[prof["prompt_side"]])
                   for t in vocab["tags"]]
        norm = _encode_text(ent, prompts, prof)
        rows = [(t["zh"], t["en"]) for t in vocab["tags"]]
        _txt_cache.update(key=key, names=rows, norm=norm,
                          vocab_ver=vocab["version"])
        return rows, norm


def analyze(image_bgr: np.ndarray, vocab: dict, clip_model: str,
            prob_thr: float = 0.02, sim_floor: float = 0.17,
            max_tags: int = 8):
    """返回 (tags: [(zh, en, score)], embedding: float32[D])"""
    import cv2
    ent = registry.get("clip", clip_model)
    prof = ent["profile"]
    img = cv2.cvtColor(cv2.resize(image_bgr, (prof["size"], prof["size"]),
                                  interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    px = img.astype(np.float32) / 255.0
    mean = np.array(prof["mean"]); std = np.array(prof["std"])
    px = ((px - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)
    pad, L = prof["pad"], prof["text_len"]
    dummy = np.full((1, L), pad, np.int64)
    emb = ent["sess"].run(
        ["image_embeds"],
        {"pixel_values": px, "input_ids": dummy,
         "attention_mask": np.zeros((1, L), np.int64)})[0][0]
    emb = emb / (np.linalg.norm(emb) + 1e-9)
    rows, tmat = text_matrix(vocab, clip_model)
    sims = tmat @ emb
    logits = (sims * 100.0).astype(np.float64)
    logits -= logits.max()
    probs = np.exp(logits)
    probs /= probs.sum()
    order = np.argsort(-probs)
    tags = []
    for i in order:
        if probs[i] < prob_thr or sims[i] < sim_floor or len(tags) >= max_tags:
            break
        tags.append((rows[i][0], rows[i][1], float(probs[i])))
    return tags, emb.astype(np.float32)
