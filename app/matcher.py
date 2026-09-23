"""文件系统 ↔ 数据库 路径匹配。

synofoto 的 folder.name 存储形式因空间类型/版本而异，因此不做绝对路径
假设，而用"最长后缀匹配"：DB 侧用 (folder 路径尾部 + 文件名)，FS 侧用
(挂载目录下相对路径尾部)，逐级尝试，唯一命中即配对。

对每个 unit 尝试三种候选：
1) folder_name + filename
2) [owner_name, 'Photos'] + folder_name + filename（个人空间 homes 布局）
3) [owner_name] + folder_name + filename（MobileBackup 直挂用户根）

歧义处理：同一 tail 键对应多个文件（典型场景：多个用户的
homes/<user>/Photos/MobileBackup/iPhone/... 同名文件）时，
- 候选 2/3 把 owner 编入键，天然消歧（前提是 owner_name 已知，
  见 pipeline._enumerate 的全局用户表回填）；
- 键仍歧义时（如 tail 窗口内不含 owner 的超深路径），用歧义键
  记录的竞争路径按"路径包含 owner 目录名"再过滤一次，唯一即命中；
- 仍不唯一 → 返回 None（记 error 跳过，绝不猜）。
"""
import os
import threading
import time

MAX_TAIL = 10          # homes/<user>/Photos/<最深目录>/<文件> 需覆盖 8+ 级
_AMB_CAP = 32          # 每个歧义键最多记录的竞争路径数（防爆内存）


class Matcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._maps = None        # k -> {tail_tuple_lower: abs_path}（首个命中）
        self._multi = None       # k -> set(tail_tuple_lower) 出现歧义的键
        self._amb = None         # k -> {key: [abs_path, ...]} 歧义键的竞争路径
        self._files = 0

    # ------------------------------------------------------- FS 索引
    def build_fs_index(self, mounts: list, exclude_dirs: list,
                       exts: set, min_bytes: int = 0):
        """mounts: [container_path, ...]；重建索引并缓存。

        内存精简：唯一键只存一个路径字符串；歧义键（罕见）才额外在
        _amb 里记录全部竞争路径（供 owner 过滤消歧）。
        """
        maps = {k: {} for k in range(1, MAX_TAIL + 1)}
        multi = {k: set() for k in range(1, MAX_TAIL + 1)}
        amb = {k: {} for k in range(1, MAX_TAIL + 1)}
        n = 0
        exclude = set(exclude_dirs) | {"@eaDir", "#recycle", "@tmp", "#snapshot"}
        for root in mounts:
            root = root.rstrip("/")
            if not os.path.isdir(root):
                continue
            walked = 0
            for dirpath, dirnames, filenames in os.walk(root):
                walked += len(filenames)
                if walked >= 500:      # 周期性让出 CPU，避免饿死 Web 服务
                    walked = 0
                    time.sleep(0.01)
                dirnames[:] = [d for d in dirnames
                               if d not in exclude and not d.startswith(".")]
                for fn in filenames:
                    if fn.startswith(".") or fn.startswith("._"):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in exts:
                        continue
                    absp = os.path.join(dirpath, fn)
                    if min_bytes:
                        try:
                            if os.path.getsize(absp) < min_bytes:
                                continue
                        except OSError:
                            continue
                    rel = os.path.relpath(absp, root)
                    comps = rel.replace("\\", "/").split("/")
                    n += 1
                    for k in range(1, min(len(comps), MAX_TAIL) + 1):
                        key = tuple(c.lower() for c in comps[-k:])
                        m = maps[k]
                        if key in m:
                            if m[key] != absp:
                                multi[k].add(key)
                                paths = amb[k].setdefault(key, [m[key]])
                                if len(paths) < _AMB_CAP and absp not in paths:
                                    paths.append(absp)
                        else:
                            m[key] = absp
        with self._lock:
            self._maps = maps
            self._multi = multi
            self._amb = amb
            self._files = n
        return n

    @property
    def file_count(self) -> int:
        with self._lock:
            return self._files

    # ------------------------------------------------------- 匹配
    @staticmethod
    def _filter_owner(paths, owner: str) -> list:
        """竞争路径中按 owner 目录名过滤（homes/<owner>/...）。"""
        o = owner.lower()
        return [p for p in paths
                if o in p.replace("\\", "/").lower().split("/")]

    def _lookup(self, maps, multi, amb, cand, owner: str = ""):
        """从长到短尝试候选路径尾部。返回 (命中路径, 是否歧义)。"""
        ambiguous = False
        for k in range(min(len(cand), MAX_TAIL), 0, -1):
            key = tuple(c.lower() for c in cand[-k:])
            if key in multi[k]:
                # 歧义键：有 owner 时按竞争路径的目录名再过滤一次
                if owner:
                    hits = self._filter_owner(amb[k].get(key, ()), owner)
                    if len(hits) == 1:
                        return hits[0], False
                ambiguous = True
                break                       # 换下一个候选（可能含 owner 键）
            hit = maps[k].get(key)
            if hit:
                return hit, False
        return "", ambiguous

    def match(self, folder_name: str, filename: str,
              owner_name: str = "") -> str:
        """返回命中的绝对路径；未命中返回 ''；歧义返回 None。"""
        with self._lock:
            maps, multi, amb = self._maps, self._multi, self._amb
        if not maps:
            return ""
        folder_comps = [c for c in folder_name.replace("\\", "/").split("/")
                        if c and c not in (".",)]
        candidates = [folder_comps + [filename]]
        if owner_name:
            o = owner_name.lower()
            # 布局一：homes/<user>/Photos/<目录>（个人空间标准布局）
            candidates.append([o, "Photos"] + folder_comps + [filename])
            # 布局二：homes/<user>/<目录>（MobileBackup 等直接挂用户根）
            candidates.append([o] + folder_comps + [filename])
        ambiguous = False
        for cand in candidates:
            path, amb_hit = self._lookup(maps, multi, amb, cand, owner_name)
            if path:
                return path
            ambiguous = ambiguous or amb_hit
        return None if ambiguous else ""

    def locate(self, rel_path: str) -> str:
        """按相对路径（任意形态）在后缀索引中定位文件，用于缩略图服务。

        索引未建立或路径存在歧义（如多用户同名目录）时返回 ''。
        """
        with self._lock:
            maps, multi = self._maps, self._multi
        if not maps or not rel_path:
            return ""
        comps = [c for c in rel_path.replace("\\", "/").split("/") if c]
        for k in range(min(len(comps), MAX_TAIL), 0, -1):
            k2 = tuple(c.lower() for c in comps[-k:])
            if k2 in multi[k]:
                return ""               # 歧义，无法唯一定位
            hit = maps[k].get(k2)
            if hit:
                return hit
        return ""

    def stats(self) -> dict:
        with self._lock:
            return {"files": self._files,
                    "indexed": bool(self._maps)}


MATCHER = Matcher()