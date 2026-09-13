"""本地 SQLite 记账库：处理状态、写库关系备份、水印、事件日志。

除分析结果外，最重要的是 tag_rows/tag_defs：
记录本工具在 synofoto 各库写入的 general_tag 行和关联关系，
用于"重新分析并替换"、卸载清理和 count 修正。
"""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
  db_name TEXT NOT NULL,
  unit_id INTEGER NOT NULL,
  status TEXT NOT NULL,            -- analyzed(待写库) | written | empty | write_error | error
  model_ver TEXT,
  tags TEXT,                       -- JSON [{n:name, nn:normalized}]
  ocr_text TEXT,
  rel_path TEXT,
  mtime REAL,
  processed_at REAL,
  retries INTEGER NOT NULL DEFAULT 0,
  owner_id INTEGER NOT NULL DEFAULT 0,
  engines TEXT,                    -- JSON {"detect":bool,"clip":bool,"ocr":bool} 各引擎成功与否
  PRIMARY KEY (db_name, unit_id)
);
CREATE TABLE IF NOT EXISTS embeddings (
  db_name TEXT NOT NULL,
  unit_id INTEGER NOT NULL,
  vec BLOB NOT NULL,
  model TEXT,
  PRIMARY KEY (db_name, unit_id)
);
CREATE TABLE IF NOT EXISTS tag_defs (
  db_name TEXT NOT NULL,
  tag_row_id INTEGER NOT NULL,
  name TEXT,
  id_user INTEGER,
  created INTEGER NOT NULL DEFAULT 0,   -- 是否由本工具创建
  PRIMARY KEY (db_name, tag_row_id)
);
CREATE TABLE IF NOT EXISTS tag_rows (
  db_name TEXT NOT NULL,
  unit_id INTEGER NOT NULL,
  tag_row_id INTEGER NOT NULL,
  PRIMARY KEY (db_name, unit_id, tag_row_id)
);
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY, v TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, level TEXT, msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_processed_ocr ON processed(ocr_text);
"""


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.path.join(config.CONFIG_DIR, "tagger.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
        # 旧库迁移：补 engines 列
        cols = {r["name"] for r in _conn.execute("PRAGMA table_info(processed)")}
        if "engines" not in cols:
            _conn.execute("ALTER TABLE processed ADD COLUMN engines TEXT")
        cols = {r["name"] for r in _conn.execute("PRAGMA table_info(embeddings)")}
        if "model" not in cols:
            _conn.execute("ALTER TABLE embeddings ADD COLUMN model TEXT")
        _conn.commit()
    return _conn


def execute(sql: str, params=()) -> int:
    with _lock:
        c = _get()
        cur = c.execute(sql, params)
        c.commit()
        return cur.lastrowid


def query(sql: str, params=()):
    with _lock:
        c = _get()
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def log(level: str, msg: str):
    rid = execute("INSERT INTO events (ts, level, msg) VALUES (?,?,?)",
                  (time.time(), level, msg[:2000]))
    # 每 500 条顺手修剪一次，events 表不无限增长
    if rid and rid % 500 == 0:
        execute("DELETE FROM events WHERE id <= "
                "(SELECT MAX(id) - 5000 FROM events)")


def prune_events(keep: int = 5000):
    execute("DELETE FROM events WHERE id <= (SELECT MAX(id) - ? FROM events)",
            (keep,))


def get_setting(key: str, default=None):
    row = query_one("SELECT v FROM settings WHERE k=?", (key,))
    return row["v"] if row else default


def set_setting(key: str, value: str):
    execute("INSERT INTO settings (k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


def is_processed(db_name: str, unit_id: int) -> bool:
    return query_one(
        "SELECT 1 AS x FROM processed WHERE db_name=? AND unit_id=?",
        (db_name, unit_id)) is not None


def mark_processed(db_name: str, unit_id: int, status: str, model_ver: str,
                   tags: list, ocr_text: str, rel_path: str,
                   embed=None, bump_retry: bool = False, owner_id: int = 0,
                   engines: dict = None, embed_model: str = None):
    retries = 0
    if bump_retry:
        row = query_one("SELECT retries FROM processed WHERE db_name=? AND unit_id=?",
                        (db_name, unit_id))
        retries = (row["retries"] + 1) if row else 1
    # 传 None 表示"保留已有值"（写库阶段不覆盖分析结果）
    if ocr_text is None or rel_path is None or engines is None:
        row = query_one("SELECT ocr_text, rel_path, engines FROM processed "
                        "WHERE db_name=? AND unit_id=?", (db_name, unit_id))
        if row:
            if ocr_text is None:
                ocr_text = row["ocr_text"]
            if rel_path is None:
                rel_path = row["rel_path"]
            if engines is None:
                engines = row["engines"]
    import json as _json
    if isinstance(engines, dict):
        engines = _json.dumps(engines, ensure_ascii=False)
    execute(
        "INSERT INTO processed (db_name, unit_id, status, model_ver, tags, "
        "ocr_text, rel_path, mtime, processed_at, retries, owner_id, engines) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(db_name, unit_id) DO UPDATE SET status=excluded.status, "
        "model_ver=excluded.model_ver, tags=excluded.tags, "
        "ocr_text=excluded.ocr_text, rel_path=excluded.rel_path, "
        "mtime=excluded.mtime, processed_at=excluded.processed_at, "
        "retries=excluded.retries, owner_id=excluded.owner_id, "
        "engines=excluded.engines",
        (db_name, unit_id, status, model_ver,
         json.dumps(tags, ensure_ascii=False),
         (ocr_text or "")[: config.load()["ocr"]["max_text_len"]],
         rel_path or "", time.time(), time.time(), retries, owner_id,
         engines if engines else None))
    if embed is not None:
        with _lock:
            _emb_cache["count"] = None   # 使语义搜索的向量缓存失效
        execute("INSERT INTO embeddings (db_name, unit_id, vec, model) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(db_name, unit_id) DO UPDATE SET vec=excluded.vec, "
                "model=excluded.model",
                (db_name, unit_id, embed, embed_model))


def save_embedding(db_name: str, unit_id: int, vec_bytes: bytes,
                   model: str = None):
    with _lock:
        _emb_cache["count"] = None
    execute("INSERT INTO embeddings (db_name, unit_id, vec, model) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(db_name, unit_id) DO UPDATE SET vec=excluded.vec, "
            "model=excluded.model",
            (db_name, unit_id, vec_bytes, model))


_emb_cache = {"count": None, "data": None}


def load_embeddings(db_name: str = None, model: str = None):
    """返回 [(db_name, unit_id, np.ndarray float32)]；语义搜索用。

    全量模式（db_name=None 且不过滤模型）带缓存：总数不变时直接复用矩阵。
    指定 model 时只取该模型的向量——不同 CLIP 模型的向量空间不兼容。
    """
    import numpy as np
    if db_name is None and not model:
        total = query_one("SELECT COUNT(*) AS c FROM embeddings")["c"]
        with _lock:
            if _emb_cache["count"] == total and _emb_cache["data"] is not None:
                return _emb_cache["data"]
        rows = query("SELECT db_name, unit_id, vec FROM embeddings")
        data = [(r["db_name"], r["unit_id"], np.frombuffer(r["vec"], np.float32))
                for r in rows]
        with _lock:
            _emb_cache["count"] = total
            _emb_cache["data"] = data
        return data
    sql = "SELECT db_name, unit_id, vec FROM embeddings WHERE 1=1"
    params = []
    if db_name:
        sql += " AND db_name=?"; params.append(db_name)
    if model:
        sql += " AND model=?"; params.append(model)
    rows = query(sql, params)
    return [(r["db_name"], r["unit_id"], np.frombuffer(r["vec"], np.float32))
            for r in rows]


def record_writes(db_name: str, unit_id: int, rows):
    """rows: [(row_id, name, id_user, created)]，created=是否本工具新建的标签行。"""
    for row_id, name, id_user, created in rows:
        execute(
            "INSERT INTO tag_defs (db_name, tag_row_id, name, id_user, created) "
            "VALUES (?,?,?,?,?) ON CONFLICT(db_name, tag_row_id) DO UPDATE "
            "SET created = MAX(created, excluded.created)",
            (db_name, row_id, name, id_user, 1 if created else 0))
        execute("INSERT OR IGNORE INTO tag_rows (db_name, unit_id, tag_row_id) "
                "VALUES (?,?,?)", (db_name, unit_id, row_id))


def our_tag_rows(db_name: str) -> list:
    return query("SELECT tag_row_id FROM tag_defs WHERE db_name=?", (db_name,))


def our_relations(db_name: str, unit_ids=None) -> list:
    if unit_ids is None:
        return query("SELECT unit_id, tag_row_id FROM tag_rows WHERE db_name=?",
                     (db_name,))
    ph = ",".join("?" * len(unit_ids))
    return query(f"SELECT unit_id, tag_row_id FROM tag_rows WHERE db_name=? "
                 f"AND unit_id IN ({ph})", [db_name, *unit_ids])


def clear_relations(db_name: str, unit_ids=None):
    if unit_ids is None:
        execute("DELETE FROM tag_rows WHERE db_name=?", (db_name,))
    else:
        ph = ",".join("?" * len(unit_ids))
        execute(f"DELETE FROM tag_rows WHERE db_name=? AND unit_id IN ({ph})",
                [db_name, *unit_ids])


def stats() -> dict:
    total = query_one("SELECT COUNT(*) AS c FROM processed")["c"]
    written = query_one(
        "SELECT COUNT(*) AS c FROM processed WHERE status='written'")["c"]
    empty = query_one(
        "SELECT COUNT(*) AS c FROM processed WHERE status='empty'")["c"]
    pending = query_one(
        "SELECT COUNT(*) AS c FROM processed WHERE status IN ('analyzed','write_error')")["c"]
    err = query_one("SELECT COUNT(*) AS c FROM processed WHERE status='error'")["c"]
    exhausted = query_one(
        "SELECT COUNT(*) AS c FROM processed WHERE status='error' "
        "AND retries >= 3")["c"]
    tagged = query_one("SELECT COUNT(*) AS c FROM tag_rows")["c"]
    linked = query_one(
        "SELECT COUNT(DISTINCT db_name || ':' || unit_id) AS c FROM tag_rows")["c"]
    max_per = query_one(
        "SELECT MAX(c) AS m FROM (SELECT COUNT(*) AS c FROM tag_rows "
        "GROUP BY db_name, unit_id)")["m"] or 0
    vecs = query_one("SELECT COUNT(*) AS c FROM embeddings")["c"]
    return {"processed": total, "written": written, "empty": empty,
            "pending_write": pending, "error": err, "error_exhausted": exhausted,
            "tag_links": tagged, "tag_linked_units": linked,
            "tag_max_per_photo": max_per, "embeddings": vecs}


def stale_pending_count(current_model_ver: str) -> int:
    """待写结果中来自旧模型/词表的条数（会自动转重新分析，不需要用户操作）。"""
    return query_one(
        "SELECT COUNT(*) AS c FROM processed "
        "WHERE status IN ('analyzed','write_error') AND model_ver != ?",
        (current_model_ver,))["c"]


def reset_error_retries() -> int:
    """把失败项的重试计数清零（模型修复后让它们重新进入扫描）。"""
    n = query_one("SELECT COUNT(*) AS c FROM processed WHERE status='error'")["c"]
    execute("UPDATE processed SET retries=0 WHERE status='error'")
    return n


def stale_count(current_model_ver: str) -> int:
    """用过期模型/词表/配置分析的照片数（与当前版本指纹不一致）。"""
    return query_one(
        "SELECT COUNT(*) AS c FROM processed "
        "WHERE status IN ('written','empty') AND model_ver != ?",
        (current_model_ver,))["c"]


def top_tags(limit=50) -> list:
    # 按 name 聚合：同名标签在不同用户下是不同行（按用户隔离的设计），
    # 词云显示时合并计数，避免"亲子游 1103 / 亲子游 759"的分裂观感
    return query(
        "SELECT name, SUM(cnt) AS count FROM ("
        "SELECT t.name AS name, COUNT(*) AS cnt FROM tag_rows r "
        "JOIN tag_defs t ON t.db_name=r.db_name AND t.tag_row_id=r.tag_row_id "
        "GROUP BY t.db_name, r.tag_row_id) "
        "GROUP BY name ORDER BY count DESC LIMIT ?", (limit,))


def recent(limit=50) -> list:
    return query("SELECT db_name, unit_id, status, tags, ocr_text, rel_path, "
                 "processed_at FROM processed ORDER BY processed_at DESC LIMIT ?",
                 (limit,))
