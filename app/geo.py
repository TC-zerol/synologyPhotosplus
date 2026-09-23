"""群晖 geocoding_info 多语言地点解析：统一输出简体中文地点标签。

背景：geocoding_info 对每个 id_geocoding 存多行（不同 lang），旧代码固定取
lang=0，导致不同照片的地点标签混着英文/繁体/简体，搜索体验割裂。
这里拿到全部语言行后在本地优选：

1. 含"仅简体使用"汉字的行（如 国/县/龙/门/东/广…）→ 简体中文行，最优；
2. 含汉字但含"仅繁体使用"字（國/縣/龍/門/東/廣…）→ 繁体行，次之
   （简繁相同的短名如"上海市"两种写法文字一致，不影响搜索）；
3. 都没有 → 退回配置的首选 lang / lang 0（保持旧行为）。

同时尽量找一行纯 ASCII（通常是英文）作为 normalized_name 双语后缀，
保证 Photos 搜索框中英文都能前缀命中。
"""
import re

from . import dbaccess, store

GEO_KEYS = ("country", "province", "city", "town")

_CJK = re.compile(r"[一-鿿]")

# 地名里高频出现、且简繁写法不同的字（只列地名常用字，避免误判）。
# 简体独有 → 该行是简体；繁体独有 → 该行是繁体。
_SIMP_ONLY = set("国县区龙门东广云贵陕湾岛济阳华宁兰乌苏沪鲁琼陇维贝尔伦罗马来亚圣萨")
_TRAD_ONLY = set("國縣區龍門東廣雲貴陝灣島濟陽華寧蘭烏蘇滬魯瓊隴維貝爾倫羅馬來亞聖薩")


def _row_score(row: dict) -> tuple:
    text = "".join(str(row.get(k) or "") for k in GEO_KEYS)
    simp = sum(1 for ch in text if ch in _SIMP_ONLY)
    trad = sum(1 for ch in text if ch in _TRAD_ONLY)
    cjk = 1 if _CJK.search(text) else 0
    # 主键：简体信号净分；次键：是否含中文（任何中文都优于纯英文）
    return (simp - trad, cjk)


def _is_ascii_row(row: dict) -> bool:
    text = "".join(str(row.get(k) or "") for k in GEO_KEYS).strip()
    return bool(text) and text.isascii()


def pick_geo(rows: list, prefer_lang: int = 0):
    """从某 id_geocoding 的全部语言行中选出 (主语言行, 英文行)。

    返回 (dict|None, dict|None)：主行用于标签名，英文行用于 normalized_name。
    prefer_lang>0 时强制取该 lang 的行（用户手动指定，兼容旧配置）。
    """
    if not rows:
        return None, None
    if prefer_lang:
        forced = [r for r in rows if r.get("lang") == prefer_lang]
        if forced:
            en = next((r for r in rows if _is_ascii_row(r)), None)
            return forced[0], en
    best = max(rows, key=lambda r: (_row_score(r), -int(r.get("lang") or 0)))
    en = next((r for r in rows if _is_ascii_row(r)), None)
    if en is best:
        en = None
    return best, en


def attach_geo(db: str, units: list, prefer_lang: int = 0) -> int:
    """给枚举出的 unit 就地挂上 geo_zh/geo_en 两个字典（country/province/...）。

    返回成功解析出地点的 unit 数。无 geocoding 支持或无地点时静默跳过。
    """
    ids = [u.get("id_geocoding") for u in units if u.get("id_geocoding")]
    if not ids:
        return 0
    geomap = dbaccess.fetch_geo_rows(db, ids)
    n = 0
    for u in units:
        rows = geomap.get(u.get("id_geocoding"))
        if not rows:
            continue
        zh, en = pick_geo(rows, prefer_lang)
        if zh:
            u["geo_zh"] = {k: (zh.get(k) or "").strip() for k in GEO_KEYS}
            n += 1
        if en:
            u["geo_en"] = {k: (en.get(k) or "").strip() for k in GEO_KEYS}
    if n:
        store.log("info", f"{db}: {n} 个文件带地点信息（已按简体中文优选）")
    return n
