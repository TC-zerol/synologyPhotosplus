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
     "folder_name": "/2023", "owner_id": 1, "owner_name": "alice"},
    {"unit_id": 2, "filename": "invoice.jpg", "type": 0, "createtime": 101, "mtime": 101,
     "folder_name": "/docs", "owner_id": 1, "owner_name": "alice"},
]


class FakeTransport:
    """模拟 SSH→psql 传输。"""
    def __init__(self):
        self.fail_write = False

    def query_json(self, db, sql):
        if "pg_database" in sql:
            return [{"datname": "synofoto"}]
        if "FROM unit" in sql:
            return [{"id": u["unit_id"], "filename": u["filename"], "type": u["type"],
                     "createtime": u["createtime"], "mtime": u["mtime"],
                     "folder_name": u["folder_name"], "owner_id": u["owner_id"],
                     "owner_name": u["owner_name"]} for u in UNITS]
        raise AssertionError("unexpected query")

    def exec_script(self, db, script):
        if self.fail_write and "INSERT INTO many" in script:
            raise Exception("simulated db down")
        if "INSERT INTO general_tag" not in script:
            return "[]"
        names = []
        for owner, name in re.findall(r"\((\d+)::int, '((?:''|[^'])*)'::text", script):
            names.append((int(owner), name.replace("''", "'")))
        seen = {}
        for owner, name in names:
            seen[(owner, name)] = {
                "id": len(seen) + 7,
                "id_user": owner,
                "name": name,
            }
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
print()
print("=== 全链路测试全部通过 ===")
