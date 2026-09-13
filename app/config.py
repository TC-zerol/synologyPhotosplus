"""全局配置：默认值 + /config/config.json 深合并持久化。"""
import copy
import json
import os
import threading

CONFIG_DIR = os.environ.get("SP_CONFIG_DIR", "/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
MODEL_DIR_BUILTIN = os.path.join(os.path.dirname(__file__), "models")
MODEL_DIR_USER = os.path.join(CONFIG_DIR, "models")

DEFAULTS = {
    "web": {
        "password": "",            # 为空则免登录（仅建议内网使用）
    },
    "mounts": [
        # 默认索引容器内 /photos 全树（compose 把照片目录挂到这里即可），
        # 多用户/子目录自动发现，无需逐个配置
        {"name": "photos", "path": "/photos"}
    ],
    "db": {
        "transport": "ssh",        # ssh | tcp
        "ssh": {
            "host": "127.0.0.1",   # 容器使用 host 网络时即为 NAS 本机
            "port": 22,
            "user": "",
            "password": "",
            "use_sudo": True,      # 以 sudo -u postgres 执行 psql
        },
        "tcp": {
            "host": "",
            "port": 5432,
            "user": "tagger",
            "password": "",
        },
        "databases": "auto",       # "auto" 或显式列表 ["synofoto", "synofoto_personal_2"]
        "force_tag_owner_id": None,  # 强制标签 id_user；None=自动(按 unit 所属用户)
    },
    "detect": {
        "enabled": True,
        "model": "yolov8n.onnx",   # 内置 yolov8n.onnx / yolov8s.onnx 或 /config/models 下的自定义文件
        "confidence": 0.35,
        "max_tags": 10,            # 单张图片物体标签上限（按置信度取前 N）
    },
    "clip": {
        "enabled": True,
        "model": "cnclip.onnx",    # 推荐 cnclip.onnx=中文CLIP ViT-L（中文概念更准）；
                                   # model.onnx=标准 CLIP ViT-B/32（更快）
        "prob_thr": 0.05,          # 零样本概率阈值（443 类 softmax，过低会出弱标签）
        "sim_floor": 0.22,         # 余弦相似度下限（cnclip 基线较高，建议 0.20~0.28）
        "max_tags": 8,             # 单张图片 CLIP 标签上限
    },
    "ocr": {
        "enabled": True,
        "confidence": 0.6,         # 文本行置信度阈值
        "min_kw_len": 2,           # 关键词最短长度
        "max_kw": 12,              # 单张图片 OCR 关键词标签上限
        "max_text_len": 4000,      # 本地全文库存的最大字符数
    },
    "exif": {
        "enabled": True,          # 元数据标签：拍摄日期(年/月/季节)、地点(国家/省/市)、相机型号
        "geocoding_lang": 0,      # 群晖地理编码语言索引（0=默认，部分安装中文在其他 lang 值）
    },
    "video": {
        "enabled": False,
        "sample_interval": 5.0,    # 每隔多少秒抽一帧
        "max_frames": 10,
    },
    "tagging": {
        "language": "bilingual",   # zh | en | bilingual（general_tag.name 的显示语言）
        "tag_prefix": "",          # 写入标签的前缀，例如 "AI·"，便于与手工标签区分
        "max_tags_per_photo": 15,  # 单张照片写入标签的总量上限（三引擎合并后）
        "dry_run": False,          # True=只分析不写库
        "backup_before_write": True,
        "backup_keep": 5,
    },
    "scan": {
        "exclude_dirs": ["@eaDir", "#recycle", "@tmp", "#snapshot"],
        "poll_interval_min": 0,    # 增量轮询间隔（分钟），0=仅手动扫描
        "batch_size": 50,          # 每批写库的照片数
    },
    "image": {"min_bytes": 2048},
}

_lock = threading.Lock()
_cache = {}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load() -> dict:
    with _lock:
        if _cache:
            return _cache
        cfg = copy.deepcopy(DEFAULTS)
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = _deep_merge(cfg, json.load(f))
            except Exception as e:
                raise RuntimeError(f"config.json 解析失败: {e}")
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.makedirs(MODEL_DIR_USER, exist_ok=True)
        _cache.update(cfg)
        return _cache


def save(update: dict) -> dict:
    with _lock:
        merged = _deep_merge(_cache or DEFAULTS, update)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        _cache.clear()
    return load()
