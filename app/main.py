"""Synology Photos+ Web 服务：配置、任务、数据库诊断、搜索、缩略图。"""
import json
import os
import re
import secrets
import threading
import time

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, dbaccess, store
from .infer import clip as clip_infer
from .infer import registry, util
from .infer.util import IMAGE_EXTS, VIDEO_EXTS
from .matcher import MATCHER
from .pipeline import PIPELINE, model_version

app = FastAPI(title="Synology Photos+", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_static_cache(request: Request, call_next):
    """静态资源禁用缓存：升级镜像后浏览器立即拿到新前端。"""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
               ".heic": "image/heic", ".heif": "image/heif",
               ".mp4": "video/mp4", ".mov": "video/quicktime"}

# 简单口令会话（web.password 为空则不启用）
_sessions = set()


def _auth(request: Request):
    """FastAPI 依赖：web.password 非空时校验会话。"""
    pw = config.load()["web"].get("password", "")
    if not pw:
        return
    tok = request.headers.get("X-Auth") or request.cookies.get("sp_auth")
    if tok and tok in _sessions:
        return
    raise HTTPException(401, "未登录")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    pw = config.load()["web"].get("password", "")
    if not pw:
        return {"ok": True}
    if secrets.compare_digest(str(body.get("password", "")), pw):
        tok = secrets.token_urlsafe(24)
        _sessions.add(tok)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("sp_auth", tok, httponly=True, samesite="lax")
        return resp
    raise HTTPException(403, "口令错误")


# ---------------------------------------------------------------- 引导数据

_stats_cache = {"t": 0.0, "data": None}


def _stats_cached() -> dict:
    """stats 含 8 个 COUNT 聚合，多客户端 2s 轮询时做 1s 微缓存。"""
    now = time.time()
    if now - _stats_cache["t"] > 1.0 or _stats_cache["data"] is None:
        _stats_cache["data"] = store.stats()
        _stats_cache["t"] = now
    return _stats_cache["data"]


@app.get("/api/status", dependencies=[Depends(_auth)])
async def status_light():
    """轻量状态接口：供页面高频轮询，不返回词表等大对象。"""
    cur = model_version(config.load())
    return {"status": PIPELINE.status(), "stats": _stats_cached(),
            "stale": store.stale_count(cur),
            "stale_pending": store.stale_pending_count(cur)}


@app.get("/api/bootstrap", dependencies=[Depends(_auth)])
async def bootstrap():
    cfg = config.load()
    return {
        "config": _public_cfg(cfg),
        "status": PIPELINE.status(),
        "stats": store.stats(),
        "stale": store.stale_count(model_version(cfg)),
        "stale_pending": store.stale_pending_count(model_version(cfg)),
        "top_tags": store.top_tags(30),
        "vocab": clip_infer.load_vocab(),
        "models": _list_models(),
        "missing_models": registry.missing_models(cfg),
        "mounts": _mount_info(cfg),
    }


def _public_cfg(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    if out["db"]["transport"] == "ssh":
        out["db"]["ssh"]["password"] = "******" if out["db"]["ssh"]["password"] else ""
    else:
        out["db"]["tcp"]["password"] = "******" if out["db"]["tcp"]["password"] else ""
    return out


def _list_models() -> dict:
    yolo, clipm = [], []
    for base, bucket in ((config.MODEL_DIR_USER, yolo),
                         (config.MODEL_DIR_BUILTIN, yolo)):
        if os.path.isdir(base):
            for fn in os.listdir(base):
                if fn.endswith(".onnx") and not any(m["name"] == fn
                                                    for m in bucket):
                    p = os.path.join(base, fn)
                    bucket.append({"name": fn, "size": os.path.getsize(p),
                                   "mtime": os.path.getmtime(p)})
    for base in (config.MODEL_DIR_USER, config.MODEL_DIR_BUILTIN):
        cd = os.path.join(base, "clip")
        if os.path.isdir(cd):
            for fn in os.listdir(cd):
                if fn.endswith(".onnx") and not any(m["name"] == fn
                                                    for m in clipm):
                    p = os.path.join(cd, fn)
                    clipm.append({"name": fn, "size": os.path.getsize(p),
                                  "mtime": os.path.getmtime(p)})
    return {"yolo": yolo, "clip": clipm}


def _mount_info(cfg: dict) -> list:
    out = []
    for m in cfg["mounts"]:
        p = m.get("path", "")
        out.append({"name": m.get("name", p), "path": p,
                    "exists": os.path.isdir(p)})
    return out


# ---------------------------------------------------------------- 配置

@app.get("/api/config", dependencies=[Depends(_auth)])
async def get_config():
    return _public_cfg(config.load())


@app.post("/api/config", dependencies=[Depends(_auth)])
async def set_config(request: Request):
    body = await request.json()
    # 密码占位不覆盖
    if body.get("db", {}).get("ssh", {}).get("password") == "******":
        body["db"]["ssh"]["password"] = config.load()["db"]["ssh"]["password"]
    if body.get("db", {}).get("tcp", {}).get("password") == "******":
        body["db"]["tcp"]["password"] = config.load()["db"]["tcp"]["password"]
    cfg = config.save(body)
    dbaccess.close_transport()   # 让新配置生效
    store.log("info", "配置已更新")
    return _public_cfg(cfg)


# ---------------------------------------------------------------- 任务

@app.post("/api/job", dependencies=[Depends(_auth)])
async def start_job(request: Request):
    body = await request.json()
    jtype = body.get("type")
    if jtype not in ("incremental", "replace", "write_pending", "retry_failed"):
        raise HTTPException(400, "type 必须是 incremental|replace|write_pending|retry_failed")
    if jtype == "retry_failed":
        # 失败项重试次数已达上限被放弃后，用这个入口复活它们
        n = store.reset_error_retries()
        store.log("info", f"已重置 {n} 个失败项的重试计数，开始重新扫描")
        PIPELINE.enqueue("incremental")
        return {"ok": True, "reset": n}
    if jtype == "replace":
        pw = str(body.get("confirm", ""))
        if pw != "REPLACE":
            raise HTTPException(400, "重新分析会先移除本工具已写标签，需要 confirm=REPLACE")
    if jtype == "write_pending" and config.load()["tagging"].get("dry_run"):
        raise HTTPException(400, "当前启用了试运行(dry-run)，补写不会写入群晖；请先在识别设置里关闭试运行并保存")
    try:
        return PIPELINE.enqueue(jtype, body.get("opts") or {})
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/api/job/cancel", dependencies=[Depends(_auth)])
async def cancel_job():
    PIPELINE.cancel()
    return {"ok": True}


# ---------------------------------------------------------------- 数据库诊断

# 备份/测试连接耗时较长，放后台线程执行，避免阻塞 Web 服务
_BG = {"backup": None, "dbtest": None, "restore": None}
_bg_lock = threading.Lock()


def _bg_start(kind: str, fn) -> bool:
    with _bg_lock:
        st = _BG[kind]
        if st and st.get("running"):
            return False
        _BG[kind] = {"running": True, "started": time.time(),
                     "error": None, "result": None}

    def wrap():
        try:
            _BG[kind]["result"] = fn()
        except Exception as e:
            _BG[kind]["error"] = str(e)
        finally:
            _BG[kind]["running"] = False
            if kind == "restore":
                store.set_setting("restore_running", "")

    threading.Thread(target=wrap, daemon=True, name=f"sp-{kind}").start()
    return True


def _do_backup():
    cfg = config.load()
    dest_dir = os.path.join(config.CONFIG_DIR, "backups")
    out = []
    for db in dbaccess.list_databases():
        out.append(dbaccess.transport().backup(
            db, dest_dir, cfg["tagging"].get("backup_keep", 5)))
    store.log("info", f"手动备份完成: {out}")
    return {"files": out}


def _do_dbtest():
    report = {"ok": False, "steps": []}

    def step(name, fn):
        try:
            val = fn()
            report["steps"].append({"name": name, "ok": True, "detail": val})
            return val
        except Exception as e:
            report["steps"].append({"name": name, "ok": False, "detail": str(e)})
            raise

    try:
        dbs = step("连接并枚举 synofoto 数据库", dbaccess.list_databases)
        total = 0
        for db in dbs:
            n = len(dbaccess.enumerate_units(db))
            total += n
        report["steps"].append({"name": "读取 unit 表", "ok": True,
                                "detail": f"{dbs} 共 {total} 个媒体文件"})
        cfg = config.load()
        n = MATCHER.file_count or MATCHER.build_fs_index(
            [m["path"] for m in cfg["mounts"] if m.get("path")] or ["/photos"],
            cfg["scan"].get("exclude_dirs", []),
            set(IMAGE_EXTS) | set(VIDEO_EXTS),
            cfg["image"].get("min_bytes", 0))
        report["steps"].append({"name": "文件系统索引", "ok": True,
                                "detail": f"{n} 个媒体文件"})
        report["ok"] = True
    except Exception:
        report["ok"] = False
    return report


@app.post("/api/db/test", dependencies=[Depends(_auth)])
async def db_test():
    """异步启动测试；用 GET /api/bg/status?kind=dbtest 轮询结果。"""
    if not _bg_start("dbtest", _do_dbtest):
        raise HTTPException(409, "测试已在进行中")
    return {"started": True}


@app.post("/api/db/backup", dependencies=[Depends(_auth)])
async def db_backup():
    """异步启动备份；用 GET /api/bg/status?kind=backup 轮询结果。"""
    if not _bg_start("backup", _do_backup):
        raise HTTPException(409, "备份已在进行中")
    store.log("info", "已开始数据库备份（pg_dump 流式写入 config/backups/）")
    return {"started": True}


@app.get("/api/bg/status", dependencies=[Depends(_auth)])
async def bg_status(kind: str = "backup"):
    st = _BG.get(kind) or {}
    return {"running": bool(st.get("running")), "started": st.get("started"),
            "error": st.get("error"), "result": st.get("result")}


# ---------------------------------------------------------------- 备份管理

@app.get("/api/db/backups", dependencies=[Depends(_auth)])
async def list_backups():
    d = os.path.join(config.CONFIG_DIR, "backups")
    files = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".sql.gz"):
                p = os.path.join(d, fn)
                files.append({"file": fn, "size": os.path.getsize(p),
                              "mtime": os.path.getmtime(p)})
    return {"files": files}


@app.post("/api/db/restore", dependencies=[Depends(_auth)])
async def db_restore(request: Request):
    """从备份还原某个库。高危操作：confirm 必须传完整文件名。"""
    body = await request.json()
    fn = os.path.basename(str(body.get("file", "")))
    if not fn.endswith(".sql.gz"):
        raise HTTPException(400, "文件名非法")
    if str(body.get("confirm", "")) != fn:
        raise HTTPException(400, "请输入完整文件名作为 confirm 确认还原")
    stem = fn[:-7]                              # 去掉 .sql.gz
    parts = stem.split("_")
    if len(parts) < 3:
        raise HTTPException(400, "文件名格式不符（<库名>_<日期>_<时间>.sql.gz）")
    db = "_".join(parts[:-2])
    if not re.match(r"^synofoto(_personal_\d+)?$", db):
        raise HTTPException(400, f"无法识别库名: {db}")
    path = os.path.join(config.CONFIG_DIR, "backups", fn)
    if not os.path.isfile(path):
        raise HTTPException(404, "备份文件不存在")
    if PIPELINE.status()["running"]:
        raise HTTPException(409, "扫描任务进行中，请先取消或等待完成再还原")
    if _BG["backup"] and _BG["backup"].get("running"):
        raise HTTPException(409, "备份进行中，请稍后再还原")
    if not _bg_start("restore", lambda: dbaccess.transport().restore(db, path)):
        raise HTTPException(409, "还原已在进行中")
    store.set_setting("restore_running", "1")
    store.log("warn", f"开始还原 {db} ← {fn}")
    return {"started": True, "db": db}


# ---------------------------------------------------------------- 词表/模型

@app.put("/api/vocab", dependencies=[Depends(_auth)])
async def put_vocab(request: Request):
    body = await request.json()
    tags = body.get("tags")
    if not isinstance(tags, list) or not tags:
        raise HTTPException(400, "tags 不能为空")
    clean = [{"zh": str(t.get("zh", "")).strip(),
              "en": str(t.get("en", "")).strip()}
             for t in tags if t.get("zh") and t.get("en")]
    if not clean:
        raise HTTPException(400, "词表格式错误（需含 zh/en）")
    path = os.path.join(config.CONFIG_DIR, "vocab.json")
    ver = clip_infer.load_vocab().get("version", 1) + 1
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": ver, "tags": clean}, f, ensure_ascii=False, indent=1)
    store.log("info", f"CLIP 词表已更新: {len(clean)} 类（重新分析后生效）")
    return {"ok": True, "count": len(clean), "version": ver}


@app.post("/api/models/upload", dependencies=[Depends(_auth)])
async def upload_model(request: Request):
    form = await request.form()
    f = form.get("file")
    if f is None:
        raise HTTPException(400, "缺少 file")
    name = os.path.basename(f.filename or "")
    if not name.endswith(".onnx"):
        raise HTTPException(400, "仅支持 .onnx")
    os.makedirs(config.MODEL_DIR_USER, exist_ok=True)
    dest = os.path.join(config.MODEL_DIR_USER, name)
    with open(dest, "wb") as out:
        while True:
            chunk = await f.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    store.log("info", f"已上传模型: {name}")
    return {"ok": True, "name": name}


# ---------------------------------------------------------------- 搜索

@app.get("/api/recent", dependencies=[Depends(_auth)])
async def recent(limit: int = 60, status: str = ""):
    """最近分析的照片及其标签——用于扫描过程中实时检查打标效果。"""
    limit = max(1, min(limit, 300))
    sql = ("SELECT db_name, unit_id, status, tags, ocr_text, rel_path, "
           "mtime, processed_at FROM processed")
    params = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY processed_at DESC LIMIT ?"
    params.append(limit)
    items = []
    for r in store.query(sql, params):
        items.append({
            "db": r["db_name"], "unit_id": r["unit_id"],
            "status": r["status"], "rel_path": r["rel_path"],
            "thumb": _thumb_url(r["rel_path"] or ""),
            "tags": json.loads(r["tags"] or "[]"),
            "ocr": (r["ocr_text"] or "")[:150],
            "processed_at": r["processed_at"],
        })
    return {"items": items}


@app.get("/api/search", dependencies=[Depends(_auth)])
async def search(q: str, mode: str = "auto", limit: int = 60):
    q = (q or "").strip()
    if not q:
        return {"items": [], "mode": mode}
    limit = max(1, min(limit, 200))
    if mode in ("auto", "semantic"):
        hits = _semantic_search(q, limit)
        if hits:
            return {"items": hits, "mode": "semantic"}
        if mode == "semantic":
            return {"items": [], "mode": "semantic"}
    items = []
    like = f"%{q}%"
    rows = store.query(
        "SELECT db_name, unit_id, tags, ocr_text, rel_path, processed_at "
        "FROM processed WHERE status IN ('written','analyzed','empty') "
        "AND (tags LIKE ? OR ocr_text LIKE ?) "
        "ORDER BY processed_at DESC LIMIT ?", (f"%\"{q}\"%", like, limit))
    for r in rows:
        items.append({"db": r["db_name"], "unit_id": r["unit_id"],
                      "rel_path": r["rel_path"], "thumb": _thumb_url(r["rel_path"]),
                      "tags": json.loads(r["tags"] or "[]"),
                      "ocr": (r["ocr_text"] or "")[:200]})
    return {"items": items, "mode": "keyword"}


def _semantic_search(q: str, limit: int):
    try:
        cfg = config.load()
        if not cfg["clip"]["enabled"]:
            return []
        vocab = clip_infer.load_vocab()
        cfg["clip"]["model"] = registry.resolve_clip_model(cfg["clip"]["model"])
        ent = registry.get("clip", cfg["clip"]["model"])
        emb = clip_infer._encode_text(ent, [q], ent["profile"])[0]
        # 只比对此前用同一 CLIP 模型生成的向量（不同模型向量空间不兼容）
        vecs = store.load_embeddings(model=cfg["clip"]["model"])
        if not vecs:
            return []
        mat = np.stack([v for _, _, v in vecs])
        norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        sims = (mat / norms) @ emb
        idx = np.argsort(-sims)[:limit]
        out = []
        for i in idx:
            if sims[i] < 0.16:
                break
            db, unit_id, _ = vecs[i]
            row = store.query_one(
                "SELECT rel_path, tags, ocr_text FROM processed "
                "WHERE db_name=? AND unit_id=?", (db, unit_id))
            if not row:
                continue
            out.append({"db": db, "unit_id": unit_id,
                        "rel_path": row["rel_path"],
                        "thumb": _thumb_url(row["rel_path"]),
                        "score": round(float(sims[i]), 3),
                        "tags": json.loads(row["tags"] or "[]")})
        return out
    except Exception as e:
        store.log("error", f"语义搜索失败: {e}")
        return []


def _thumb_url(rel_path: str) -> str:
    if not rel_path:
        return ""
    return "/api/file?path=" + _quote(rel_path)


def _quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


@app.get("/api/file", dependencies=[Depends(_auth)])
async def serve_file(path: str):
    """返回原文件用于预览。防目录穿越。

    优先：分析时记录的真实容器路径（必须落在挂载根内）；
    其次：按挂载根直拼相对路径；最后：后缀索引定位。
    """
    cfg = config.load()
    mounts = [(os.path.abspath(m["path"]).rstrip("/"))
              for m in cfg["mounts"] if m.get("path")] or ["/photos"]

    def _try(cand: str):
        if os.path.isfile(cand):
            ext = os.path.splitext(cand)[1].lower()
            return FileResponse(cand, media_type=MEDIA_TYPES.get(ext,
                                "application/octet-stream"))
        return None

    # 1) 绝对路径（须位于挂载根内）
    cand = os.path.abspath(path.replace("\\", "/"))
    if not os.path.basename(cand).startswith(".") and \
            any(cand == r or cand.startswith(r + os.sep) for r in mounts):
        resp = _try(cand)
        if resp:
            return resp
        ext_low = os.path.splitext(cand)[1].lower()
        if resp is None and (ext_low in util.RAW_EXTS
                             or ext_low in (".heic", ".heif", ".tif", ".tiff",
                                            ".avif")):
            # 浏览器无法直接显示的格式：回源群晖在 @eaDir 生成的缩略图
            ea_thumb = util.find_ea_thumb(cand)
            if ea_thumb:
                resp = _try(ea_thumb)

    # 2) 相对路径直拼
    rel = os.path.normpath(path.lstrip("/").replace("\\", "/"))
    if rel.startswith(".."):
        raise HTTPException(400, "非法路径")
    for r in mounts:
        resp = _try(os.path.join(r, rel))
        if resp:
            return resp

    # 3) 后缀索引定位（索引已由扫描任务建好；这里绝不同步建索引——
    #    async 路由里遍历全树会把整个 Web 服务卡住）
    if MATCHER.file_count:
        hit = MATCHER.locate(rel)
        if hit:
            resp = _try(hit)
            if resp:
                return resp
    raise HTTPException(404, "文件不存在（检查挂载配置或先运行一次扫描）")


# ---------------------------------------------------------------- 日志

@app.get("/api/logs", dependencies=[Depends(_auth)])
async def logs(after_id: int = 0, limit: int = 200, order: str = "desc"):
    """order=asc 供日志页增量追赶（刷屏期间不会漏中间行）。"""
    direction = "ASC" if order == "asc" else "DESC"
    return {"rows": store.query(
        f"SELECT id, ts, level, msg FROM events WHERE id > ? "
        f"ORDER BY id {direction} LIMIT ?", (after_id, limit))}


# ---------------------------------------------------------------- 静态页

@app.get("/", response_class=HTMLResponse)
async def index():
    return open(os.path.join(STATIC_DIR, "index.html"),
                encoding="utf-8").read()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _download_models_bg():
    """模型后台下载（子进程，输出逐行进日志）。可选模型一并下载。"""
    import subprocess
    import sys
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "scripts", "download_models.py")
    if not os.path.isfile(script):
        store.log("error", f"模型下载脚本不存在: {script}")
        raise RuntimeError("模型下载脚本不存在")
    store.log("warn", "开始后台下载模型（约 420MB，期间无法扫描）…")
    proc = subprocess.Popen([sys.executable, script, "--all",
                             "--dest", config.MODEL_DIR_BUILTIN],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            store.log("info", f"[模型下载] {line[:300]}")
    proc.wait()
    if proc.returncode == 0:
        store.log("info", "模型下载完成，可以开始扫描")
    else:
        raise RuntimeError(f"模型下载失败（exit={proc.returncode}），"
                           f"可在 compose 中设置 HF_ENDPOINT 镜像后重试")


@app.post("/api/models/download", dependencies=[Depends(_auth)])
async def download_models():
    """后台下载缺失模型；用 GET /api/bg/status?kind=modeldl 轮询。"""
    if not _bg_start("modeldl", _download_models_bg):
        raise HTTPException(409, "下载已在进行中")
    return {"started": True}


@app.on_event("startup")
async def on_startup():
    PIPELINE.start()
    store.log("info", "Synology Photos+ 已启动")
    missing = registry.missing_models(config.load())
    if missing:
        store.log("warn", f"检测到缺失模型: {', '.join(missing)}。"
                          f"请到'模型与词表'页点击下载，或自行放置到 app/models/")
