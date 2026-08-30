"""文件系统 ↔ 数据库 路径匹配。

synofoto 的 folder.name 存储形式因空间类型/版本而异，因此不做绝对路径
假设，而用"最长后缀匹配"：DB 侧用 (folder 路径尾部 + 文件名)，FS 侧用
(挂载目录下相对路径尾部)，逐级尝试，唯一命中即配对。

对每个 unit 尝试两种候选：
1) folder_name + filename
2) [owner_name, 'Photos'] + folder_name + filename（个人空间 homes 布局）
"""
import os
import threading
import time

MAX_TAIL = 8


class Matcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._maps = None        # k -> {tail_tuple_lower: abs_path}（首个命中）
        self._multi = None       # k -> set(tail_tuple_lower) 出现歧义的键
        self._files = 0

    # ------------------------------------------------------- FS 索引
    def build_fs_index(self, mounts: list, exclude_dirs: list,
                       exts: set, min_bytes: int = 0):
        """mounts: [container_path, ...]；重建索引并缓存。

        内存精简：每个键只存一个路径字符串，重复键记入 _multi 集合，
        大库（数十万文件）占用约为朴素实现（集合存全量路径）的十分之一。
        """
        maps = {k: {} for k in range(1, MAX_TAIL + 1)}
        multi = {k: set() for k in range(1, MAX_TAIL + 1)}
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
                        else:
                            m[key] = absp
        with self._lock:
            self._maps = maps
            self._multi = multi
            self._files = n
        return n

    @property
    def file_count(self) -> int:
        with self._lock:
            return self._files

    # ------------------------------------------------------- 匹配
    def _lookup(self, maps, multi, cand):
        """从长到短尝试候选路径尾部。返回 (命中路径, 是否歧义)。"""
        ambiguous = False
        for k in range(min(len(cand), MAX_TAIL), 0, -1):
            key = tuple(c.lower() for c in cand[-k:])
            if key in multi[k]:
                ambiguous = True
                break                       # 更长 tail 已歧义，换下一个候选
            hit = maps[k].get(key)
            if hit:
                return hit, False
        return "", ambiguous

    def match(self, folder_name: str, filename: str,
              owner_name: str = "") -> str:
        """返回命中的绝对路径；未命中返回 ''；歧义返回 None。"""
        with self._lock:
            maps, multi = self._maps, self._multi
        if not maps:
            return ""
        folder_comps = [c for c in folder_name.replace("\\", "/").split("/")
                        if c and c not in (".",)]
        candidates = [folder_comps + [filename]]
        if owner_name:
            candidates.append([owner_name.lower(), "Photos"]
                              + folder_comps + [filename])
        ambiguous = False
        for cand in candidates:
            path, amb = self._lookup(maps, multi, cand)
            if path:
                return path
            ambiguous = ambiguous or amb
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
