"""分析管线：任务调度、断点续扫、批量写库、容错。

设计要点（对应"极强容错 + 断点续扫"）：
- 每张图的推理结果先落本地 SQLite（status=analyzed），再批量写 synofoto；
  写库成功才标记 written。任何时刻崩溃/重启，增量扫描会自动跳过
  已完成项并优先补写"已分析未写库"的项，绝不重复推理。
- 单张图失败只记 error(含重试计数)，不影响整批；error 项在后续扫描中
  自动重试（上限 3 次）。
- 模型任务结束后统一卸载释放内存；另有空闲自动卸载。
"""
import json
import os
import queue
import threading
import time
import traceback

from . import config, dbaccess, matcher, store
from .infer import clip as clip_infer
from .infer import ocr as ocr_infer
from .infer import registry, util, yolo, video as video_infer
from .matcher import MATCHER

ERROR_RETRY_LIMIT = 3


def _missing_engines(st: dict, cfg: dict) -> list:
    """该 unit 上当前启用但上次失败的引擎列表（用于单引擎补全）。"""
    try:
        done = json.loads(st["engines"] or "{}")
    except Exception:
        return []
    enabled = [k for k, on in (("detect", cfg["detect"]["enabled"]),
                               ("clip", cfg["clip"]["enabled"]),
                               ("ocr", cfg["ocr"]["enabled"])) if on]
    return [k for k in enabled if done.get(k) is False]


def model_version(cfg: dict) -> str:
    """识别引擎版本指纹：任一组件（模型/开关/词表）变化即视为过期。"""
    try:
        vocab_ver = clip_infer.load_vocab().get("version", 0)
    except Exception:
        vocab_ver = "?"
    return (f"yolo:{cfg['detect']['model'] if cfg['detect']['enabled'] else 'off'}"
            f"|clip:{cfg['clip']['model'] if cfg['clip']['enabled'] else 'off'}"
            f"|ocr:{'on' if cfg['ocr']['enabled'] else 'off'}"
            f"|vocab:{vocab_ver}")


# ---------------------------------------------------------------- 状态

class _Job:
    def __init__(self, jtype: str, opts: dict = None):
        self.type = jtype
        self.opts = opts or {}
        self.canceled = threading.Event()
        self.phase = "queued"
        self.total = 0
        self.done = 0
        self.current = ""
        self.tags_written = 0
        self.errors = 0
        self.matched = 0
        self.unmatched = 0
        self.started = time.time()
        self.eta_sec = None
        self.finished = None
        self.error = None

    def snapshot(self) -> dict:
        return {
            "type": self.type, "phase": self.phase, "total": self.total,
            "done": self.done, "current": self.current,
            "tags_written": self.tags_written, "errors": self.errors,
            "matched": self.matched, "unmatched": self.unmatched,
            "started": self.started, "eta_sec": self.eta_sec,
            "finished": self.finished, "error": self.error,
            "canceled": self.canceled.is_set(),
        }


class Pipeline:
    def __init__(self):
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=16)
        self._job = None            # 当前任务
        self._last = None           # 上次完成的任务快照
        self._stop = threading.Event()
        self._threads_started = False
        self._last_scan = 0.0

    # ------------------------------------------------------ 线程
    def start(self):
        if self._threads_started:
            return
        self._threads_started = True
        self._last_scan = time.time()   # 启动后不立即自动扫描，等首个轮询周期
        threading.Thread(target=self._worker, daemon=True,
                         name="sp-worker").start()
        threading.Thread(target=self._scheduler, daemon=True,
                         name="sp-scheduler").start()
        threading.Thread(target=registry.idle_unload_loop,
                         args=(self._stop,), daemon=True,
                         name="sp-idle-unload").start()
        leftover = store.get_setting("active_job")
        if leftover:
            store.log("warn", f"检测到上次任务中断：{leftover}（增量扫描会自动续接）")
            store.set_setting("active_job", "")

    # ------------------------------------------------------ 对外接口
    def status(self) -> dict:
        with self._lock:
            cur = self._job.snapshot() if self._job else None
            last = self._last
        return {"running": cur, "last": last,
                "models_loaded": registry.loaded(),
                "last_scan": self._last_scan}

    def enqueue(self, jtype: str, opts: dict = None) -> dict:
        with self._lock:
            if self._job is not None and self._job.finished is None:
                raise RuntimeError("已有任务在运行，请等待完成或取消")
            job = _Job(jtype, opts)
            self._job = job
        self._queue.put(job)
        store.set_setting("active_job", json.dumps(
            {"type": jtype, "opts": opts or {}, "started": job.started}))
        store.log("info", f"任务入队: {jtype} {opts or ''}")
        return job.snapshot()

    def cancel(self):
        with self._lock:
            if self._job:
                self._job.canceled.set()

    # ------------------------------------------------------ 后台线程
    def _worker(self):
        while not self._stop.is_set():
            job = self._queue.get()
            try:
                self._run(job)
            except Exception as e:
                job.error = str(e)
                job.finished = time.time()
                store.log("error", f"任务异常终止: {e}\n{traceback.format_exc()}")
            finally:
                store.set_setting("active_job", "")
                with self._lock:
                    self._last = self._job.snapshot() if self._job else None
                    self._job = None
                registry.unload_all()   # 任务结束释放模型内存
                self._last_scan = time.time()

    def _scheduler(self):
        """增量轮询：poll_interval_min>0 时自动增量扫描。"""
        while not self._stop.wait(60):
            cfg = config.load()
            interval = int(cfg["scan"].get("poll_interval_min", 0))
            if interval <= 0:
                continue
            with self._lock:
                busy = self._job is not None and self._job.finished is None
            if busy or store.get_setting("restore_running") == "1"                     or time.time() - self._last_scan < interval * 60:
                continue
            try:
                self.enqueue("incremental")
            except RuntimeError:
                pass

    # ------------------------------------------------------ 主流程
    def _run(self, job: _Job):
        cfg = config.load()
        if job.type == "write_pending":
            return self._write_pending(job, cfg)
        if job.type in ("incremental", "replace"):
            return self._scan(job, cfg)
        raise ValueError(f"未知任务类型: {job.type}")

    def _enumerate(self, cfg):
        dbs = dbaccess.list_databases()
        if not dbs:
            raise dbaccess.DBError("未找到 synofoto 数据库，请检查数据库连接配置")
        all_units = []
        for db in dbs:
            units = dbaccess.enumerate_units(db)
            store.log("info", f"数据库 {db}: {len(units)} 个媒体文件")
            all_units.extend((db, u) for u in units)
        return dbs, all_units

    def _scan(self, job: _Job, cfg: dict):
        replace = job.type == "replace"
        mounts = [m["path"] for m in cfg["mounts"] if m.get("path")]
        if not mounts:
            mounts = ["/photos"]   # 未配置时自动索引 /photos 全树
            store.log("info", "未配置挂载，自动索引 /photos")
        if cfg["clip"]["enabled"]:
            resolved = registry.resolve_clip_model(cfg["clip"]["model"])
            if resolved != cfg["clip"]["model"]:
                store.log("warn", f"CLIP 模型 {cfg['clip']['model']} 不存在，"
                                  f"本次扫描回退使用 {resolved}（请到识别设置里保存）")
                cfg["clip"]["model"] = resolved
        # 断点续扫第一步：优先补写"已分析未写库"的项（不重新推理）
        if not replace:
            self._flush_pending_records(job, cfg)

        job.phase = "enumerate"
        dbs, all_units = self._enumerate(cfg)
        if job.canceled.is_set():
            return self._finish(job, canceled=True)

        job.phase = "index"
        exts = set(util.IMAGE_EXTS)
        if cfg["video"].get("enabled"):
            exts |= set(util.VIDEO_EXTS)
        n = MATCHER.build_fs_index(mounts, cfg["scan"].get("exclude_dirs", []),
                                   exts, cfg["image"].get("min_bytes", 0))
        store.log("info", f"文件索引完成: {n} 个媒体文件")
        if job.canceled.is_set():
            return self._finish(job, canceled=True)

        # 替换模式：先清掉本工具写入的旧标签
        if replace:
            job.phase = "cleanup"
            for db in dbs:
                script = dbaccess.build_cleanup_script(db)
                if script:
                    dbaccess.transport().exec_script(db, script)
                    store.log("info", f"{db}: 已移除本工具旧标签")
            store.execute("DELETE FROM tag_rows")
            store.execute("DELETE FROM tag_defs")
            store.execute("DELETE FROM embeddings")
            store.execute("DELETE FROM processed")

        # 构建待处理清单（断点核心：SQLite 中 written/empty 视为完成；
        # 但若某引擎上次失败，则进入"单引擎补全"清单）
        job.phase = "plan"
        model_ver = model_version(cfg)
        # 一次性装载记账表（大库逐条查询=每轮上万次 SQLite 往返）
        book = {(r["db_name"], r["unit_id"]): r
                for r in store.query(
                    "SELECT db_name, unit_id, status, retries, engines "
                    "FROM processed")}
        work = []           # (db, unit, missing_engines 或 None)
        for db, u in all_units:
            st = book.get((db, u["unit_id"]))
            if st and st["status"] in ("written", "empty"):
                missing = _missing_engines(st, cfg)
                if missing:
                    work.append((db, u, missing))
                continue
            if st and st["status"] in ("analyzed", "write_error"):
                continue
            if st and st["status"] == "error" \
                    and st["retries"] >= ERROR_RETRY_LIMIT:
                continue
            ext = os.path.splitext(u["filename"])[1].lower()
            if ext in util.VIDEO_EXTS and not cfg["video"].get("enabled"):
                continue
            work.append((db, u, None))
        job.total = len(work)
        store.log("info", f"待处理 {job.total} 项（共枚举 {len(all_units)}，"
                          f"已完成 {len(all_units) - job.total}）")
        if not work:
            return self._finish(job)

        # 逐项处理
        job.phase = "analyze"
        vocab = clip_infer.load_vocab() if cfg["clip"]["enabled"] else None
        batch = []          # 当前写库批次
        t0 = time.time()
        for idx, (db, u, missing) in enumerate(work):
            if job.canceled.is_set():
                break
            # 1) 路径匹配
            path = MATCHER.match(u["folder_name"], u["filename"],
                                 u.get("owner_name") or "")
            rel = (u["folder_name"].lstrip("/") + "/" + u["filename"]).strip("/")
            if path is None:
                store.log("warn", f"路径歧义，跳过: {rel}")
                self._record(db, u["unit_id"], "error", model_ver, [], "", rel,
                             bump=True)
                job.errors += 1
            elif not path:
                job.unmatched += 1   # 未挂载到的文件（如视频被排除等），不记账
                job.done += 1
                self._progress(job, t0)
                continue
            else:
                job.matched += 1
                job.current = rel
                # 2) 推理（单张失败不影响整体）
                # rel_path 记录匹配到的真实容器路径，供缩略图直接定位
                try:
                    result = self._analyze(path, cfg, vocab, model_ver,
                                           only=set(missing) if missing else None)
                    if missing:
                        # 单引擎补全：合并旧标签，只把新增部分送去写库
                        result = self._merge_backfill(db, u["unit_id"], result,
                                                      missing, model_ver, cfg)
                    self._record(db, u["unit_id"], result["status"], model_ver,
                                 result["tags"], result["ocr_text"], path,
                                 embed=result.get("embed"),
                                 owner_id=u["owner_id"],
                                 engines=result.get("engines"),
                                 embed_model=(cfg["clip"]["model"]
                                              if cfg["clip"]["enabled"] and
                                              result.get("embed") is not None
                                              else None))
                    if result["tags"] and result["status"] == "analyzed":
                        batch.append({"db": db, "unit_id": u["unit_id"],
                                      "owner_id": (cfg["db"].get("force_tag_owner_id")
                                                   if cfg["db"].get("force_tag_owner_id") is not None
                                                   else u["owner_id"]),
                                      "tags": result["tags"]})
                    job.done += 1
                except Exception as e:
                    store.log("error", f"分析失败 {rel}: {e}")
                    self._record(db, u["unit_id"], "error", model_ver, [], "",
                                 rel, bump=True, owner_id=u["owner_id"])
                    job.errors += 1
                    job.done += 1
            # 3) 批量写库
            if len(batch) >= cfg["scan"].get("batch_size", 50):
                self._flush(job, cfg, batch)
                batch.clear()
            self._progress(job, t0)
            time.sleep(0.02)   # 让出 CPU，保证 Web 服务响应
        if not job.canceled.is_set() and batch:
            self._flush(job, cfg, batch)
        self._finish(job)

    def _analyze(self, path: str, cfg: dict, vocab, model_ver: str,
                 only=None) -> dict:
        """only: 仅运行这些引擎（单引擎补全用）；None=全部启用引擎。"""
        ext = os.path.splitext(path)[1].lower()
        tags = []           # [(name, normalized, score|None)]
        ocr_text = ""
        embed = None
        engines = {}        # 各启用引擎的成功与否
        lang = cfg["tagging"].get("language", "bilingual")
        want = (lambda k: cfg.get(k, {}).get("enabled") and
                (only is None or k in only))
        if ext in util.VIDEO_EXTS:
            engines["detect"] = False
            dets = []
            if cfg["detect"]["enabled"]:
                try:
                    dets = video_infer.detect_video(
                        path, cfg["detect"]["model"], cfg["detect"]["confidence"],
                        cfg["video"].get("sample_interval", 5.0),
                        cfg["video"].get("max_frames", 10))
                    engines["detect"] = True
                except Exception as e:
                    store.log("error", f"视频检测失败（跳过）{path}: {e}")
            for cls_id, s in dets:
                name, nn = yolo.labels(cls_id, lang)
                tags.append((name, nn, s))
            return {"status": "analyzed" if tags else "empty", "tags": tags,
                    "ocr_text": "", "embed": None, "engines": engines}

        img = util.imread_any(path)
        if img is None:
            raise ValueError("图片读取失败")
        # 各引擎独立容错：单个引擎失败只少一类标签，不连累整张图
        if want("detect"):
            engines["detect"] = False
            try:
                for cls_id, s in yolo.detect(img, cfg["detect"]["model"],
                                             cfg["detect"]["confidence"]):
                    name, nn = yolo.labels(cls_id, lang)
                    if name not in {t[0] for t in tags}:
                        tags.append((name, nn, s))
                tags = tags[: cfg["detect"]["max_tags"]]
                engines["detect"] = True
            except Exception as e:
                store.log("error", f"YOLO 引擎失败（跳过该引擎）{path}: {e}")
        if want("clip"):
            engines["clip"] = False
            try:
                ctags, embed = clip_infer.analyze(
                    img, vocab, cfg["clip"]["model"], cfg["clip"].get("prob_thr"),
                    cfg["clip"].get("sim_floor"), cfg["clip"].get("max_tags", 8))
                for zh, en, score in ctags:
                    if lang == "en":
                        name, nn = en, f"{en} {zh}".lower()
                    elif lang == "zh":
                        name, nn = zh, f"{zh} {en}".lower()
                    else:
                        name, nn = zh, f"{en} {zh}".lower()
                    if name not in {t[0] for t in tags}:
                        tags.append((name, nn, score))
                engines["clip"] = True
            except Exception as e:
                store.log("error", f"CLIP 引擎失败（跳过该引擎）{path}: {e}")
        if want("ocr"):
            engines["ocr"] = False
            try:
                lines = ocr_infer.extract_lines(img, cfg["ocr"]["confidence"])
                kws = ocr_infer.keywords(lines, cfg["ocr"].get("min_kw_len", 2),
                                         cfg["ocr"].get("max_kw", 24))
                prefix = cfg["tagging"].get("tag_prefix", "")
                for kw in kws:
                    nn = (prefix + kw).lower()
                    if (prefix + kw) not in {t[0] for t in tags}:
                        tags.append((prefix + kw, nn, None))
                ocr_text = ocr_infer.full_text(lines, cfg["ocr"].get("max_text_len"))
                engines["ocr"] = True
            except Exception as e:
                store.log("error", f"OCR 引擎失败（跳过该引擎）{path}: {e}")
        cap = cfg["tagging"].get("max_tags_per_photo", 15)
        if len(tags) > cap:
            tags = tags[:cap]
        return {"status": "analyzed" if tags else "empty", "tags": tags,
                "ocr_text": ocr_text, "embed": embed, "engines": engines}

    def _merge_backfill(self, db: str, unit_id: int, result: dict,
                        missing: list, model_ver: str, cfg: dict = None) -> dict:
        """单引擎补全：把新跑出的标签合并进已有结果。

        - 有新增标签：status=analyzed（连同合并后的完整标签列表写库），
          写库脚本的 NOT EXISTS 保护保证旧关联不会重复插入
        - 无新增（引擎恢复但没识别出东西）：保持原状态，只更新 engines 记录
        """
        row = store.query_one(
            "SELECT status, tags, ocr_text, engines FROM processed "
            "WHERE db_name=? AND unit_id=?", (db, unit_id))
        old_tags = [(t["n"], t["nn"], t.get("s"))
                    for t in json.loads((row and row["tags"]) or "[]")]
        old_ocr = (row and row["ocr_text"]) or ""
        merged = old_tags + [t for t in result["tags"]
                             if t[0] not in {x[0] for x in old_tags}]
        cap = (cfg or config.load()).get("tagging", {}).get(
            "max_tags_per_photo", 15)
        merged = merged[:cap]
        engines = {}
        try:
            engines = json.loads((row and row["engines"]) or "{}")
        except Exception:
            pass
        engines.update(result.get("engines") or {})
        result["engines"] = engines
        result["ocr_text"] = result["ocr_text"] or old_ocr
        added = len(merged) - len(old_tags)
        if added > 0:
            result["tags"] = merged
            result["status"] = "analyzed"
            store.log("info", f"单引擎补全 {db}#{unit_id}: "
                              f"{'/'.join(missing)} 新增 {added} 个标签")
        else:
            # 没有新标签：保持 written/empty，避免整标签列表重写
            done_all = not _missing_engines({"engines": json.dumps(engines)},
                                            config.load())
            result["tags"] = old_tags
            result["status"] = "written" if done_all else (
                row["status"] if row else "written")
            if result["status"] == "written" and not old_tags:
                result["status"] = "empty"
            result["_skip_write"] = True
        return result

    # ------------------------------------------------------ 写库
    def _flush(self, job: _Job, cfg: dict, batch: list):
        if cfg["tagging"].get("dry_run"):
            for it in batch:
                store.mark_processed(
                    it["db"], it["unit_id"], "analyzed", model_version(cfg),
                    [{"n": n, "nn": nn, "s": s} for n, nn, s in it["tags"]], None, None,
                    owner_id=it["owner_id"])
            store.log("info", f"[dry-run] 未写库 {len(batch)} 项"
                              f"（结果已保存，关闭 dry-run 后用\"补写标签\"落库）")
            return
        if cfg["tagging"].get("backup_before_write", True):
            self._backup_once(job, cfg)
        by_db = {}
        for it in batch:
            by_db.setdefault(it["db"], []).append(it)
        for db, items in by_db.items():
            try:
                _t0 = time.time()
                # 1) 先查哪些标签行已存在（用户手工建过的 → 复用，不新建）
                out = dbaccess.transport().exec_script(
                    db, dbaccess.build_existing_tags_script(items))
                _t1 = time.time()
                existing = {(owner, name): rid
                            for rid, owner, name in dbaccess.parse_tag_rows(out)}
                # 2) 补建缺失标签 + 建立关联 + 修正 count
                out = dbaccess.transport().exec_script(
                    db, dbaccess.build_write_script(items))
                _t2 = time.time()
                rows = dbaccess.parse_tag_rows(out)
                rows_map = {(owner, name): (rid, owner, name)
                            for rid, owner, name in rows}
                expected = {(it["owner_id"] or 0, name)
                            for it in items for name, _nn, _s in it["tags"]}
                missing = expected - set(rows_map)
                if missing:
                    sample = ", ".join(f"{owner}:{name}"
                                       for owner, name in sorted(missing)[:5])
                    raise dbaccess.DBError(
                        f"写库后未能确认 {len(missing)} 个标签行: {sample}")
                for it in items:
                    unit_rows = []
                    for name, nn, _s in it["tags"]:
                        key = (it["owner_id"] or 0, name)
                        if key in rows_map:
                            rid, owner, nm = rows_map[key]
                            # 不在 existing 里 = 本工具新建的行
                            unit_rows.append((rid, name, owner,
                                              key not in existing))
                    if unit_rows:
                        store.record_writes(db, it["unit_id"], unit_rows)
                for it in items:
                    store.mark_processed(
                        it["db"], it["unit_id"], "written", model_version(cfg),
                        [{"n": n, "nn": nn, "s": s} for n, nn, s in it["tags"]], None, None,
                        owner_id=it["owner_id"])
                job.tags_written += sum(len(i["tags"]) for i in items)
                reused = sum(1 for k in rows_map if k in existing)
                store.log("info", f"{db}: 写入 {len(items)} 项标签成功"
                                  f"（新建标签行 {len(rows) - reused}，"
                                  f"复用已有 {reused}，"
                                  f"查重 {_t1 - _t0:.2f}s，"
                                  f"写库+计数 {_t2 - _t1:.2f}s）")
            except Exception as e:
                # 任何写库异常都不击穿任务：结果保留为 write_error，稍后自动补写
                _t1 = locals().get("_t1", _t0)
                store.log("error", f"{db}: 写库失败（保留分析结果，稍后可重试写库）: {e}"
                                  f" [查重 {_t1 - _t0:.2f}s，"
                                  f"写库已进行 {time.time() - _t1:.2f}s]")
                for it in items:
                    store.mark_processed(
                        it["db"], it["unit_id"], "write_error",
                        model_version(cfg),
                        [{"n": n, "nn": nn, "s": s} for n, nn, s in it["tags"]], None, None,
                        owner_id=it["owner_id"])
                job.errors += len(items)

    def _backup_once(self, job: _Job, cfg: dict):
        if getattr(job, "_backup_done", False):
            return
        try:
            dest_dir = os.path.join(config.CONFIG_DIR, "backups")
            for db in dbaccess.list_databases():
                dest = dbaccess.transport().backup(
                    db, dest_dir, cfg["tagging"].get("backup_keep", 5))
                store.log("info", f"数据库已备份: {dest}")
            job._backup_done = True
        except Exception as e:
            store.log("warn", f"备份失败（继续写库）: {e}")
            job._backup_done = True   # 避免每批都重试备份阻塞任务

    def _invalidate_stale_pending(self, cfg: dict):
        """待写结果若来自旧模型/词表，转为待重分析（下次计划自动重跑）。"""
        cur = model_version(cfg)
        outdated = store.query(
            "SELECT db_name, unit_id FROM processed "
            "WHERE status IN ('analyzed','write_error') AND model_ver != ?", (cur,))
        if outdated:
            for r in outdated:
                store.execute(
                    "UPDATE processed SET status='error', retries=0 "
                    "WHERE db_name=? AND unit_id=?", (r["db_name"], r["unit_id"]))
            store.log("info", f"{len(outdated)} 条待写结果来自旧模型/词表，"
                              f"不写入库，将自动用当前模型重新分析")
        return len(outdated)

    def _flush_pending_records(self, job: _Job, cfg: dict):
        """扫描前先把上次遗留的"已分析未写库"记录补写完成（不重新推理）。

        若待写结果是用旧模型/词表/参数分析的（model_ver 不一致），
        则不写库——转为待重分析状态，本轮扫描自动用新模型重跑。
        """
        self._invalidate_stale_pending(cfg)
        rows = store.query(
            "SELECT db_name, unit_id, owner_id, tags FROM processed "
            "WHERE status IN ('analyzed','write_error')")
        if not rows:
            return
        store.log("info", f"发现 {len(rows)} 项上次遗留的待写库结果，先补写…")
        by_db = {}
        for r in rows:
            tags = [(t["n"], t["nn"], t.get("s"))
                    for t in json.loads(r["tags"] or "[]")]
            if not tags:
                store.mark_processed(r["db_name"], r["unit_id"], "empty",
                                     model_version(cfg), [], None, None,
                                     owner_id=r["owner_id"])
                continue
            by_db.setdefault(r["db_name"], []).append(
                {"db": r["db_name"], "unit_id": r["unit_id"],
                 "owner_id": r["owner_id"] or 0, "tags": tags})
        for db, items in by_db.items():
            if job.canceled.is_set():
                return
            self._flush(job, cfg, items)

    def _write_pending(self, job: _Job, cfg: dict):
        """把"已分析未写库"的项写入 synofoto（不重新推理）。"""
        self._invalidate_stale_pending(cfg)
        rows = store.query(
            "SELECT db_name, unit_id, owner_id, tags FROM processed "
            "WHERE status IN ('analyzed','write_error') ORDER BY db_name, unit_id")
        job.total = len(rows)
        if not rows:
            store.log("info", "没有待写库的项")
            return self._finish(job)
        job.phase = "write"
        by_db = {}
        for r in rows:
            tags = [(t["n"], t["nn"], t.get("s"))
                    for t in json.loads(r["tags"] or "[]")]
            if not tags:
                store.mark_processed(r["db_name"], r["unit_id"], "empty",
                                     model_version(cfg), [], None, None,
                                     owner_id=r["owner_id"])
                job.done += 1
                continue
            by_db.setdefault(r["db_name"], []).append(
                {"db": r["db_name"], "unit_id": r["unit_id"],
                 "owner_id": r["owner_id"] or 0, "tags": tags})
        for db, items in by_db.items():
            if job.canceled.is_set():
                break
            self._flush(job, cfg, items)
            job.done += len(items)
        self._finish(job)

    # ------------------------------------------------------ 记账/进度
    def _record(self, db, unit_id, status, model_ver, tags, ocr_text, rel,
                embed=None, bump=False, owner_id=0, engines=None,
                embed_model=None):
        store.mark_processed(db, unit_id, status, model_ver,
                             [{"n": n, "nn": nn, "s": s} for n, nn, s in tags],
                             ocr_text, rel, embed=embed, bump_retry=bump,
                             owner_id=owner_id, engines=engines,
                             embed_model=embed_model)

    def _progress(self, job: _Job, t0: float):
        if job.done > 0 and job.done % 10 == 0:
            rate = job.done / max(1e-9, time.time() - t0)
            job.eta_sec = int((job.total - job.done) / max(rate, 1e-9))

    def _finish(self, job: _Job, canceled: bool = False):
        job.phase = "done"
        job.finished = time.time()
        if canceled:
            store.log("info", f"任务已取消: {job.type}（已完成 {job.done}/{job.total}，"
                              f"下次扫描自动续接）")
        else:
            store.log("info", f"任务完成: {job.type} 处理 {job.done}/{job.total}，"
                              f"标签 {job.tags_written}，错误 {job.errors}")


PIPELINE = Pipeline()
