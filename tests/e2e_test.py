"""端到端测试：模拟 synofoto 数据库，验证 扫描→断点→写库容错→补写→语义搜索→API。

本地运行（需已安装 requirements）：
    python tests/e2e_test.py
"""
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SP_CONFIG_DIR"] = tempfile.mkdtemp(prefix="spcfg_")

import numpy as np  # noqa: E402
import cv2  # noqa: E402

import app.config as config  # noqa: E402

root = tempfile.mkdtemp(prefix="spphotos_")
os.makedirs(os.path.join(root, "2023"), exist_ok=True)
os.makedirs(os.path.join(root, "docs"), exist_ok=True)
img1 = np.zeros((480, 640, 3), np.uint8)
cv2.rectangle(img1, (0, 300), (640, 480), (80, 140, 60), -1)
cv2.rectangle(img1, (0, 0), (640, 300), (200, 160, 90), -1)
cv2.circle(img1, (520, 80), 45, (60, 220, 255), -1)
cv2.imwrite(os.path.join(root, "2023", "scene.jpg"), img1)
img2 = np.full((300, 700, 3), 255, np.uint8)
cv2.putText(img2, "INVOICE NO.20260831", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (20, 20, 20), 3)
cv2.putText(img2, "TOTAL: 888 CNY", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 3)
cv2.imwrite(os.path.join(root, "docs", "invoice.jpg"), img2)

UNITS = [
    {"unit_id": 1, "filename": "scene.jpg", "type": 0, "createtime": 100, "mtime": 100,
     "folder_name": "/2023", "owner_id": 1, "owner_name": "alice", "id_geocoding": 10},
    {"unit_id": 2, "filename": "invoice.jpg", "type": 0, "createtime": 101, "mtime": 101,
     "folder_name": "/docs", "owner_id": 1, "owner_name": "alice", "id_geocoding": 0},
]

# id_geocoding=10 的多语言地点行：英 / 简 / 繁，验证"自动优选简体中文"
GEO_ROWS = [
    {"id_geocoding": 10, "lang": 0, "country": "China", "province": "Shanghai",
     "city": "Shanghai", "town": "Pudong New Area"},
    {"id_geocoding": 10, "lang": 1, "country": "中国", "province": "上海市",
     "city": "上海市", "town": "浦东新区"},
    {"id_geocoding": 10, "lang": 2, "country": "中國", "province": "上海市",
     "city": "上海市", "town": "浦東新區"},
]

_SCHEMA_COLS = {
    "unit": ["id", "filename", "type", "createtime", "mtime", "id_folder",
             "id_geocoding"],
    "folder": ["id", "name", "id_user"],
    "user_info": ["id", "name"],
    "geocoding_info": ["id_geocoding", "lang", "country", "province", "city",
                       "town"],
}


class FakeTransport:
    """模拟 SSH→psql 传输。"""
    def __init__(self):
        self.fail_write = False
        self.scripts = []          # 记录 exec_script 收到的 SQL（测试断言用）
        self.tag_ids = {}          # (owner,name)->id 持久分配（模拟真实库行 id 稳定）

    def query_json(self, db, sql):
        if "pg_database" in sql:
            return [{"datname": "synofoto"}]
        if "information_schema" in sql:
            return [{"table_name": t, "column_name": c}
                    for t, cs in _SCHEMA_COLS.items() for c in cs]
        if "FROM user_info" in sql:
            return [{"id": 1, "name": "alice"}]
        if "FROM geocoding_info" in sql:
            return GEO_ROWS
        if "FROM unit" in sql:
            return [{"id": u["unit_id"], "filename": u["filename"], "type": u["type"],
                     "createtime": u["createtime"], "mtime": u["mtime"],
                     "folder_name": u["folder_name"], "owner_id": u["owner_id"],
                     "owner_name": u["owner_name"],
                     "id_geocoding": u.get("id_geocoding", 0)} for u in UNITS]
        raise AssertionError("unexpected query: " + sql[:120])

    def exec_script(self, db, script):
        self.scripts.append(script)
        if self.fail_write and "INSERT INTO many" in script:
            raise Exception("simulated db down")
        if "INSERT INTO general_tag" not in script:
            return "[]"
        names = []
        for owner, name in re.findall(r"\((\d+)::int, '((?:''|[^'])*)'::text", script):
            names.append((int(owner), name.replace("''", "'")))
        seen = {}
        for owner, name in names:
            key = (owner, name)
            if key not in self.tag_ids:
                self.tag_ids[key] = len(self.tag_ids) + 7
            seen[key] = {"id": self.tag_ids[key], "id_user": owner,
                         "name": name}
        return json.dumps(list(seen.values()), ensure_ascii=False)

    def list_databases(self):
        return ["synofoto"]

    def backup(self, db, d, k):
        return f"{d}/{db}.sql.gz"

    def close(self):
        pass


import app.dbaccess as dbaccess  # noqa: E402

fake = FakeTransport()
dbaccess._transport = fake
dbaccess._transport_cfg_key = ("ssh", repr(sorted(config.load()["db"]["ssh"].items())),
                               repr(sorted(config.load()["db"]["tcp"].items())))
dbaccess.list_databases = lambda: ["synofoto"]
_script = dbaccess.build_write_script([
    {"unit_id": 1, "owner_id": 1, "tags": [("测试", "测试 test", 1.0)]}
])
assert _script.strip().splitlines()[-1].startswith("SELECT COALESCE"), _script
config.save({"mounts": [{"name": "test", "path": root}],
             "scan": {"batch_size": 1, "poll_interval_min": 0},
             "tagging": {"backup_before_write": False},
             "ocr": {"confidence": 0.5}})

import app.store as store  # noqa: E402
from app.pipeline import PIPELINE  # noqa: E402

PIPELINE.start()


def run_and_wait(jtype, timeout=300):
    ts_before = time.time()
    PIPELINE.enqueue(jtype)
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = PIPELINE.status()
        if st["running"] is None and st["last"] and st["last"]["started"] > ts_before - 0.5:
            return st["last"]
        time.sleep(0.4)
    raise TimeoutError(jtype)


last = run_and_wait("incremental")
print("① 首次扫描:", {k: last[k] for k in ("done", "total", "tags_written", "errors", "error")})
assert (last["total"], last["done"], last["errors"], last["error"]) == (2, 2, 0, None), str(last)
rows = {r["unit_id"]: r for r in store.query(
    "SELECT unit_id, status, tags, ocr_text, rel_path FROM processed")}
assert rows[1]["status"] == "written" and rows[2]["status"] == "written", str(rows)
assert "20260831" in rows[2]["ocr_text"], repr(rows[2]["ocr_text"])
assert any(t["n"] == "发票" for t in json.loads(rows[2]["tags"])), str(rows[2]["tags"])
# 地点标签：geocoding 多语言行自动优选简体中文，英文进 normalized_name
tags1 = {t["n"]: t["nn"] for t in json.loads(rows[1]["tags"])}
assert "浦东新区" in tags1 and "中国" in tags1, str(tags1)
assert not any(n in tags1 for n in ("Pudong New Area", "浦東新區", "中國")), str(tags1)
assert "pudong" in tags1["浦东新区"], tags1["浦东新区"]
# 相机型号标签已下线
assert not any("iPhone" in n or "Apple" in n for n in tags1), str(tags1)
print("   地点标签(简体):", [n for n in tags1 if n in ("中国", "上海市", "浦东新区")])
print("   OCR:", repr(rows[2]["ocr_text"]))
print("   标签:", [t["n"] for t in json.loads(rows[2]["tags"])][:6])
assert store.query("SELECT COUNT(*) c FROM embeddings")[0]["c"] == 2

last = run_and_wait("incremental")
print("② 断点续扫: 待处理 =", last["total"])
assert last["total"] == 0, str(last)

store.execute("UPDATE processed SET status='analyzed' WHERE unit_id IN (1,2)")
fake.fail_write = True
run_and_wait("write_pending")
rows2 = store.query("SELECT unit_id, status FROM processed ORDER BY unit_id")
print("③ 写库故障后:", [(r["unit_id"], r["status"]) for r in rows2])
assert all(r["status"] == "write_error" for r in rows2), str(rows2)

fake.fail_write = False
run_and_wait("write_pending")
rows3 = store.query("SELECT unit_id, status, ocr_text FROM processed ORDER BY unit_id")
print("④ 故障恢复后:", [(r["unit_id"], r["status"]) for r in rows3])
assert all(r["status"] == "written" for r in rows3), str(rows3)
assert "20260831" in [r for r in rows3 if r["unit_id"] == 2][0]["ocr_text"], "补写后 ocr_text 丢失"

from app.main import _semantic_search  # noqa: E402
hits = _semantic_search("一张发票收据的照片", 10)
print("⑤ 语义搜索:", [(h["rel_path"], h["score"]) for h in hits])
assert hits and hits[0]["rel_path"].endswith("invoice.jpg"), str(hits)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
c = TestClient(app)
r = c.get("/api/bootstrap")
assert r.status_code == 200, r.text
print("⑥ API bootstrap OK")
r = c.get("/api/search?q=888&mode=keyword")
print("   关键词搜索:", len(r.json()["items"]), "条命中")
assert len(r.json()["items"]) == 1, r.json()
assert c.get("/api/logs").status_code == 200
assert "Synology Photos+" in c.get("/").text
print("   前端页面 OK")

from app.infer import registry  # noqa: E402
registry.unload_all()
print("⑦ 模型卸载后 loaded:", registry.loaded())
# ---------------------------------------------------------------- 回归：三个修复
print()
print("⑧ 路径歧义：多用户同名文件按属主消歧")
import app.matcher as matcher_mod  # noqa: E402
mroot = tempfile.mkdtemp(prefix="spmatch_")
pa = os.path.join(mroot, "homes", "alice", "Photos", "MobileBackup",
                  "iPhone", "2025", "06")
pb = os.path.join(mroot, "homes", "bob", "Photos", "MobileBackup",
                  "iPhone", "2025", "06")
os.makedirs(pa)
os.makedirs(pb)
fa = os.path.join(pa, "IMG_1343.HEIC")
fb = os.path.join(pb, "IMG_1343.HEIC")
open(fa, "wb").write(b"a")
open(fb, "wb").write(b"b")
m2 = matcher_mod.Matcher()
m2.build_fs_index([mroot], [], {".heic"})
assert m2.match("MobileBackup/iPhone/2025/06", "IMG_1343.HEIC", "alice") == fa
assert m2.match("MobileBackup/iPhone/2025/06", "IMG_1343.HEIC", "bob") == fb
# 无属主信息 → 仍歧义跳过（绝不猜），与旧行为一致
assert m2.match("MobileBackup/iPhone/2025/06", "IMG_1343.HEIC", "") is None
assert m2.match("MobileBackup/iPhone/2025/06", "NONE.HEIC", "alice") == ""
print("   alice/bob/无属主/不存在 四种场景 OK")

print("⑨ _exif_tags：相机标签下线 + 地点简体双语 normalized")
from app.pipeline import _exif_tags  # noqa: E402
_tags = []
_exif_tags({"createtime": 1700000000, "camera": "Apple iPhone 15 Pro Max",
            "geo_zh": {"country": "中国", "province": "广东省",
                       "city": "深圳市", "town": ""},
            "geo_en": {"country": "China", "province": "Guangdong",
                       "city": "Shenzhen", "town": ""}},
           config.load(), _tags)
_names = {t[0] for t in _tags}
assert not any("iPhone" in n or "Apple" in n for n in _names), _names
assert {"深圳市", "广东省", "中国", "2023年", "11月", "秋季"} <= _names, _names
_nn = {t[0]: t[1] for t in _tags}
assert _nn["深圳市"] == "深圳市 shenzhen", _nn["深圳市"]
print("   OK:", sorted(_names))

print("⑩ 个人空间库缺 user_info 时 owner_name 全局回填")
import app.geo as geo_mod  # noqa: E402
_orig = (dbaccess.list_databases, dbaccess.list_users,
         dbaccess.enumerate_units, geo_mod.attach_geo)
try:
    dbaccess.list_databases = lambda: ["synofoto", "synofoto_personal_2"]
    dbaccess.list_users = lambda db: ([{"id": 2, "name": "bob"}]
                                      if db == "synofoto" else [])
    dbaccess.enumerate_units = lambda db: [
        {"unit_id": 1, "filename": "IMG_1343.HEIC", "type": 0, "createtime": 1,
         "mtime": 1, "folder_name": "MobileBackup/iPhone", "owner_id": 2,
         "owner_name": "", "id_geocoding": 0}]
    geo_mod.attach_geo = lambda *a, **k: 0
    _dbs, _units = PIPELINE._enumerate(config.load())
    assert _units[0][1]["owner_name"] == "bob", _units
    print("   synofoto_personal_2 的 owner_name 回填为:",
          _units[0][1]["owner_name"])
finally:
    (dbaccess.list_databases, dbaccess.list_users,
     dbaccess.enumerate_units, geo_mod.attach_geo) = _orig


print()
print("=== 全链路测试全部通过 ===")

