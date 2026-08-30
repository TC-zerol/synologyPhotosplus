"""RapidOCR 文本识别 + 关键词提取（作为标签写入 general_tag，全文留本地检索）。"""
import re

import numpy as np

from . import registry

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_@./#&]{2,}")
_STRIP = re.compile(r"[\s|:：,，.。;；!！?？()（）\[\]【】<>\"'`~]+$")


def extract_lines(image_bgr: np.ndarray, conf_thr: float = 0.6) -> list:
    """返回 [(text, conf)]"""
    ent = registry.get("ocr")
    res, _ = ent["ocr"](image_bgr)
    if not res:
        return []
    return [(r[1], float(r[2])) for r in res if float(r[2]) >= conf_thr and r[1].strip()]


def keywords(lines: list, min_len: int = 2, max_kw: int = 24) -> list:
    """从 OCR 行提取适合做标签的关键词（去重、保序）。

    - 中文片段 2~12 字直接作为关键词
    - 拉丁/数字串 ≥3 字符作为关键词
    - 整行较短(≤16字)且含中文时整行也保留（如"报销发票"）
    """
    out, seen = [], set()

    def add(kw: str):
        kw = _STRIP.sub("", kw).strip()
        if not kw:
            return
        low = kw.lower()
        if low in seen:
            return
        # 最小长度：中文按字符数、拉丁按字符数
        if len(kw) < min_len:
            return
        if len(kw) > 24:  # 过长的片段不适合做标签
            return
        seen.add(low)
        out.append(kw)

    for text, _conf in lines:
        text = text.strip()
        for seg in _CJK.findall(text):
            if len(seg) <= 12:
                add(seg)
            else:
                # 长中文行按 6 字滑窗切出头部短语
                add(seg[:8])
        for seg in _LATIN.findall(text):
            add(seg)
        if _CJK.search(text) and len(text) <= 16:
            add(text)
        if len(out) >= max_kw:
            break
    return out[:max_kw]


def full_text(lines: list, max_len: int = 4000) -> str:
    return "\n".join(t for t, _ in lines)[:max_len]
