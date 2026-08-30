"""Synology Photos PostgreSQL 访问层。

两种传输（config.db.transport）：
- ssh（推荐，默认）：容器通过 SSH 连 NAS，执行 `sudo -u postgres psql`，
  不改动 DSM 的 pg_hba/listen_addresses，零侵入。
- tcp：直连 PostgreSQL（需自行开启远程访问并建角色，即 synology-tagger 的老路）。

核心表与 eleonne/synology-tagger 已验证的路线一致：
- 库：synofoto（团队空间）与 synofoto_personal_<N>（个人空间）
- 表：unit / folder / user_info / general_tag / many_unit_has_many_general_tag
"""
import gzip
import json
import os
import re
import shlex
import threading
import time

from . import config, store

ANSI = re.compile(r"\x1b[^m]*m")

PSQL_CANDIDATES = [
    "psql",
    "/usr/local/pgsql/bin/psql",
    "/usr/local/bin/psql",
    "/usr/bin/psql",
    "/var/packages/PostgreSQL/target/usr/bin/psql",
    "/volume1/@appstore/PostgreSQL/target/usr/bin/psql",
]
PG_DUMP_CANDIDATES = [p.replace("psql", "pg_dump") for p in PSQL_CANDIDATES]

PSQL_FLAGS = "-X -q -t -A -v ON_ERROR_STOP=1"


class DBError(Exception):
    pass


def _json_query_sql(sql: str) -> str:
    inner = sql.rstrip().rstrip(";")
    return ("SELECT COALESCE(json_agg(row_to_json(t))::text, '[]') "
            f"FROM ({inner}) t;")


# ---------------------------------------------------------------- SSH 传输

class SSHTransport:
    def __init__(self, cfg: dict):
        self.cfg = cfg["db"]["ssh"]
        self._client = None
        self._lock = threading.Lock()
        self._psql_path = None
        self._dump_path = None

    def _connect(self):
        if self._client is not None:
            try:
                t = self._client.get_transport()
                if t and t.is_active():
                    return
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        import paramiko
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(self.cfg["host"], port=int(self.cfg["port"] or 22),
                    username=self.cfg["user"], password=self.cfg["password"],
                    timeout=15, banner_timeout=15, auth_timeout=15,
                    allow_agent=False, look_for_keys=False)
        self._client = cli

    def _prep(self, cmd: str, sudo: bool):
        """组装实际命令与 sudo 密码前缀。返回 (real_cmd, pw_prefix)。"""
        if sudo and self.cfg.get("use_sudo", True):
            if self.cfg["user"] == "root":
                return f"sudo -n -u postgres {cmd}", ""
            return (f"sudo -S -p '' -u postgres {cmd}",
                    self.cfg["password"] + "\n")
        return cmd, ""

    def _exec(self, cmd: str, stdin_text, timeout: int, sudo: bool = False,
              chunk_cb=None) -> str:
        """执行命令，收集 stdout 为文本；chunk_cb 非空时同时流式回调每个块。"""
        real, pw = self._prep(cmd, sudo)
        with self._lock:
            self._connect()
            try:
                chan = self._client.get_transport().open_session()
                chan.settimeout(timeout)
                chan.exec_command(real)
                stdin_data = (pw + stdin_text) if pw else stdin_text
                if stdin_data:
                    chan.sendall(stdin_data.encode("utf-8"))
                chan.shutdown_write()
                out, err = bytearray(), bytearray()
                while True:
                    got = False
                    if chan.recv_ready():
                        data = chan.recv(1 << 16)
                        out += data
                        if chunk_cb:
                            chunk_cb(data)
                        got = True
                    if chan.recv_stderr_ready():
                        err += chan.recv_stderr(1 << 16)
                        got = True
                    if not got:
                        if chan.exit_status_ready() and not chan.recv_ready() \
                                and not chan.recv_stderr_ready():
                            break
                        time.sleep(0.02)
                code = chan.recv_exit_status()
                text = ANSI.sub("", out.decode("utf-8", "replace"))
                if code != 0:
                    etext = ANSI.sub("", err.decode("utf-8", "replace")).strip()
                    raise DBError(f"命令失败(exit={code}): {etext[-500:] or text[-200:]}")
                return text
            except DBError:
                raise
            except Exception as e:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                raise DBError(f"SSH 执行失败: {e}") from e

    def run(self, cmd: str, input_text: str = None, timeout: int = 120,
            sudo: bool = False) -> str:
        """执行命令并返回 stdout 文本。"""
        return self._exec(cmd, input_text, timeout, sudo=sudo)

    def run_stream(self, cmd: str, sink, timeout: int = 600,
                   sudo: bool = False):
        """执行命令，stdout 按块流式交给 sink(bytes)——用于 pg_dump 直写磁盘。"""
        real, pw = self._prep(cmd, sudo)
        with self._lock:
            self._connect()
        try:
            chan = self._client.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(real)
            if pw:
                chan.sendall(pw.encode("utf-8"))
            chan.shutdown_write()
            while True:
                got = False
                if chan.recv_ready():
                    sink(chan.recv(1 << 16)); got = True
                if chan.recv_stderr_ready():
                    chan.recv_stderr(1 << 16); got = True
                if not got:
                    if chan.exit_status_ready() and not chan.recv_ready() \
                            and not chan.recv_stderr_ready():
                        break
                    time.sleep(0.02)
            code = chan.recv_exit_status()
            if code != 0:
                raise DBError(f"命令失败(exit={code})，输出可能不完整")
        except DBError:
            raise
        except Exception as e:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            raise DBError(f"SSH 流式执行失败: {e}") from e

    def _probe_bin(self, candidates, which: str) -> str:
        cache = self._psql_path if which == "psql" else self._dump_path
        if cache:
            return cache
        probe = ("for d in " + " ".join(shlex.quote(c) for c in candidates) + "; do "
                 "if [ \"$d\" = psql ] || [ \"$d\" = pg_dump ]; then command -v $d "
                 "&& exit 0; elif [ -x \"$d\" ]; then echo \"$d\" && exit 0; fi; "
                 "done; exit 1;")
        out = self.run(probe, timeout=20).strip().splitlines()
        if not out:
            raise DBError(f"NAS 上未找到 {which}（PostgreSQL 工具）")
        path = out[-1].strip()
        if which == "psql":
            self._psql_path = path
        else:
            self._dump_path = path
        return path

    def _psql_cmd(self, db: str) -> str:
        psql = self._probe_bin(PSQL_CANDIDATES, "psql")
        return f"{shlex.quote(psql)} {PSQL_FLAGS} -d {shlex.quote(db)}"

    def exec_script(self, db: str, script: str) -> str:
        return self.run(self._psql_cmd(db), input_text=script, timeout=600,
                        sudo=True)

    def query_json(self, db: str, sql: str):
        out = self.exec_script(db, _json_query_sql(sql)).strip()
        if not out:
            return []
        try:
            return json.loads(out.splitlines()[-1])
        except json.JSONDecodeError as e:
            raise DBError(f"查询结果解析失败: {out[:200]}") from e

    def list_databases(self) -> list:
        rows = self.query_json(
            "postgres",
            "SELECT datname FROM pg_database WHERE datname LIKE 'synofoto%' "
            "ORDER BY datname")
        return [r["datname"] for r in rows
                if r["datname"] == "synofoto"
                or re.match(r"^synofoto_personal_\d+$", r["datname"])]

    def backup(self, db: str, dest_dir: str, keep: int = 5) -> str:
        """pg_dump 流式直写 gzip 文件（不占内存），返回备份文件路径。"""
        pg_dump = self._probe_bin(PG_DUMP_CANDIDATES, "pg_dump")
        os.makedirs(dest_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(dest_dir, f"{db}_{ts}.sql.gz")
        with gzip.open(dest, "wb", compresslevel=6) as f:
            self.run_stream(f"{shlex.quote(pg_dump)} -d {shlex.quote(db)}",
                            f.write, timeout=3600, sudo=True)
        import glob
        olds = sorted(glob.glob(os.path.join(dest_dir, f"{db}_*.sql.gz")))
        for old in olds[:-keep] if keep > 0 else []:
            try:
                os.remove(old)
            except OSError:
                pass
        return dest

    def restore(self, db: str, gz_path: str, timeout: int = 3600) -> bool:
        """把本地 gzip 备份流式灌回指定库（gunzip 内容经 SSH 直送 psql）。"""
        psql = self._probe_bin(PSQL_CANDIDATES, "psql")
        cmd = f"{shlex.quote(psql)} {PSQL_FLAGS} -d {shlex.quote(db)}"
        real, pw = self._prep(cmd, sudo=True)
        import gzip as _gzip
        with self._lock:
            self._connect()
        try:
            chan = self._client.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(real)
            if pw:
                chan.sendall(pw.encode("utf-8"))
            with _gzip.open(gz_path, "rb") as f:
                while True:
                    chunk = f.read(1 << 16)
                    if not chunk:
                        break
                    chan.sendall(chunk)
            chan.shutdown_write()
            err = bytearray()
            while True:
                got = False
                if chan.recv_ready():
                    chan.recv(1 << 16); got = True
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(1 << 16); got = True
                if not got:
                    if chan.exit_status_ready() and not chan.recv_ready() \
                            and not chan.recv_stderr_ready():
                        break
                    time.sleep(0.02)
            code = chan.recv_exit_status()
            if code != 0:
                etext = ANSI.sub("", err.decode("utf-8", "replace")).strip()
                raise DBError(f"还原失败(exit={code}): {etext[-500:]}")
            return True
        except DBError:
            raise
        except Exception as e:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            raise DBError(f"SSH 还原失败: {e}") from e

    def close(self):
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None


# ---------------------------------------------------------------- TCP 传输

class TCPTransport:
    def __init__(self, cfg: dict):
        self.cfg = cfg["db"]["tcp"]

    def _conn(self, db: str):
        import psycopg2
        return psycopg2.connect(host=self.cfg["host"], port=self.cfg["port"],
                                user=self.cfg["user"],
                                password=self.cfg["password"], dbname=db,
                                connect_timeout=10)

    def exec_script(self, db: str, script: str) -> str:
        with self._conn(db) as conn:
            with conn.cursor() as cur:
                cur.execute(script)
                rows = cur.fetchall() if cur.description else []
        return "\n".join(json.dumps(r[0]) if len(r) == 1 else "\t".join(
            "" if v is None else str(v) for v in r) for r in rows)

    def query_json(self, db: str, sql: str):
        with self._conn(db) as conn:
            with conn.cursor() as cur:
                cur.execute(_json_query_sql(sql))
                row = cur.fetchone()
        return json.loads(row[0]) if row and row[0] else []

    def list_databases(self) -> list:
        rows = self.query_json(
            "postgres",
            "SELECT datname FROM pg_database WHERE datname LIKE 'synofoto%' "
            "ORDER BY datname")
        return [r["datname"] for r in rows
                if r["datname"] == "synofoto"
                or re.match(r"^synofoto_personal_\d+$", r["datname"])]

    def backup(self, db: str, dest_dir: str, keep: int = 5) -> str:
        raise DBError("TCP 模式不支持自动备份，请切换 SSH 模式或在 NAS 上手动 pg_dump")

    def restore(self, db: str, gz_path: str, timeout: int = 3600) -> bool:
        raise DBError("TCP 模式不支持页面还原，请切换 SSH 模式或手动执行 gunzip | psql")

    def close(self):
        pass


# ---------------------------------------------------------------- 门面

_transport = None
_transport_cfg_key = None


def transport(cfg=None):
    global _transport, _transport_cfg_key
    cfg = cfg or config.load()
    key = (cfg["db"]["transport"], repr(sorted(cfg["db"]["ssh"].items())),
           repr(sorted(cfg["db"]["tcp"].items())))
    if _transport is None or _transport_cfg_key != key:
        if _transport:
            _transport.close()
        _transport = (SSHTransport(cfg) if cfg["db"]["transport"] == "ssh"
                      else TCPTransport(cfg))
        _transport_cfg_key = key
    return _transport


def close_transport():
    global _transport
    if _transport:
        _transport.close()
        _transport = None


def _escape(s: str) -> str:
    # PG 默认 standard_conforming_strings=on，反斜杠是字面量；只需处理单引号
    return s.replace("'", "''")


ENUM_SQL = ("SELECT u.id, u.filename, u.type, COALESCE(u.createtime, 0) AS "
            "createtime, COALESCE(u.mtime, 0) AS mtime, f.name AS folder_name, "
            "f.id_user AS owner_id, COALESCE(ui.name, '') AS owner_name "
            "FROM unit u JOIN folder f ON f.id = u.id_folder "
            "LEFT JOIN user_info ui ON ui.id = f.id_user ORDER BY u.id")

# 极老版本无 user_info 表时的降级查询（不引用 ui）
ENUM_SQL_NOUI = ("SELECT u.id, u.filename, u.type, COALESCE(u.createtime, 0) AS "
                 "createtime, COALESCE(u.mtime, 0) AS mtime, f.name AS folder_name, "
                 "f.id_user AS owner_id, '' AS owner_name "
                 "FROM unit u JOIN folder f ON f.id = u.id_folder ORDER BY u.id")


def enumerate_units(db: str) -> list:
    """返回 [{unit_id, filename, type, createtime, folder_name, owner_id, owner_name}]"""
    t = transport()
    try:
        rows = t.query_json(db, ENUM_SQL)
    except DBError:
        rows = t.query_json(db, ENUM_SQL_NOUI)
    units = []
    for r in rows:
        try:
            units.append({
                "unit_id": int(r["id"]), "filename": r["filename"],
                "type": int(r["type"] or 0),
                "createtime": int(r["createtime"] or 0),
                "mtime": int(r["mtime"] or 0),
                "folder_name": r["folder_name"] or "",
                "owner_id": int(r["owner_id"] or 0),
                "owner_name": r.get("owner_name") or "",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return units


def list_databases() -> list:
    cfg = config.load()
    dbs = cfg["db"].get("databases", "auto")
    if isinstance(dbs, list) and dbs:
        return dbs
    return transport().list_databases()


def build_write_script(items: list) -> str:
    """items: [{unit_id, owner_id, tags:[(name, normalized)]}]
    生成一个事务脚本：确保标签存在 -> 取回标签行 id -> 建立关联 -> 修正本批 count。
    """
    tag_keys = set()             # (owner, name, norm)
    per_item = []
    for it in items:
        owner = it["owner_id"] or 0
        for name, norm, _s in it["tags"]:
            tag_keys.add((owner, name, norm))
        per_item.append((it["unit_id"], owner, [t[0] for t in it["tags"]]))
    lines = ["BEGIN;"]
    if tag_keys:
        vals = ", ".join("({o}::int, '{n}'::text, '{nn}'::text)".format(
            o=o, n=_escape(n), nn=_escape(nn)) for o, n, nn in tag_keys)
        lines.append(
            f"INSERT INTO general_tag (id_user, name, count, normalized_name) "
            f"SELECT v.uid, v.nm, 0, v.nn FROM (VALUES {vals}) AS v(uid, nm, nn) "
            f"WHERE NOT EXISTS (SELECT 1 FROM general_tag g "
            f"WHERE g.id_user = v.uid AND g.name = v.nm);")
    rel_vals = []
    for unit_id, owner, names in per_item:
        for name in names:
            rel_vals.append(
                f"({unit_id}::int, {owner}::int, '{_escape(name)}'::text)")
    if rel_vals:
        lines.append(
            "INSERT INTO many_unit_has_many_general_tag (id_unit, id_general_tag) "
            f"SELECT v.u, g.id FROM (VALUES {', '.join(rel_vals)}) "
            "AS v(u, uid, nm) JOIN general_tag g "
            "ON g.id_user = v.uid AND g.name = v.nm "
            "WHERE NOT EXISTS (SELECT 1 FROM many_unit_has_many_general_tag m "
            "WHERE m.id_unit = v.u AND m.id_general_tag = g.id);")
        count_vals = ", ".join(
            sorted({"({}::int, '{}'::text)".format(o, _escape(n))
                    for _, o, names in per_item for n in names}))
        lines.append(
            "UPDATE general_tag g SET count = "
            "(SELECT COUNT(*) FROM many_unit_has_many_general_tag m "
            "WHERE m.id_general_tag = g.id) "
            "WHERE (g.id_user, g.name) IN (VALUES " + count_vals + ");")
    # SELECT 必须是最后一个语句：psycopg2(TCP 模式)只返回最后一个结果集
    if tag_keys:
        vals = ", ".join("({o}::int, '{n}'::text, '{nn}'::text)".format(
            o=o, n=_escape(n), nn=_escape(nn)) for o, n, nn in tag_keys)
        lines.append(
            "SELECT COALESCE(json_agg(row_to_json(t))::text, '[]') FROM "
            "(SELECT g.id, g.id_user, g.name FROM general_tag g "
            f"JOIN (VALUES {vals}) AS v(uid, nm, nn) "
            f"ON g.id_user = v.uid AND g.name = v.nm) t;")
    lines.append("COMMIT;")
    return "\n".join(lines)


def parse_tag_rows(output: str) -> list:
    """解析写库脚本中 SELECT json_agg(...) 返回的标签行 [(id, id_user, name)]。"""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            data = json.loads(line)
            return [(int(r["id"]), int(r["id_user"]), r["name"]) for r in data]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return []


def build_existing_tags_script(items: list) -> str:
    """查询哪些 (id_user, name) 标签已存在——用于区分"复用"与"本工具新建"。"""
    tag_keys = {(it["owner_id"] or 0, n) for it in items for n, *_rest in it["tags"]}
    if not tag_keys:
        return "SELECT '[]' AS t WHERE false;"
    vals = ", ".join(f"({o}::int, '{_escape(n)}'::text)" for o, n in sorted(tag_keys))
    return ("SELECT COALESCE(json_agg(row_to_json(t))::text, '[]') FROM "
            "(SELECT g.id, g.id_user, g.name FROM general_tag g "
            f"JOIN (VALUES {vals}) AS v(uid, nm) "
            "ON g.id_user = v.uid AND g.name = v.nm) t;")


def build_cleanup_script(db: str) -> str:
    """替换模式第一步：移除本工具写入的标签。

    - 本工具**新建**的标签行：删除其全部关联 + 行本身
    - **复用**的已有标签行（如用户手工建过的同名标签）：只删除本工具
      建立的那几条 (unit, tag) 关联，并修正 count，**行本身不动**
    """
    created = [r["tag_row_id"] for r in store.query(
        "SELECT tag_row_id FROM tag_defs WHERE db_name=? AND created=1", (db,))]
    reused = [r["tag_row_id"] for r in store.query(
        "SELECT tag_row_id FROM tag_defs WHERE db_name=? AND created=0", (db,))]
    rels = store.query(
        "SELECT unit_id, tag_row_id FROM tag_rows WHERE db_name=? "
        f"AND tag_row_id IN ({','.join(str(r) for r in reused) or '0'})", (db,))
    lines = ["BEGIN;"]
    if created:
        ids = ",".join(str(r) for r in created)
        lines.append(f"DELETE FROM many_unit_has_many_general_tag "
                     f"WHERE id_general_tag IN ({ids});")
    pairs = ", ".join(f"({r['unit_id']}::int, {r['tag_row_id']}::int)"
                      for r in rels)
    if pairs:
        lines.append(f"DELETE FROM many_unit_has_many_general_tag m "
                     f"WHERE (m.id_unit, m.id_general_tag) IN (VALUES {pairs});")
    if reused:
        rids = ",".join(str(r) for r in reused)
        lines.append("UPDATE general_tag g SET count = "
                     "(SELECT COUNT(*) FROM many_unit_has_many_general_tag m "
                     "WHERE m.id_general_tag = g.id) "
                     f"WHERE g.id IN ({rids});")
    if created:
        ids = ",".join(str(r) for r in created)
        lines.append(f"DELETE FROM general_tag WHERE id IN ({ids});")
    lines.append("COMMIT;")
    return "\n".join(lines) if len(lines) > 2 else None
