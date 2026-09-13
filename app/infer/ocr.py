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


# App UI 高频词/无检索价值词：截图里到处都是，打成标签只会污染词表
STOPWORDS = {
    "关注", "推荐", "评论", "点赞", "分享", "转发", "收藏", "首页", "我的",
    "消息", "私信", "更多", "打开", "查看", "详情", "搜索", "登录", "注册",
    "下载", "安装", "立即", "免费", "广告", "直播", "回放", "举报", "客服",
    "确认", "取消", "删除", "编辑", "复制", "保存", "提交", "发送", "已读",
    "全部", "其他", "今天", "昨天", "明天", "刚刚", " Crop ", "crop",
}


def keywords(lines: list, min_len: int = 2, max_kw: int = 24) -> list:
    """从 OCR 行提取适合做标签的关键词（去重、保序、过滤噪声）。"""
    out, seen = [], set()

    def add(kw: str):
        kw = _STRIP.sub("", kw).strip()
        if not kw:
            return
        low = kw.lower()
        if low in seen:
            return
        if low in STOPWORDS or kw in STOPWORDS:
            return
        if len(kw) < min_len:
            return
        if len(kw) > 24:  # 过长的片段不适合做标签
            return
        # 纯数字/纯符号串（如"100""3:51"）没有检索价值
        if not _CJK.search(kw) and not re.search(r"[A-Za-z]", kw):
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
        # 整行保留条件收紧：≤10 字、含中文、且不含数字/符号混排
        # （"【9:51》退款已受理，原订单号"这类转账/通知长句不再是标签，
        #  但全文仍在本地可搜）
        if _CJK.search(text) and len(text) <= 10                 and not re.search(r"[0-9A-Za-z《》【】》:：/]", text):
            add(text)
        if len(out) >= max_kw:
            break
    return out[:max_kw]


def full_text(lines: list, max_len: int = 4000) -> str:
    return "\n".join(t for t, _ in lines)[:max_len]
