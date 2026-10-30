"""追光几何 · 存储引擎

设计要点：
- 仓库元数据放在工作目录下的 .zgr/，工作目录本身照常使用（CAD/Office 文件原样不动）。
- 所有内容按 sha256 寻址，对象压缩后明文存放，可随时手工检查。
- 快照 = 目录树 + 提交对象；提交线性父链，分支用 refs/heads/<name>。
- 标记（红/黄/绿）与标签保存在独立的 marks.json，不改写提交对象。
"""
from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import os
import re
import shutil
import stat as stat_mod
import threading
import time
import zlib

from collections import OrderedDict, deque

META_DIR = ".zgr"
# 写文件时的半成品后缀：和目标文件同目录（保证替换是同一个卷上的原子 rename），
# 名字固定不带 pid，断了也好认、好清。
PART_SUFFIX = ".zgr-part"
# 盘上至少给别的程序留这么多空间，否则一次大记录就能把系统盘塞死
# （Windows 塞满之后连 SolidWorks 存盘都会失败，比记录失败严重得多）
MIN_FREE_BYTES = 512 << 20
# 目录树最多几层。正常项目十几层就到底了；对象也可能是对端推来的，
# 几千层嵌套会让递归爆栈——给个上限，超了就说人话。
MAX_TREE_DEPTH = 64
# 单个对象解压后的上限。从网络收来的对象可能是**压缩炸弹**：几百 KB 的
# 全零流解开来是几十 GB，一把梭解压会把服务进程直接打死。正常的 CAD 文件
# 压缩比在 2~5 倍，2 GB 足够放下任何真实零件。
MAX_RAW_OBJECT = 512 << 20

DEFAULT_IGNORE = [
    ".zgr", ".git", ".svn", ".evercraft", ".DS_Store", "Thumbs.db", "desktop.ini",
    "$RECYCLE.BIN", "System Volume Information", "~$*", "*.tmp", "*.swp", "*.bak",
]
MARKS = ("red", "yellow", "green")


class RepoError(Exception):
    """仓库操作失败"""


# --------------------------------------------------------------------- 跨进程锁
# 网页服务和命令行可能同时动同一个项目。进程内的锁管不到另一个进程，
# 所以要落到文件上。锁是「按项目」的，不同项目互不影响。
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()
_TLS = threading.local()


def _repo_lock(path: str):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        rl = _LOCKS.get(key)
        if rl is None:
            rl = _LOCKS[key] = threading.RLock()
        return rl


def _take_file_lock(meta: str, timeout: float) -> object:
    os.makedirs(meta, exist_ok=True)
    p = os.path.join(meta, "lock")
    f = open(p, "a+b")
    try:
        if os.path.getsize(p) == 0:      # Windows 的文件锁必须锁一个真实存在的字节
            f.write(b"\0")
            f.flush()
        t0 = time.time()
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                if time.time() - t0 > timeout:
                    f.close()
                    raise RepoError(
                        "另一个窗口（或另一台程序）正在操作这个项目，已经等了 %d 秒还没完。"
                        "请等它做完再试——这时候硬来才真会出问题。" % int(timeout)) from None
                time.sleep(0.15)
    except BaseException:
        try:
            f.close()
        except OSError:
            pass
        raise


def _drop_file_lock(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            f.close()
        except OSError:
            pass


class _ProcLock:
    """同一线程里重复进出只锁一次（commit 里可能再调 restore）"""

    def __init__(self, root: str, meta: str, what: str, timeout: float):
        self.root = root
        self.meta = meta
        self.what = what
        self.timeout = timeout
        self.rl = None
        self.f = None
        self.key = os.path.normcase(os.path.abspath(root))

    def __enter__(self):
        self.rl = _repo_lock(self.root)
        if not self.rl.acquire(timeout=self.timeout):
            raise RepoError("这个项目正被别的操作占用，等了 %d 秒还没轮到%s"
                            % (int(self.timeout), self.what))
        held = getattr(_TLS, "held", None)
        if held is None:
            held = _TLS.held = {}
        first = held.get(self.key, 0) == 0
        held[self.key] = held.get(self.key, 0) + 1
        try:
            # 目录还不是项目时不要抢锁：_take_file_lock 会建目录，
            # 那等于「对普通文件夹提交」顺手造出一个 .zgr，跟直接报错的本意相违
            if first and os.path.isdir(self.meta):
                self.f = _take_file_lock(self.meta, self.timeout)
        except BaseException:
            held[self.key] -= 1
            self.rl.release()
            raise
        return self

    def __exit__(self, *exc):
        held = getattr(_TLS, "held", {})
        held[self.key] = max(0, held.get(self.key, 1) - 1)
        if held[self.key] == 0 and self.f is not None:
            _drop_file_lock(self.f)
        if self.rl is not None:
            self.rl.release()
        return False


def now_ms() -> int:
    return int(time.time() * 1000)


def norm(rel: str) -> str:
    return rel.replace("\\", "/")


def _depth(d: str) -> int:
    """目录深度：根为 0，子目录逐级 +1（不能只看 '/' 个数，根和一级目录都是 0）"""
    return 0 if not d else d.count("/") + 1


_WIN_BAD = set('/\\:*?"<>|')
# 同一个字符集的正则版：树里两千个条目逐字符扫一遍是纯 Python 循环，
# `files_of()` 一次就要 12 万次 ord()；交给正则，一次 C 层扫描就完事。
# 控制字符（<32）也在这里一起挡掉。
_WIN_BAD_RX = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} \
    | {"lpt%d" % i for i in range(1, 10)}


def safe_rel(rel: str):
    """校验树里的相对路径，挡住 ../ 、盘符、UNC、Windows 非法字符；
    合法返回规范化路径，非法返回 None"""
    if not rel:
        return None
    rel = norm(rel)
    if rel.startswith("/") or ":" in rel:
        return None
    for part in rel.split("/"):
        if not part or part in (".", ".."):
            return None
        if any(c in _WIN_BAD for c in part) or any(ord(c) < 32 for c in part):
            return None
        if part[-1] in " .":
            return None
        if part.split(".")[0].lower() in _WIN_RESERVED:
            return None
    return rel


def safe_branch(name: str) -> str:
    name = (name or "main").strip().replace("\\", "/")
    if "/" in name or name in (".", "..") or not name or ".." in name:
        raise RepoError("分支名不合法：%r" % name)
    if any(c in _WIN_BAD for c in name) or name[-1] in " .":
        raise RepoError("分支名含有 Windows 不允许的字符：%r" % name)
    if name.split(".")[0].lower() in _WIN_RESERVED:
        raise RepoError("分支名与系统保留名冲突：%r" % name)
    return name


def is_link_like(path: str) -> bool:
    """符号链接 / 目录 junction 都算：扫描和删除都不能越出项目边界"""
    try:
        if os.path.islink(path):
            return True
    except OSError:
        return True
    try:
        st = os.lstat(path)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x400)      # FILE_ATTRIBUTE_REPARSE_POINT


_PATSET: dict = {}
_PATSET_MAX = 64


def _pat_sets(patterns):
    """把整组忽略规则编译成**两条**正则，差一点的文件名就不用逐条比了。

    老写法每个文件、每条规则都调一次 `fnmatch.fnmatch`，而它在 Windows 上
    两边都要先 normcase（转小写走 LCMapStringEx 系统调用）。2000 个文件的
    项目跑一次 status：52 万次 fnmatch、100 万次系统调用，光这一步 1.7 秒。
    这里把整组规则并成一条正则，每个文件最多两三次 match。

    语义与原版一致：Windows 上不区分大小写，`\\` 与 `/` 等价，
    `*` 照样能跨目录匹配（`fnmatch` 本来就不认分隔符）。
    """
    key = tuple(patterns)
    got = _PATSET.get(key)
    if got is not None:
        return got
    flags = re.IGNORECASE if os.name == "nt" else 0
    full, part = [], []
    for p in key:
        p = str(p)
        if os.name == "nt":
            p = p.replace("\\", "/")        # 与 fnmatch 在 Windows 上的 normcase 一致
        if not p:
            continue
        rx = fnmatch.translate(p)           # 形如 (?s:…)\Z，各自已经锚住结尾
        full.append("(?:%s)" % rx)
        if "/" not in p:                    # 只写了名字的规则，也管任意一层目录名
            part.append("(?:%s)" % rx)
    got = (re.compile("|".join(full), flags) if full else None,
           re.compile("|".join(part), flags) if part else None)
    if len(_PATSET) > _PATSET_MAX:          # 规则是人手填的，这个小上限足够
        _PATSET.clear()
    _PATSET[key] = got
    return got


def ignored(rel: str, patterns) -> bool:
    """判断相对路径是否命中忽略规则（支持通配符、目录名、全路径）"""
    rel = norm(rel)
    name = rel.rsplit("/", 1)[-1]
    # 写盘用的半成品，无论用户怎么改忽略规则都不算项目文件。
    # 否则恢复到一半被强杀留下的 .zgr-part 会被当成零件记录进版本。
    if name.endswith(PART_SUFFIX):
        return True
    full_rx, part_rx = _pat_sets(patterns)
    if full_rx is not None and (full_rx.match(name) is not None
                                or full_rx.match(rel) is not None):
        return True
    if part_rx is not None:
        for p in rel.split("/"):
            if part_rx.match(p) is not None:
                return True
    return False


def check_new_root(path: str) -> None:
    """新建项目前先把路径本身挡住：必须是绝对路径、长度合理、名字合法。

    以前这里什么都不查——一个畸形请求就会在服务的工作目录里凭空建出一串目录，
    甚至去动 C:\\Windows 这种地方。宁可拒绝，也不能乱建。
    """
    p = str(path or "").strip()
    if not p:
        raise RepoError("没有指定项目位置")
    if not os.path.isabs(p):
        raise RepoError("项目位置必须是完整路径（比如 D:\\图纸\\新项目）")
    if len(p) > 240:
        raise RepoError("项目路径太长（%d 个字符），Windows 上超过 260 会读写不了" % len(p))
    bad = set('/\\:*?"<>|')
    for part in p.replace("/", "\\").split("\\"):
        if not part:
            continue
        if part != part.strip(" ."):
            raise RepoError("项目路径里有一段以空格或点结尾，Windows 建不出来：%s" % part)
        if any(ch in bad and ch not in (":", "\\") for ch in part):
            raise RepoError("项目路径里有非法字符：%s" % part)
        if any(ord(ch) < 32 for ch in part):
            raise RepoError("项目路径里有控制字符")
        if part.split(".")[0].lower() in _WIN_RESERVED:
            raise RepoError("项目路径里用了系统保留名：%s" % part)


def _writable(path: str) -> bool:
    """这个文件现在能不能被覆盖写：只读属性、被别的程序独占，都会返回 False。

    只探测、不修改内容：r+b 打不开就是写不了，SolidWorks 正开着的零件会在这里露馅。
    """
    path = fs_path(path)
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not (st.st_mode & stat_mod.S_IWRITE):
        return False
    try:
        with open(path, "r+b"):
            pass
    except OSError:
        return False
    return True


# 对象落库（临时文件 → 正式路径）串行化用；只在最后一步加锁，不影响边读边压的性能
_OBJECT_LOCK = threading.Lock()
# 全历史搜索索引：按项目路径缓存，签名（分支头）一变就重建
_SEARCH_CACHE: dict = {}
_SEARCH_LOCK = threading.Lock()
_SEARCH_TTL = 120.0


# Windows 传统 API 的路径上限是 260 字符；超过就报 WinError 206（文件名或扩展名太长）。
# 167/174 上 LongPathsEnabled 都是 0，而 CAD 项目的目录往往套得很深——一旦命中，
# 这个项目就再也记不了版本，报错还误指向「SolidWorks 正在写」。
# `\\?\` 前缀直接下发到系统调用，不受这个上限约束，也不用改注册表、不用管理员权限。
WIN_LONG = 240          # 从这么长开始加前缀（留出同名临时文件 .tmp.pid.tid.ns 的余量）


def fs_path(p: str) -> str:
    """过长的 Windows 路径加上 \\\\?\\ 前缀；其余情况原样返回。

    只应该用在**真正要交给系统**的那一步（open / stat / makedirs / scandir /
    replace / copy），不要拿它拼相对路径：前缀会跟着字符串跑到显示和比较里去。
    """
    if os.name != "nt" or not p:
        return p
    s = str(p)
    if s.startswith("\\\\?\\"):
        return s
    # 快路径：已经是规规矩矩的绝对路径、又不长，就直接交回去。
    # 这个函数在扫描/状态/体积统计里是每文件一次（2000 文件的项目就是 2000 次），
    # 每次都走一遍 abspath（normpath + getcwd）平白多出几十毫秒；
    # 绝大多数调用本来就是绝对路径，没必要再规范化一遍。
    if len(s) < WIN_LONG and ".." not in s and "/" not in s and (
            (s[1:2] == ":" and "\\" in s)                    # C:\项目\a.sldprt
            or s.startswith("\\\\")):                        # \\服务器\共享\a.sldprt
        return s
    a = os.path.abspath(s)
    if len(a) < WIN_LONG:
        return a
    a = a.replace("/", "\\")
    if a.startswith("\\\\"):
        # UNC：\\服务器\共享\x → \\?\UNC\服务器\共享\x
        return "\\\\?\\UNC" + a[1:]
    return "\\\\?\\" + a


def _too_long_err(e, path) -> str:
    """把「路径太长」认出来，别让它被当成「文件被占用」悄悄跳过"""
    win = getattr(e, "winerror", None)
    if win == 206 or getattr(e, "errno", None) == 36:      # 36 = ENAMETOOLONG
        return ("路径太长（%d 个字符），Windows 的普通路径上限是 260：%s\n"
                "可以把项目的上级目录挪浅一点，或者把中间几层文件夹改短；"
                "也可以让管理员打开长路径支持（LongPathsEnabled）。"
                % (len(str(path)), str(path)[-120:]))
    return ""


# ---------------------------------------------------------------- 对象读缓存
# 为什么要有：打开一个项目，**同一个目录树对象会被读十几遍**——时间线要算每版
# 改了哪些文件、文件清单要展开整棵树、搜索要翻提交、状态要和工作区比对，每次都是
# 解压 + 重算一遍 sha256。对象是内容寻址的，写完就再也不会变，读进内存的副本
# 永远是对的；唯一要管的是**别把几十兆的图纸也塞进来**，所以有大小上限和总量上限。
#
# 一律按「对象文件的完整路径」做键：服务里每个请求都新建一个 Repo，按实例缓存
# 等于白缓存；按路径缓存才能跨请求、跨线程命中。
OBJ_CACHE_MAX = 48 << 20        # 全进程最多缓存这么多字节
OBJ_CACHE_ITEM = 2 << 20        # 单个对象超过 2 MB 就不缓存（大图纸走流式）

_obj_cache = OrderedDict()      # path -> (kind, payload, size, mtime_ns)
_obj_cache_bytes = 0
_obj_cache_lock = threading.Lock()
# 命中率是「缓存到底有没有用」的唯一证据：两台真机上打开项目慢不慢，
# 光看耗时分不清是卡在磁盘还是卡在别处，有这几个数就能一眼判断。
_obj_cache_stat = {"hits": 0, "misses": 0, "evicted": 0}


def _obj_cache_get(path: str):
    with _obj_cache_lock:
        ent = _obj_cache.get(path)
        if ent is None:
            _obj_cache_stat["misses"] += 1
            return None
        _obj_cache.move_to_end(path)        # LRU：常用的活下来
    # 命中也要跟盘上对一眼：**被别的进程删掉/改掉的对象不能从内存里继续供给**。
    # 一次 stat（几微秒）换掉的是「解压 + 重算 sha256」（几百微秒），还顺带管住两件事：
    # 命令行里跑过 `zgr gc` 之后服务不会继续捧着已删对象；盘上被改坏的对象会被
    # 下一句 open 里的哈希校验抓个正着，而不是被缓存悄悄瞒过去。
    try:
        st = os.stat(path)
    except OSError:
        _obj_cache_drop(path)
        with _obj_cache_lock:
            _obj_cache_stat["misses"] += 1
        return None
    if st.st_size != ent[2] or st.st_mtime_ns != ent[3]:
        _obj_cache_drop(path)
        with _obj_cache_lock:
            _obj_cache_stat["misses"] += 1
        return None
    with _obj_cache_lock:
        _obj_cache_stat["hits"] += 1
    return ent[0], ent[1]


def _obj_cache_drop(path: str) -> None:
    global _obj_cache_bytes
    with _obj_cache_lock:
        old = _obj_cache.pop(path, None)
        if old is not None:
            _obj_cache_bytes -= len(old[1])


def _obj_cache_put(path: str, kind: str, payload: bytes, st=None) -> None:
    global _obj_cache_bytes
    n = len(payload)
    if n > OBJ_CACHE_ITEM:
        return
    if st is None:
        try:
            st = os.stat(path)
        except OSError:
            return
    with _obj_cache_lock:
        old = _obj_cache.get(path)
        if old is not None:
            _obj_cache_bytes -= len(old[1])
        _obj_cache[path] = (kind, payload, st.st_size, st.st_mtime_ns)
        _obj_cache.move_to_end(path)
        _obj_cache_bytes += n
        while _obj_cache_bytes > OBJ_CACHE_MAX and len(_obj_cache) > 1:
            _k, ent2 = _obj_cache.popitem(last=False)
            _obj_cache_bytes -= len(ent2[1])
            _obj_cache_stat["evicted"] += 1


def obj_cache_clear() -> None:
    """丢掉整个缓存（删过对象、整盘搬过项目之后必须调一次）"""
    global _obj_cache_bytes
    with _obj_cache_lock:
        _obj_cache.clear()
        _obj_cache_bytes = 0
        for k in _obj_cache_stat:
            _obj_cache_stat[k] = 0


def obj_cache_stats() -> dict:
    with _obj_cache_lock:
        out = {"items": len(_obj_cache), "bytes": _obj_cache_bytes,
               "max_bytes": OBJ_CACHE_MAX}
        out.update(_obj_cache_stat)
        return out


def _atomic_write(path: str, data: bytes) -> None:
    path = fs_path(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 临时名带上线程和时间：实时同步线程与网页请求可能同时写同一个 config.json，
    # 只用 pid 会互相覆盖临时文件。
    tmp = "%s.tmp.%d.%d.%d" % (path, os.getpid(),
                               threading.get_ident(), time.time_ns())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Windows 上目标文件可能恰好被别人打开一下（杀毒/并发读），短暂重试即可
        for i in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _locked(what: str, timeout: float = 180.0):
    """给 Repo 上的方法套一层「按项目的跨进程锁」"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **kw):
            with self.lock(what, timeout=timeout):
                return fn(self, *a, **kw)
        return wrapper
    return deco


class Repo:
    def __init__(self, path: str):
        self.root = os.path.abspath(path)
        self.meta = os.path.join(self.root, META_DIR)
        self.last_skipped = []

    def lock(self, what="操作", timeout: float = 180.0):
        """按项目上锁：网页服务和命令行同时动一个项目时排队，不互相踩"""
        return _ProcLock(self.root, self.meta, what, timeout)


    # ------------------------------------------------------------------ 基础
    @property
    def exists(self) -> bool:
        return os.path.isdir(self.meta) and os.path.isfile(os.path.join(self.meta, "config.json"))

    def init(self, name: str | None = None) -> "Repo":
        if self.exists:
            raise RepoError("这里已经是追光几何仓库：%s" % self.root)
        check_new_root(self.root)
        if not os.path.isdir(self.root):
            os.makedirs(self.root, exist_ok=True)
        for sub in ("objects", "refs/heads", "logs", "backup"):
            os.makedirs(os.path.join(self.meta, sub), exist_ok=True)
        cfg = {
            "version": 1,
            "name": name or os.path.basename(self.root.rstrip("\\/")) or "项目",
            "created": now_ms(),
            "ignore": DEFAULT_IGNORE,
            "author": os.environ.get("USERNAME", "user"),
            "auto_checkout": True,
        }
        self._write_json("config.json", cfg)
        self._write_text("HEAD", "main")
        self._write_json("marks.json", {})
        self._write_json("index.json", {"files": {}, "head": None, "branch": "main"})
        return self

    # ------------------------------------------------------------------ 读写元数据
    def _p(self, *parts) -> str:
        return os.path.join(self.meta, *parts)

    def _read_json(self, name, default=None):
        p = fs_path(self._p(name))
        for i in range(6):
            try:
                # utf-8-sig：记事本手改过的文件开头带 BOM，按 utf-8 读会解析失败，
                # 结果 config/日志这类文件被当成「没有」——静默丢掉用户设置。
                with open(p, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except OSError:
                # Windows 上正好被 os.replace 换掉时会短暂打不开，等一下再读
                if i == 5:
                    break
                time.sleep(0.02)
            except ValueError:
                break
        if default is not None:
            return default
        raise RepoError("读取 %s 失败" % name)

    def _write_json(self, name, obj) -> None:
        _atomic_write(self._p(name), json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    def update_config(self, fn, name="config.json") -> dict:
        """读-改-写配置，整段在**跨进程锁**里。

        以前是 `cfg = repo.config; cfg["x"] = v; repo._write_json(...)`：网页服务
        和命令行（`zgr remote add`、`zgr live`、`zgr watch`）同时改一个项目时，
        后写的把对方刚改的那几项整份盖掉——丢的是「忽略规则」「同步地址」
        「预览回源」这类不起眼、但丢了你根本想不起来原来是什么的设置。

        fn 拿到的是副本，返回新 dict（返回 None 表示就地改完即可）。
        """
        with self.lock("改设置"):
            cfg = self._read_json(name, {})
            if not isinstance(cfg, dict):
                cfg = {}
            work = dict(cfg)
            new = fn(work)
            if new is None:
                new = work
            if new != cfg:
                self._write_json(name, new)
            return new

    def _read_text(self, name, default="") -> str:
        try:
            with open(fs_path(self._p(name)), "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return default

    def _write_text(self, name, text) -> None:
        _atomic_write(self._p(name), text.encode("utf-8"))

    @property
    def config(self) -> dict:
        return self._read_json("config.json")

    @property
    def name(self) -> str:
        try:
            return self.config.get("name") or os.path.basename(self.root)
        except RepoError:
            return os.path.basename(self.root)

    @property
    def ignore_patterns(self):
        cfg = self.config
        return list(cfg.get("ignore") or DEFAULT_IGNORE)

    @property
    def branch(self) -> str:
        return self._read_text("HEAD", "main") or "main"

    def branches(self):
        d = self._p("refs", "heads")
        if not os.path.isdir(d):
            return []
        return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))

    def head_oid(self, branch: str | None = None):
        # 显式传进来的分支名会拼进路径：必须校验，否则 "..\\..\\某个文件" 就能把
        # 任意文件当成分支指针读出来（跨机同步接口传的就是外面来的分支名）
        branch = safe_branch(branch) if branch is not None else self.branch
        oid = self._read_text(os.path.join("refs", "heads", branch), "")
        return oid or None

    def _set_head(self, oid: str, branch: str) -> None:
        rel = os.path.join("refs", "heads", safe_branch(branch))
        if oid is None:                    # 清掉分支指针（等同删分支），别让 None 炸出 500
            try:
                os.remove(self._p(rel))
            except OSError:
                pass
            return
        self._write_text(rel, oid)

    @_locked("切换分支")
    def set_branch(self, branch: str, oid: str) -> None:
        """把分支指向某提交（同步快进/合并用）

        ⚠ 不要清空 index.files：那是「路径→(oid,mtime,size)」的内容缓存，
        与分支无关，清掉会让下一次 status/restore 把整个项目重新读一遍。
        """
        branch = safe_branch(branch)
        self._set_head(oid, branch)
        idx = self._read_json("index.json", {}) or {}
        idx["head"] = oid
        idx["branch"] = branch
        idx.setdefault("files", {})
        self._write_json("index.json", idx)

    @_locked("更新分支指针")
    def set_ref(self, branch: str, oid: str) -> None:
        """只动某条分支的指针，**不碰当前分支和索引**

        `set_branch()` 是「切到这条分支」用的，它会把 index.json 的 head/branch
        一起改掉。同步别人的线时只想要「这条分支在本机也到这一版了」，
        绝不能顺手把用户当前所在的分支改掉——那样下一次 status/restore 会拿
        别人的版本对着自己的工作区算。
        """
        self._set_head(oid, safe_branch(branch))

    @_locked("删除分支")
    def delete_branch(self, branch: str) -> None:
        """删掉一条分支的指针（只删 refs/heads/<名>，一个对象都不删）

        和 git branch -d 一个意思：提交、文件、历史全留着，还能按提交号重新开分支。
        当前所在的分支不允许删——先把 HEAD 切走。
        """
        branch = safe_branch(branch)
        if branch == self.branch:
            raise RepoError("不能删除当前所在的分支，先切到别的分支再删")
        p = self._p("refs", "heads", branch)
        if not os.path.isfile(p):
            raise RepoError("没有这条分支：%s" % branch)
        os.remove(p)

    def commit_reachable(self, oid: str | None, limit: int = 200000) -> set:
        """从某提交沿**全部父链**能走到的提交集合（只看提交，不碰树和文件）

        用来判断「这条分支是不是已经被别的分支吃掉了」。
        sync.reachable 会把树和文件一起走，判断分支合并太浪费。
        """
        if not oid or not self.has(oid):
            return set()
        seen, stack = set(), [oid]
        while stack and len(seen) < limit:
            cur = stack.pop()
            if not cur or cur in seen or not self.has(cur):
                continue
            seen.add(cur)
            try:
                c = self.commit_info(cur)
            except RepoError:
                continue
            stack.extend([p for p in (c.get("parents") or []) if p])
        return seen

    # ------------------------------------------------------------------ 体检
    def stored_objects(self):
        """盘上所有对象号（一个字节都不读，只列名字）。
        注意和 all_objects() 不是一回事：那个返回的是**可达**对象。"""
        out = []
        base = fs_path(self._p("objects"))
        if not os.path.isdir(base):
            return out
        try:
            dirs = os.listdir(base)
        except OSError:
            return out
        for d in dirs:
            sub = os.path.join(base, d)
            if len(d) != 2 or not os.path.isdir(sub):
                continue
            try:
                names = os.listdir(sub)
            except OSError:
                continue
            for name in names:
                if len(name) == 62:
                    out.append(d + name)
        return out

    def _verify_object(self, oid: str):
        """把一个对象从压缩文件读出来，逐项核对（用于 deep 体检）"""
        p = self._obj_path(oid)
        try:
            with open(p, "rb") as f:
                raw = zlib.decompress(f.read())
        except (OSError, zlib.error) as e:
            raise RepoError("读不出来或解压失败：%s" % e)
        kind, rest = raw.split(b"\n", 1)
        n, payload = rest.split(b"\n", 1)
        if len(payload) != int(n):
            raise RepoError("长度不符（头里写 %s，实际 %d）" % (n.decode("ascii", "replace"), len(payload)))
        if hashlib.sha256(raw).hexdigest() != oid:
            raise RepoError("内容与对象号不符（被改过或磁盘损坏）")

    def fsck(self, deep: bool = False, progress=None) -> dict:
        """体检：分支头能不能解开、对象缺没缺、内容坏没坏、有多少孤儿对象。

        deep=False 只走连通性，几秒钟就能扫完几万个对象；
        deep=True 逐个重算 sha256，能抓到磁盘静默损坏（同长度改字节），慢但彻底。
        这一步只读不写，怎么跑都不会动图纸。
        """
        report = {"ok": True, "objects": 0, "refs": {}, "missing": [], "bad": [],
                  "dangling": [], "deep": bool(deep), "errors": [], "warnings": []}
        refs = {}
        for b in self.branches():
            try:
                refs[b] = self.head_oid(b)
            except OSError as e:
                report["errors"].append("分支 %s 读不出来：%s" % (b, e))
                refs[b] = None
        report["refs"] = refs

        objs = self.stored_objects()
        report["objects"] = len(objs)
        if objs and not any(refs.values()):
            # 最怕的「看起来是空项目」：图纸和对象都在，但分支指针没了。
            # 不算错（可能是故意删光了分支），但必须说出来，不能让人以为一切正常。
            report["warnings"].append(
                "库里还有 %d 个对象，但没有任何分支指向它们——历史成了孤儿。"
                "多半是分支指针被删/丢了；对象一个没少，可从另一台机器 repair 或按提交号重开分支。" % len(objs))
        bad_ids = {}
        if deep:
            for n, oid in enumerate(objs):
                if progress and (n % 100 == 0):
                    try:
                        progress(n, len(objs))
                    except Exception:      # 进度回调坏了也不能中断体检
                        pass
                try:
                    self._verify_object(oid)
                except RepoError as e:
                    bad_ids[oid] = str(e)
                    report["bad"].append({"oid": oid, "why": str(e)})
            if progress:
                try:
                    progress(len(objs), len(objs))
                except Exception:
                    pass

        # 连通性：从分支头往下走。只解析提交和目录（都是小对象），
        # 文件对象在浅体检里只确认「在不在」，逐字节核对留给 deep——否则扫一遍
        # 1.9 GB 的图纸项目要读全部内容，谁也不会真的去点。
        bad_set = set(bad_ids)
        reachable, missing, stack = set(), {}, [(o, "commit") for o in refs.values() if o]
        while stack:
            cur, want = stack.pop()
            if not cur or cur in reachable:
                continue
            reachable.add(cur)
            if not self.has(cur):
                missing[cur] = True
                continue
            if cur in bad_set:
                continue                  # 坏对象已经记过，不再往下走
            if want == "blob":
                continue
            try:
                # 体检必须读**盘上那一份**：缓存里可能是刚才好的那一版，
                # 盘上被人改坏/磁盘坏了，体检替它瞒过去就失去意义了。
                kind, payload = self.get(cur, use_cache=False)
            except RepoError as e:
                bad_ids[cur] = str(e)
                report["bad"].append({"oid": cur, "why": str(e)})
                continue
            if kind != want:
                bad_ids[cur] = "对象类型不对（该是 %s，实际 %s）" % (want, kind)
                report["bad"].append({"oid": cur, "why": bad_ids[cur]})
                continue
            if kind == "commit":
                try:
                    info = json.loads(payload.decode("utf-8"))
                except ValueError:
                    bad_ids[cur] = "提交内容不是合法 JSON"
                    report["bad"].append({"oid": cur, "why": bad_ids[cur]})
                    continue
                if info.get("tree"):
                    stack.append((info["tree"], "tree"))
                for p in (info.get("parents") or []):
                    if p:
                        stack.append((p, "commit"))
            elif kind == "tree":
                try:
                    items = json.loads(payload.decode("utf-8"))
                except ValueError:
                    bad_ids[cur] = "目录内容不是合法 JSON"
                    report["bad"].append({"oid": cur, "why": bad_ids[cur]})
                    continue
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict) and it.get("h"):
                        stack.append((it["h"], "tree" if it.get("t") == "d" else "blob"))

        report["missing"] = sorted(missing)
        report["dangling"] = sorted(o for o in objs if o not in reachable and o not in bad_ids)
        report["ok"] = not (report["missing"] or report["bad"] or report["errors"])
        return report

    # ------------------------------------------------------------------ 对象
    def _obj_path(self, oid: str) -> str:
        if (not oid or len(oid) != 64
                or any(c not in "0123456789abcdef" for c in oid)):
            raise RepoError("对象号不合法：%r" % (oid[:80] if isinstance(oid, str) else oid))
        # 所有用到它的地方都是系统调用（open/stat/isfile/getsize），
        # 加上前缀不会漏进界面显示或路径比较
        return fs_path(self._p("objects", oid[:2], oid[2:]))

    def has(self, oid: str) -> bool:
        try:
            return os.path.isfile(self._obj_path(oid))
        except RepoError:
            return False

    def put(self, kind: str, payload: bytes) -> str:
        header = kind.encode() + b"\n" + str(len(payload)).encode() + b"\n"
        oid = hashlib.sha256(header + payload).hexdigest()
        p = self._obj_path(oid)
        if os.path.isfile(p):
            return oid
        try:
            with _OBJECT_LOCK:
                if os.path.isfile(p):
                    return oid
                _atomic_write(p, zlib.compress(header + payload, 6))
        except OSError:
            # 内容寻址：路径只由内容决定，别人已经写进去了就等于我写成了
            if not os.path.isfile(p):
                raise
        return oid

    def _decompress_limited(self, data: bytes, limit: int = MAX_RAW_OBJECT) -> bytes:
        """解压但**绝不**让结果超过 limit 字节。

        `zlib.decompress` 会一路解到底：收到一个几十 GB 的解压流时，内存先炸，
        进程直接没了。这里用 decompressobj 分批解，一旦超过上限就判非法。
        """
        d = zlib.decompressobj()
        try:
            raw = d.decompress(data, limit + 1)
        except zlib.error as e:
            raise RepoError("对象解压失败：%s" % e)
        if len(raw) > limit or d.unconsumed_tail:
            raise RepoError("对象解压后超过上限（%d MB），拒绝接收" % (limit >> 20))
        if not d.eof:
            raise RepoError("对象数据不完整")
        return raw

    def get(self, oid: str, use_cache: bool = True):
        p = self._obj_path(oid)
        if use_cache:
            hit = _obj_cache_get(p)
            if hit is not None:
                return hit
        try:
            st = os.stat(p)
            with open(p, "rb") as f:
                raw = self._decompress_limited(f.read())
            kind, rest = raw.split(b"\n", 1)
            n, payload = rest.split(b"\n", 1)
            if len(payload) != int(n):
                raise RepoError("对象长度不符：%s" % oid)
            # 长度对不代表内容对：磁盘坏道、手改文件都能同长度改掉几个字节。
            # 对象是按 sha256 命名的，重算一遍就知道，代价只有一次哈希。
            if hashlib.sha256(raw).hexdigest() != oid:
                raise RepoError("对象校验不过（盘上文件被改过或磁盘损坏）：%s" % oid)
            kind_s = kind.decode()
            if use_cache:
                _obj_cache_put(p, kind_s, payload, st)
            return kind_s, payload
        except (OSError, zlib.error, ValueError) as e:
            raise RepoError("对象损坏 %s：%s" % (oid, e))

    def put_raw_object(self, oid: str, data: bytes) -> bool:
        """同步接收：校验后落盘，返回是否新增

        解压有上限：这是从**网络**收到的东西，一个几百 KB 的全零流解开来能是
        几十 GB。以前 `zlib.decompress(data)` 一把梭，一个请求就能把服务进程
        的内存吃干净；现在边解边数，超过上限立刻判非法。
        """
        try:
            raw = self._decompress_limited(data)
            kind, rest = raw.split(b"\n", 1)
            n, payload = rest.split(b"\n", 1)
            if len(payload) != int(n):
                raise ValueError("长度不符")
        except RepoError:
            raise
        except Exception as e:
            raise RepoError("收到损坏对象 %s：%s" % (oid, e))
        if hashlib.sha256(raw).hexdigest() != oid:
            raise RepoError("对象哈希不匹配：%s" % oid)
        p = self._obj_path(oid)
        if os.path.isfile(p):
            return False
        _atomic_write(p, data)
        return True

    def object_bytes(self, oid: str) -> bytes:
        p = self._obj_path(oid)
        with open(p, "rb") as f:
            return f.read()

    def object_path(self, oid: str) -> str:
        return self._obj_path(oid)

    def payload_size(self, oid: str) -> int:
        """只解压开头一小段读出对象头里的长度，不用把整个大文件解压一遍"""
        d = zlib.decompressobj()
        try:
            with open(self._obj_path(oid), "rb") as f:
                buf = d.decompress(f.read(1 << 16))
        except (OSError, zlib.error) as e:
            raise RepoError("对象损坏 %s：%s" % (oid, e)) from None
        i = buf.find(b"\n")
        j = buf.find(b"\n", i + 1)
        if i < 0 or j < 0:
            raise RepoError("对象头损坏：%s" % oid)
        try:
            return int(buf[i + 1:j])
        except ValueError:
            raise RepoError("对象头损坏：%s" % oid) from None

    def iter_payload(self, oid: str, chunk: int = 1 << 20):
        """流式解压出文件内容本身（跳过 kind/长度头），大文件不进内存"""
        it = self._iter_raw(oid, chunk)
        buf = b""
        for piece in it:
            buf += piece
            i = buf.find(b"\n")
            if i >= 0:
                j = buf.find(b"\n", i + 1)
                if j >= 0:
                    try:
                        left = int(buf[i + 1:j])
                    except ValueError:
                        raise RepoError("对象头损坏：%s" % oid) from None
                    buf = buf[j + 1:]
                    break
            if len(buf) > 1 << 20:
                raise RepoError("对象头异常：%s" % oid)
        else:
            raise RepoError("对象头损坏：%s" % oid)
        while left > 0:
            if buf:
                take = buf[:left]
                left -= len(take)
                buf = b""
                yield take
                continue
            try:
                buf = next(it)
            except StopIteration:
                raise RepoError("对象长度不符：%s" % oid) from None

    def _iter_raw(self, oid: str, chunk: int = 1 << 20):
        """流式解压对象（kind\\nlen\\npayload），大对象不进内存。

        解压失败一律翻译成 RepoError：调用方（恢复、导出、预览）只认得这一种错，
        漏出去一个 zlib.error 就会变成 500，或者让「恢复失败要回滚」那条路走不到。
        """
        d = zlib.decompressobj()
        try:
            with open(self._obj_path(oid), "rb") as f:
                while True:
                    c = f.read(chunk)
                    if not c:
                        tail = d.flush()
                        if tail:
                            yield tail
                        return
                    out = d.decompress(c)
                    if out:
                        yield out
        except (OSError, zlib.error) as e:
            raise RepoError("对象损坏 %s：%s" % (oid, e)) from None

    def blob_to_file(self, oid: str, dst: str):
        """把对象明文流式写到 dst；返回 (kind, 字节数)

        边写边算 sha256，写完再核对一次对象号：盘上坏一个字节也当场发现。
        坏对象绝不落到 dst 上——先写临时文件，验过了才替换，图纸文件不会变成半截。
        """
        it = self._iter_raw(oid)
        buf = b""
        for chunk in it:
            buf += chunk
            if buf.count(b"\n") >= 2:
                break
            if len(buf) > 1 << 20:
                raise RepoError("对象头异常：%s" % oid)
        i = buf.find(b"\n")
        j = buf.find(b"\n", i + 1)
        if i < 0 or j < 0:
            raise RepoError("对象头损坏：%s" % oid)
        kind_b = buf[:i]
        kind = kind_b.decode("ascii", "replace")
        try:
            n = int(buf[i + 1:j])
        except ValueError:
            raise RepoError("对象头损坏：%s" % oid) from None
        first = buf[j + 1:]
        # 先加长路径前缀，临时文件跟着它取名：两者同一个目录，替换才是原子的。
        # 这之后不能再拿 os.path.abspath 去加工（它会把 \\?\ 前缀洗掉）。
        dst = fs_path(dst)
        tmp = dst + PART_SUFFIX
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        h = hashlib.sha256()
        h.update(kind_b + b"\n" + str(n).encode() + b"\n")
        total = 0
        try:
            with open(tmp, "wb") as out:
                out.write(first)
                h.update(first)
                total += len(first)
                for chunk in it:
                    out.write(chunk)
                    h.update(chunk)
                    total += len(chunk)
                # 落盘再替换：断电后不能留下一个长度对、内容空的「图纸」。
                # 这一步慢几毫秒，换来的是「报成功就一定在盘上」。
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            # 解压炸了、盘满了、被中断：临时文件绝不能留在用户的图纸目录里
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        if total != n:
            os.remove(tmp)
            raise RepoError("对象长度不符：%s（%d≠%d）" % (oid, total, n))
        if h.hexdigest() != oid:
            os.remove(tmp)
            raise RepoError(
                "对象校验不过（盘上这个对象被改过或磁盘坏了）：%s。目标文件没有动，"
                "请用「体检」查出坏对象，再从另一台机器同步补回来。" % oid)
        os.replace(tmp, dst)
        return kind, total

    def put_raw_file(self, oid: str, src: str) -> bool:
        """同步接收大对象：从临时文件流式校验落盘，返回是否新增"""
        dest = self._obj_path(oid)
        if os.path.isfile(dest):
            return False
        h = hashlib.sha256()
        d = zlib.decompressobj()
        tmp = dest + ".tmp.%d.%d" % (os.getpid(), int(time.time() * 1000))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with open(src, "rb") as f, open(tmp, "wb") as out:
                while True:
                    c = f.read(1 << 20)
                    if not c:
                        break
                    out.write(c)              # 压缩数据原样落盘
                    h.update(d.decompress(c))
                    if d.eof:
                        break
                h.update(d.flush())
            if h.hexdigest() != oid:
                raise RepoError("对象哈希不匹配：%s" % oid)
            os.replace(tmp, dest)
            return True
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------ 扫描与快照
    def scan(self, root: str | None = None) -> dict:
        """返回 {相对路径: os.stat 结果}，按忽略规则过滤；root 可指定别的工作区

        用 os.scandir 而不是 os.walk：界面上每几秒就问一次「有没有改动」，这一步是纯等待。
        老写法每个文件要问系统四次（islink、lstat、stat、isfile），现在一次 stat 拿全
        （名字、是否目录、是否链接、大小、时间全从目录项和这一次 stat 来）。三千个零件
        的项目里这是每几秒被省下来的一千多次系统调用。
        """
        base = fs_path(os.path.abspath(root or self.root))
        pats = self.ignore_patterns
        meta = META_DIR
        found = {}
        stack = [("", base)]
        while stack:
            rel_dir, abs_dir = stack.pop()
            try:
                with os.scandir(abs_dir) as it:
                    # 先收完再关句柄；顺便排序，让结果顺序和以前一致
                    ents = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in ents:
                name = e.name
                rel = rel_dir + "/" + name if rel_dir else name
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                # junction/符号链接一律不跟随：否则会把项目外文件卷进快照，恢复时误删
                if e.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
                    continue
                if stat_mod.S_ISDIR(st.st_mode):
                    if name == meta or ignored(rel, pats):
                        continue
                    # 每下一层都要重新加前缀：只给根目录加过是不够的，
                    # 目录深到某一层超过 260 时 scandir 就抛错，整棵子树被悄悄跳过
                    # （在 167 上实测过：318 字符的文件能被写出来，却记录不进去）。
                    stack.append((rel, fs_path(e.path)))
                elif stat_mod.S_ISREG(st.st_mode):
                    if ignored(rel, pats) or rel.split("/")[0] == meta:
                        continue
                    found[rel] = st
        return found

    def _hash_file(self, abspath: str):
        """流式计算 blob 的 oid 与大小"""
        abspath = fs_path(abspath)
        for _ in range(3):
            st0 = os.stat(abspath)
            size = st0.st_size
            h = hashlib.sha256()
            h.update(b"blob\n" + str(size).encode() + b"\n")
            n = 0
            with open(abspath, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    n += len(chunk)
                    h.update(chunk)
            st1 = os.stat(abspath)
            if n == size and (st1.st_mtime_ns, st1.st_size) == (st0.st_mtime_ns, st0.st_size):
                return h.hexdigest(), size
        raise RepoError("文件在记录过程中持续变化：%s" % abspath)

    def _store_file(self, abspath: str, level: int | None = None):
        """压缩落盘，边读边校验"""
        abspath = fs_path(abspath)
        for _ in range(3):
            st0 = os.stat(abspath)
            size = st0.st_size
            header = b"blob\n" + str(size).encode() + b"\n"
            h = hashlib.sha256()
            h.update(header)
            # CAD/压缩包这类大文件压缩率≈1，用 1 级省 3-5 倍 CPU；小文件仍用 6 级
            lv = 6 if level is None else level
            if level is None and size > (2 << 20):
                lv = 1
            comp = zlib.compressobj(lv)
            os.makedirs(fs_path(self._p("objects")), exist_ok=True)
            # 临时名必须带线程号 + 纳秒：只带 pid+毫秒时，同一进程里两个线程在同一毫秒
            # 写文件会撞同一个临时文件——轻则这次记录悄悄少几个文件，重则把别人的字节
            # 挪进你的对象里（对象库被污染）。
            tmp = fs_path(self._p("objects", "tmp.%d.%d.%d" % (
                os.getpid(), threading.get_ident(), time.time_ns())))
            n = 0
            with open(abspath, "rb") as src, open(tmp, "wb") as dst:
                dst.write(comp.compress(header))
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    n += len(chunk)
                    h.update(chunk)
                    dst.write(comp.compress(chunk))
                dst.write(comp.flush())
            oid = h.hexdigest()
            try:
                st1 = os.stat(abspath)
            except OSError:
                st1 = st0
            if n != size or (st1.st_mtime_ns, st1.st_size) != (st0.st_mtime_ns, st0.st_size):
                os.remove(tmp)
                continue
            dest = self._obj_path(oid)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # 落库这一步串行化：Windows 上两个线程同时替换同一个对象路径会抛
            # 共享冲突，被当成「文件读不了」就把这个文件从这次记录里丢掉了
            with _OBJECT_LOCK:
                if os.path.isfile(dest):
                    os.remove(tmp)
                else:
                    os.replace(tmp, dest)
            return oid, size
        raise RepoError("文件在记录过程中持续变化：%s" % abspath)

    def build(self, write: bool = True, skip=None, cache=None):
        """构建当前工作目录树；返回 (files, root_tree_oid)

        skip：这次明确不收录的路径（正在被别的程序写的小文件），沿用上一版内容。
        cache：已经读好的 index.json（省一次磁盘读；看状态那条热路径上很有用）。
        """
        if cache is None:
            cache = (self._read_json("index.json", {"files": {}}) or {}).get("files", {})
        skip = set(skip or ())
        files = {}
        skipped = [{"path": rel, "error": "正在写入（本次未收录）", "soft": True}
                   for rel in sorted(skip)]
        for rel, st in self.scan().items():
            if rel in skip:
                continue
            c = cache.get(rel)
            ap = fs_path(os.path.join(self.root, rel.replace("/", os.sep)))
            if (
                c
                and c.get("mtime") == st.st_mtime_ns
                and c.get("size") == st.st_size
                and (write is False or self.has(c.get("oid", "")))
            ):
                files[rel] = {"oid": c["oid"], "size": st.st_size, "mtime": st.st_mtime_ns}
                continue
            try:
                oid, size = self._store_file(ap) if write else self._hash_file(ap)
            except (RepoError, OSError) as e:
                # 「路径太长」要单独认出来：它不是「文件正被占用」，是项目结构的问题，
                # 换一套话说清楚才好让人知道去哪儿改。
                long_err = _too_long_err(e, ap)
                if long_err:
                    skipped.append({"path": rel, "error": long_err, "long": True})
                    continue
                # 单个文件被占用/正在保存不应让整次记录失败
                skipped.append({"path": rel, "error": str(e)})
                continue
            files[rel] = {"oid": oid, "size": size, "mtime": st.st_mtime_ns}
        long_bad = [x for x in skipped if x.get("long")]
        if long_bad and write:
            # 这个问题不会自己好：硬记一版只会让它悄悄少几个零件（以后恢复就删掉了）。
            self.last_skipped = skipped
            raise RepoError(
                "有 %d 个文件的路径长得连系统都打不开，本次记录已中止，工作区没有改动：\n%s\n"
                "把中间几层文件夹改短、或把项目挪到浅一点的目录，再记录。"
                % (len(long_bad), "\n".join(x["error"] for x in long_bad[:3])))
        if skipped:
            # 读不了的文件沿用上一版内容，绝不能因为读不到就从快照里消失（否则恢复时会误删）。
            # 看状态时也一样：SolidWorks 正锁着刚存的文件，把它报成「删除」比报成「没变」危险得多。
            head = self.head_oid()
            base = {}
            if head:
                try:
                    _, base = self.files_of(head)
                except RepoError:
                    base = {}
            for item in list(skipped):
                b = base.get(item["path"])
                if b:
                    files[item["path"]] = {"oid": b["oid"], "size": b["size"],
                                           "mtime": 0, "stale": True}
                    item["kept_previous"] = True
            # 上一版也没有的文件：绝不能就这么从快照里消失——这一版一旦被恢复，
            # 那个文件就会被当成「该删的」删掉。宁可这次记不成，也不能记个残的。
            # （明确因为「正在写入」跳过的那些除外：它们本来就没进过版本。）
            lost = [x for x in skipped
                    if not x.get("kept_previous") and not x.get("soft")]
            if lost and write:
                self.last_skipped = skipped
                raise RepoError(
                    "有 %d 个文件读不了（多半正被 SolidWorks 等程序占用），本次记录已中止，"
                    "工作区没有做任何改动。请先关掉占用它的程序再记录。比如：%s"
                    % (len(lost), "、".join(x["path"] for x in lost[:5])))
        # write=False 只是拿一张「工作区现在长什么样」的清单去和 HEAD 比，四个调用点
        # 全都丢掉了根树 oid —— 以前这里每次都白算一遍（每个目录一次 JSON 序列化）。
        # 真要这个 oid 用 _tree_oid_from() 显式算，别在轮询路径上算。
        root = self._build_trees(files) if write else ""
        self.last_skipped = skipped
        return files, root

    def _tree_oid_from(self, files) -> str:
        """不落盘地算出根树 oid（用于状态比较）"""
        nodes = {}
        for rel, info in files.items():
            d, _, name = rel.rpartition("/")
            nodes.setdefault(d, []).append(
                {"t": "b", "h": info["oid"], "n": name, "s": info["size"]}
            )
        pending = {d: list(v) for d, v in nodes.items()}
        if not pending:
            payload = b"[]"
            return hashlib.sha256(b"tree\n%d\n" % len(payload) + payload).hexdigest()
        while pending:
            d = max(pending, key=_depth)
            items = pending.pop(d)
            payload = json.dumps(sorted(items, key=lambda x: x["n"]),
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            oid = hashlib.sha256(
                b"tree\n" + str(len(payload)).encode() + b"\n" + payload).hexdigest()
            if d:
                parent, _, name = d.rpartition("/")
                pending.setdefault(parent, []).append({"t": "d", "h": oid, "n": name})
            else:
                return oid
        raise RepoError("tree 构建失败")

    def _build_trees(self, files) -> str:
        nodes = {}
        for rel, info in files.items():
            d, _, name = rel.rpartition("/")
            nodes.setdefault(d, []).append(
                {"t": "b", "h": info["oid"], "n": name, "s": info["size"]}
            )
        if not nodes:
            return self.put("tree", b"[]")
        pending = {d: list(v) for d, v in nodes.items()}
        while pending:
            d = max(pending, key=_depth)
            items = pending.pop(d)
            payload = json.dumps(sorted(items, key=lambda x: x["n"]),
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            oid = self.put("tree", payload)
            if d:
                parent, _, name = d.rpartition("/")
                pending.setdefault(parent, []).append({"t": "d", "h": oid, "n": name})
            else:
                return oid
        raise RepoError("tree 构建失败")

    # ------------------------------------------------------------------ 提交
    def unstable_files(self, window: float = 3.0, wait: float = 0.12, scanned=None) -> list:
        """找出「还在被写」的文件：刚动过的文件隔一下再看，变了就说明没写完。

        SolidWorks 保存一个大装配要好几秒；用户点完保存立刻点「记录」，
        这时读到的就是写了一半的文件——记进版本里，以后每一条分支都带着这个残件。
        只复查最近 3 秒内动过的文件（写盘中的候选），成本是几十次 stat。

        scanned：调用方已经扫好的目录清单，省掉一次全盘扫描（记录这条路上要扫好几遍）。
        """
        now = time.time()
        recent = []
        for rel, st in (scanned if scanned is not None else self.scan()).items():
            age = now - (st.st_mtime_ns / 1e9)
            if -60 < age < window:          # 时钟有点偏也照查（负数=刚写的）
                recent.append((rel, st))
        if not recent:
            return []
        time.sleep(wait)
        bad = []
        for rel, st in recent:
            p = fs_path(os.path.join(self.root, rel.replace("/", os.sep)))
            try:
                st2 = os.stat(p)
            except OSError:
                continue                    # 文件没了/打不开，交给 build 的原有逻辑处理
            if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
                bad.append(rel)
        if bad:
            return bad
        # 还有一种漏网的：正在保存的 CAD 文件中间会停顿（写一阵、算一阵），
        # 0.12 秒那一眼恰好落在停顿里，什么都没看出来。对「要紧的」文件
        # （已在版本里的 / 体积够大的）再等 0.8 秒复核一次——代价是刚保存完
        # 立刻点记录会多等不到 1 秒，换来的是不会把存了一半的图纸记进历史。
        tracked = set()
        head = self.head_oid()
        if head:
            try:
                _, tracked = self.files_of(head)
            except RepoError:
                tracked = set()
        cand = [(rel, st) for rel, st in recent
                if rel in tracked or st.st_size >= self.BUSY_HARD_BYTES]
        if cand:
            time.sleep(self.BUSY_EXTRA_WAIT)
            for rel, st in cand:
                p = fs_path(os.path.join(self.root, rel.replace("/", os.sep)))
                try:
                    st2 = os.stat(p)
                except OSError:
                    continue
                if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
                    bad.append(rel)
        return bad

    # 「正在写」的文件分两类：
    #   hard —— 已经在版本里（用户在乎它）或者体积够大（多半是图纸/装配），
    #           宁可这次记不成，也不能把写了一半的存进版本；
    #   soft —— 没记录过的小文件，典型是日志、CAD 的 .dwl/.sv$ 之类，
    #           一有改动就永远在写，挡住整次记录等于让软件没法用。
    #           这类本次跳过、并在结果里点名，下次稳定了就自动进来。
    BUSY_HARD_BYTES = 256 * 1024
    BUSY_EXTRA_WAIT = 0.8

    def _busy_split(self, bad):
        if not bad:
            return [], []
        head = self.head_oid()
        tracked = {}
        if head:
            try:
                _, tracked = self.files_of(head)
            except RepoError:
                tracked = {}
        hard, soft = [], []
        for rel in bad:
            if rel in tracked:
                hard.append(rel)
                continue
            try:
                size = os.stat(fs_path(os.path.join(
                    self.root, rel.replace("/", os.sep)))).st_size
            except OSError:
                size = 0
            (hard if size >= self.BUSY_HARD_BYTES else soft).append(rel)
        return sorted(hard), sorted(soft)

    def _auto_message(self, old: dict, new: dict) -> str:
        """没写说明时，别只留一个时间戳——把这次动了哪些零件写进去。

        图纸目录里最常见的名字就是「侧板.sldprt」这种，写对了以后翻时间线、
        在「找零件」里看「最后一次改动说明」时都能一眼认出是哪次改的。
        """
        changed = [p for p in sorted(new) if (old.get(p) or {}).get("oid") != new[p]["oid"]]
        removed = [p for p in sorted(old) if p not in new]
        n = len(changed) + len(removed)
        stamp = time.strftime("%Y-%m-%d %H:%M")
        if not n:
            return "记录 %s" % stamp
        names = [p.split("/")[-1] for p in (changed + removed)[:3]]
        more = " 等" if n > len(names) else ""
        return "记录 %d 个文件：%s%s（%s）" % (n, "、".join(names), more, stamp)

    @_locked("记录版本", timeout=60)
    def commit(self, message: str = "", author: str | None = None, branch: str | None = None):
        branch = branch or self.branch
        # 先看盘：这一版要写多少、盘上还剩多少。以前完全没有这道检查，
        # 盘快满时会在写到一半时抛出「没有磁盘空间」——对象库里留下半截文件，
        # 用户看到的就是一句看不懂的系统错误。宁可在这里先拦住，并告诉他怎么腾地方。
        # 这一次记录只扫一遍目录：盘余量估算和「有没有文件还在写」共用同一张清单。
        # 以前这两步各扫一遍、build 又扫一遍，三千个零件的项目等于把整个目录问三遍。
        # 清单早一点点不碍事：余量只是估算，_store_file 落盘前还会重新校验 mtime。
        scanned = self.scan()
        need = self.pending_bytes(scanned)
        if need:
            disk = self.disk()
            if disk["free"] >= 0 and disk["free"] < need + MIN_FREE_BYTES:
                free_mb = disk["free"] / 1e6
                need_mb = need / 1e6
                floor_mb = MIN_FREE_BYTES / 1e6
                if disk["free"] < MIN_FREE_BYTES:
                    # 盘**整体**已经低于安全线，这次改动可能只有几 KB。
                    # 以前不分情况一律说「这次记录要写 X MB」，X 常常是 0.0——
                    # 用户看着「要写 0 MB 却被拦住」只会以为软件坏了，真正该做的是去腾地方。
                    raise RepoError(
                        "盘上只剩 %.0f MB，已经低于「至少留 %.0f MB」的安全线。"
                        "这次记录本身只要 %.1f MB——不是改动太大，是整块盘快满了。"
                        "先腾出 %.0f MB 再记录；可以点「📦 体积」清回收站/旧缓存，"
                        "或把项目挪到空一点的盘上。"
                        % (free_mb, floor_mb, need_mb, max(0.0, floor_mb - free_mb)))
                raise RepoError(
                    "盘上只剩 %.0f MB，这次记录要写 %.0f MB，写完就低于 %.0f MB 的安全线了，"
                    "所以先没动文件。可以在「📦 体积」里清回收站/缓存，"
                    "或把项目放到空一点的盘上再记录。"
                    % (free_mb, need_mb, floor_mb))
        hard, soft = self._busy_split(self.unstable_files(scanned=scanned))
        if hard:
            raise RepoError(
                "有 %d 个文件还在被写入（多半是 SolidWorks 刚点了保存、还没写完）：%s。"
                "等它写完再记录——现在记下去，存进版本的就是写了一半的文件。"
                % (len(hard), "、".join(hard[:5])))
        files, root = self.build(write=True, skip=soft)
        parent = self.head_oid(branch)
        pfiles = {}
        if parent:
            try:
                pc, pfiles = self.files_of(parent)
                if pc.get("tree") == root:
                    return None  # 无变化
            except RepoError:
                pfiles = {}
        cfg = self.config
        payload = json.dumps(
            {
                "tree": root,
                "parents": [parent] if parent else [],
                "time": now_ms(),
                "author": author or cfg.get("author", "user"),
                "message": (message or "").strip() or self._auto_message(pfiles, files),
                "stats": {
                    "files": len(files),
                    "bytes": sum(v["size"] for v in files.values()),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        oid = self.put("commit", payload)
        self._set_head(oid, branch)
        self._write_json(
            "index.json",
            {
                "files": {r: {"oid": v["oid"], "size": v["size"], "mtime": v["mtime"]}
                          for r, v in files.items()},
                "head": oid,
                "branch": branch,
                "time": now_ms(),
            },
        )
        info = json.loads(payload)
        skipped = getattr(self, "last_skipped", []) or []
        with open(self._p("logs", "log.txt"), "a", encoding="utf-8") as f:
            f.write("%s %s %s %s%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), oid[:12], branch,
                info["message"].replace("\n", " "),
                ("（%d 个文件被占用，沿用上一版）" % len(skipped)) if skipped else ""))
        return oid

    @_locked("记录版本", timeout=60)
    def commit_tree(self, tree: str, parents, message: str = "", author: str | None = None,
                    branch: str | None = None, stats: dict | None = None):
        """直接用已有树对象生成提交（合并用，支持两个父提交）"""
        branch = branch or self.branch
        cfg = self.config
        payload = json.dumps(
            {
                "tree": tree,
                "parents": [p for p in (parents or []) if p],
                "time": now_ms(),
                "author": author or cfg.get("author", "user"),
                "message": (message or "").strip()
                or "合并 %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                "stats": stats or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        oid = self.put("commit", payload)
        self._set_head(oid, branch)
        idx = self._read_json("index.json", {}) or {}
        idx["head"] = oid
        idx["branch"] = branch
        idx.setdefault("files", {})
        self._write_json("index.json", idx)
        info = json.loads(payload)
        with open(self._p("logs", "log.txt"), "a", encoding="utf-8") as f:
            f.write("%s %s %s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), oid[:12], branch,
                info["message"].replace("\n", " ")))
        return oid

    def resolve(self, oid: str) -> str:
        """把版本号前缀（如 66d07b1d37）解析成完整 id"""
        oid = (oid or "").strip().lower()
        if not oid:
            raise RepoError("缺少版本号")
        if self.has(oid):
            return oid
        if len(oid) < 4:
            raise RepoError("版本号太短：%s" % oid)
        matches = set()
        branches = self.branches() or [self.branch]
        stack = [self.head_oid(b) for b in branches]
        seen = set()
        while stack and len(seen) < 200000:
            cur = stack.pop()
            if not cur or cur in seen or not self.has(cur):
                continue
            seen.add(cur)
            if cur.startswith(oid):
                matches.add(cur)
            try:
                c = self.commit_info(cur)
            except RepoError:
                continue
            stack.extend(c.get("parents") or [])
        if len(matches) == 1:
            return matches.pop()
        if not matches:
            raise RepoError("找不到版本：%s" % oid)
        raise RepoError("版本号前缀有歧义，请多给几位：%s" % oid)

    def commit_info(self, oid: str) -> dict:
        kind, payload = self.get(oid)
        if kind != "commit":
            raise RepoError("不是提交对象：%s" % oid)
        info = json.loads(payload)
        info["id"] = oid
        return info

    def tree_of(self, oid: str) -> str:
        return self.commit_info(oid)["tree"]

    def log_graph(self, limit: int = 200):
        """所有分支可达的提交，按时间倒序，**带完整 parents**。

        老的 log() 只沿第一父提交往下走，所以界面上永远是一条直链，
        分支一旦合并就再也看不见分叉了。画分支树必须把所有父提交都带上。
        读取条数和 log() 同量级（最多 limit 个提交对象），不会更慢。
        """
        out, heads, _has_more = self.log_graph_ex(limit)
        return out, heads

    def log_graph_ex(self, limit: int = 200):
        """同上，另外告诉调用方「是不是被 limit 截断了」。

        界面原来靠 `len(commits) >= limit` 猜——项目正好有 limit 个版本时
        就会一直显示「更早的还没加载」，点一下才自愈。BFS 是因为队列没空
        才停的话，才是真的有更早的。
        """
        marks = self._read_json("marks.json", {}) or {}
        heads = {}
        for b in self.branches():
            h = self.head_oid(b)
            if h:
                heads[b] = h
        infos, seen = {}, set()
        queue = deque(heads.values())      # 用 deque 出队：list.pop(0) 每次都要整体挪一遍
        cap = max(1, int(limit))
        has_more = False
        while queue:
            if len(infos) >= cap:
                # 队列里可能只剩已经取过的 oid（合并的基点会被两个子提交各入队
                # 一次）——那不是「还有更早的」，只是重复。可达版本数正好等于
                # 一屏时，老写法会永远显示「更早的还没加载」，点一下才自愈。
                has_more = any(o and o not in seen for o in queue)
                break
            oid = queue.popleft()
            if not oid or oid in seen:
                continue
            seen.add(oid)
            try:
                c = self.commit_info(oid)
            except RepoError:
                continue
            infos[oid] = c
            for p in (c.get("parents") or []):
                if p and p not in seen:
                    queue.append(p)
        out = sorted(infos.values(),
                     key=lambda c: (c.get("time") or 0), reverse=True)
        keep = {c["id"] for c in out}
        owner = {}
        for b, h in heads.items():
            owner.setdefault(h, []).append(b)
        for c in out:
            c["parents"] = [p for p in (c.get("parents") or []) if p in keep]
            c["mark"] = (marks.get(c["id"]) or {}).get("mark")
            c["tags"] = (marks.get(c["id"]) or {}).get("tags") or []
            c["branches"] = owner.get(c["id"], [])
        return out, heads, has_more

    def count_since(self, since_ms: int, until_ms: int | None = None, cap: int = 20000):
        """从各分支头往回走，数「since_ms <= 时间 < until_ms」的版本有几个。

        返回 (个数, 是否被截断)。until_ms 不给就是 since 之后 24 小时。

        为什么不能直接用日志那一屏数：界面只取最近几百个版本，一个项目今天记了
        500 版就数不出来。也不能把整个历史都翻一遍——几千个版本的项目每次打开都
        要白读几千个对象。正常情况下提交时间是单调的（父比子老），走到比 since 还
        老的提交就不用再往下走：再往下只会更老。今天没记东西时这只是一次「看几个头」。

        但**时间戳是墙上时钟，没人保证父一定比子老**（NTP 校时、手工改时间、
        对端机器时钟慢，同步回来的提交原样保留对端时间）。所以剪枝留一天余量：
        昨天同一时刻之前的才真停。读的对象仍被 cap 限住，不会因为余量就翻整个历史。
        """
        rows, cut = self.commits_since(since_ms, until_ms, cap=cap)
        return len(rows), cut

    def commits_since(self, since_ms: int, until_ms: int | None = None, cap: int = 20000):
        """时间落在 [since, until) 里的提交（新的在前），返回 (列表, 是否被截断)。

        周报、日报都靠它：一次往回走，把区间里的版本全收下来，而不是每天各走一遍。
        """
        upper = int(until_ms) if until_ms else int(since_ms) + 86400000
        floor = int(since_ms) - 86400000
        heads = []
        for b in self.branches():
            h = self.head_oid(b)
            if h:
                heads.append(h)
        seen, queue, out = set(), deque(heads), []
        cap = max(1, int(cap))
        while queue:
            if len(out) >= cap:
                # 收到上限就不收了：结果正好 cap 条，但队列里还有没看的提交，
                # 所以真实数目「至少这么多」
                out.sort(key=lambda c: (c.get("time") or 0), reverse=True)
                return out, True
            oid = queue.popleft()
            if not oid or oid in seen:
                continue
            seen.add(oid)
            try:
                c = self.commit_info(oid)
            except RepoError:
                continue
            t = c.get("time") or 0
            if t and t < floor:
                # 比余量线还老：它和它的父提交都不可能落在这个区间
                continue
            if t and since_ms <= t < upper:
                out.append(c)
            # 时间缺失（t=0）或对端时钟跑到未来：既不能算，也不能据此停——
            # 继续往上走，宁可多读几十个对象，也别把今天真正记的版本整条剪掉
            for p in (c.get("parents") or []):
                if p and p not in seen:
                    queue.append(p)
        out.sort(key=lambda c: (c.get("time") or 0), reverse=True)
        return out, False

    def log(self, limit: int = 200, branch: str | None = None):
        oid = self.head_oid(branch)
        marks = self._read_json("marks.json", {}) or {}
        out, seen = [], set()
        while oid and oid not in seen and len(out) < max(1, int(limit)):
            seen.add(oid)
            try:
                c = self.commit_info(oid)
            except RepoError:
                break
            c["mark"] = (marks.get(oid) or {}).get("mark")
            c["tags"] = (marks.get(oid) or {}).get("tags") or []
            out.append(c)
            oid = (c.get("parents") or [None])[0]
        return out

    def count_files(self, tree_oid: str | None, depth: int = 0) -> int:
        """这棵子树里一共有多少个文件（只用于「整目录新增/删除」这种少见情况）"""
        if not tree_oid:
            return 0
        if depth > MAX_TREE_DEPTH:
            # 目录层级是人给的，但对象也可能是对端推来的：几千层嵌套会让递归爆栈，
            # 报一句看得懂的话，别让请求线程抛 RecursionError
            raise RepoError("目录树太深了（超过 %d 层），拒绝继续解析" % MAX_TREE_DEPTH)
        kind, payload = self.get(tree_oid)
        if kind != "tree":
            raise RepoError("树对象损坏：%s" % tree_oid)
        n = 0
        for item in json.loads(payload):
            n += 1 if item["t"] == "b" else self.count_files(item["h"], depth + 1)
        return n

    def tree_diff_counts(self, a_tree: str | None, b_tree: str | None, depth: int = 0):
        """两棵树之间「新增/修改/删除」各多少个文件，**只沿有差异的目录**往下走。

        `diff()` 是把两边整棵树都展开成路径表再比，三千个零件、四百个版本时
        光时间线就要一千次全树解析（实测 985 ms，界面每次滚动都卡一下）。
        这里像 git 一样逐层比：两边 oid 一样的目录直接跳过，只进真正变了的那几个
        子目录——改了一个零件，代价是「根树 + 那一层目录」两三个对象，
        和项目里有多少文件无关。
        """
        if a_tree == b_tree:
            return 0, 0, 0
        if depth > MAX_TREE_DEPTH:
            raise RepoError("目录树太深了（超过 %d 层），拒绝继续解析" % MAX_TREE_DEPTH)
        a = {}
        if a_tree:
            kind, payload = self.get(a_tree)
            if kind != "tree":
                raise RepoError("树对象损坏：%s" % a_tree)
            a = {i["n"]: i for i in json.loads(payload)}
        b = {}
        if b_tree:
            kind, payload = self.get(b_tree)
            if kind != "tree":
                raise RepoError("树对象损坏：%s" % b_tree)
            b = {i["n"]: i for i in json.loads(payload)}
        added = modified = deleted = 0
        for name, ib in b.items():
            ia = a.get(name)
            if ia is None:
                added += 1 if ib["t"] == "b" else self.count_files(ib["h"], depth + 1)
            elif ia["t"] != ib["t"]:
                # 文件和目录互换：算一删一增，别让统计凭空少掉
                deleted += 1 if ia["t"] == "b" else self.count_files(ia["h"], depth + 1)
                added += 1 if ib["t"] == "b" else self.count_files(ib["h"], depth + 1)
            elif ib["t"] == "b":
                if ia["h"] != ib["h"]:
                    modified += 1
            else:
                x, y, z = self.tree_diff_counts(ia["h"], ib["h"], depth + 1)
                added += x
                modified += y
                deleted += z
        for name, ia in a.items():
            if name not in b:
                deleted += 1 if ia["t"] == "b" else self.count_files(ia["h"], depth + 1)
        return added, modified, deleted

    def files_of(self, commit_oid: str):
        """返回 (提交信息, {路径: {oid,size}})"""
        c = self.commit_info(self.resolve(commit_oid))
        out = {}

        def walk(tree_oid, prefix):
            kind, payload = self.get(tree_oid)
            if kind != "tree":
                raise RepoError("树对象损坏：%s" % tree_oid)
            for item in json.loads(payload):
                name = item.get("n") or ""
                # 树条目名必须是单个合法文件名，绝不允许 ../ 或盘符（防止写盘越界）
                if (not name or name in (".", "..")
                        or _WIN_BAD_RX.search(name) is not None):
                    raise RepoError("树里有非法文件名：%r" % name)
                path = prefix + name
                # 元数据目录绝不能出现在快照里：对端推来的提交如果带 .zgr/config.json，
                # 落地时就会把本机的配置、分支指针、待更新标记一起改掉
                if path == META_DIR or path.startswith(META_DIR + "/"):
                    raise RepoError("快照里出现了元数据目录 %s，已拒绝：%s"
                                    % (META_DIR, path))
                if item["t"] == "d":
                    walk(item["h"], path + "/")
                else:
                    out[path] = {"oid": item["h"], "size": int(item.get("s", 0))}

        walk(c["tree"], "")
        return c, out

    # ------------------------------------------------------------------ 状态 / 对比
    def _save_cache(self, files, cache=None):
        """把扫描结果回填进 index.json，避免下次又整盘重读（大项目/网络盘必须）

        cache：build() 刚用过的那份索引；给了就不重读、也不重建一整份新字典再比较，
        直接逐个比对。工作区没变时这里必须什么也不写——三千个零件的项目里那是一条
        每分钟被问十几次的热路径，写一份几百 KB 的 JSON 只是白白磨盘。
        """
        try:
            if cache is None:
                idx = self._read_json("index.json", {}) or {}
                cache = idx.get("files") or {}
            old = cache if isinstance(cache, dict) else {}
            same = len(old) == len(files)
            if same:
                for rel, v in files.items():
                    o = old.get(rel)
                    if (o is None or o.get("oid") != v["oid"]
                            or o.get("mtime") != v["mtime"] or o.get("size") != v["size"]):
                        same = False
                        break
            if same:
                return
            idx = self._read_json("index.json", {}) or {}
            idx["files"] = {rel: {"oid": v["oid"], "size": v["size"], "mtime": v["mtime"]}
                            for rel, v in files.items()}
            self._write_json("index.json", idx)
        except (OSError, RepoError):
            pass

    # ------------------------------------------------------------------ 磁盘余量
    def disk(self) -> dict:
        """项目所在盘的余量。读数失败就返回 free=-1（此时所有预检一律放行）。"""
        try:
            u = shutil.disk_usage(self.root)
        except (OSError, ValueError):
            return {"free": -1, "total": 0, "low": False, "min_free": MIN_FREE_BYTES}
        return {"free": int(u.free), "total": int(u.total or 0),
                "low": u.free < MIN_FREE_BYTES, "min_free": MIN_FREE_BYTES}

    def pending_bytes(self, scanned=None) -> int:
        """这次记录**大概**要往盘上写多少字节（估算，不重新算哈希）。

        用索引缓存里的 (mtime, size) 比一遍：变了的就是要重新写进对象库的那些。
        索引里没有的（新文件）也算。估大一点没关系——预检宁可早一步提醒，
        也不要等写到一半才报「磁盘空间不足」，那时已经写了一部分对象了。
        scanned：调用方已经扫好的目录清单（记录时一次扫描够用好几处）。
        """
        try:
            cache = (self._read_json("index.json", {"files": {}}) or {}).get("files", {})
        except (RepoError, OSError):
            cache = {}
        try:
            scanned = scanned if scanned is not None else self.scan()
        except (RepoError, OSError):
            return 0
        total = 0
        for rel, st in scanned.items():
            c = cache.get(rel)
            if c and c.get("mtime") == st.st_mtime_ns and c.get("size") == st.st_size:
                continue
            total += int(st.st_size or 0)
        return total

    def status(self, details: bool = True):
        """工作区相对 HEAD 的改动。

        details=True 时额外给 entries / totals：每个文件的旧大小、现大小、差值，
        界面据此显示「改了多少 MB、现在多少 MB」。未改动文件不做额外 IO。
        """
        try:
            cache = (self._read_json("index.json", {"files": {}}) or {}).get("files", {})
        except (OSError, RepoError):
            cache = {}
        current, _ = self.build(write=False, cache=cache)
        self._save_cache(current, cache)
        head = self.head_oid()
        base = {}
        if head:
            try:
                _, base = self.files_of(head)
            except RepoError:
                base = {}
        added = sorted(set(current) - set(base))
        deleted = sorted(set(base) - set(current))
        modified = sorted(
            p for p in set(current) & set(base) if current[p]["oid"] != base[p]["oid"]
        )
        out = {
            "head": head,
            "branch": self.branch,
            "disk": self.disk(),
            "clean": not (added or deleted or modified),
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "counts": {"added": len(added), "modified": len(modified), "deleted": len(deleted)},
            "skipped": list(getattr(self, "last_skipped", [])),
        }
        if not details:
            return out

        def entry(path, st):
            new = current.get(path) or {}
            old = base.get(path) or {}
            new_size = int(new.get("size") or 0)
            old_size = int(old.get("size") or 0)
            return {"path": path, "st": st,
                    "old_size": old_size, "new_size": new_size,
                    "delta": new_size - old_size,
                    "mtime": int(new.get("mtime") or 0)}

        entries = [entry(p, "A") for p in added]
        entries += [entry(p, "M") for p in modified]
        entries += [entry(p, "D") for p in deleted]
        entries.sort(key=lambda e: (e["st"] != "M", -abs(e["delta"]), e["path"]))
        out["entries"] = entries
        out["totals"] = {
            "old": sum(e["old_size"] for e in entries),
            "new": sum(e["new_size"] for e in entries),
            "delta": sum(e["delta"] for e in entries),
        }
        return out

    def tree_entry(self, tree_oid: str, file: str):
        """在一个树对象里只沿目录往下找**一个**文件：{"oid","size"} 或 None。

        `files_of()` 会把整棵树的每个目录都解开——查 3000 个零件的项目里某几个
        文件「是哪一版改的」时，每翻一个提交就展开一次整棵树，几十个提交就是几十秒。
        路径多深就只解析几层。
        """
        file = norm(file).lstrip("/")
        if not tree_oid or not file:
            return None
        cur = tree_oid
        parts = file.split("/")
        for i, name in enumerate(parts):
            kind, payload = self.get(cur)
            if kind != "tree":
                raise RepoError("树对象损坏：%s" % str(cur)[:12])
            found = None
            for item in json.loads(payload):
                if (item.get("n") or "") == name:
                    found = item
                    break
            if not found:
                return None
            if i == len(parts) - 1:
                if found.get("t") == "d":
                    return None
                return {"oid": found.get("h"), "size": int(found.get("s", 0) or 0)}
            if found.get("t") != "d":
                return None
            cur = found.get("h")
        return None

    def diff(self, a: str, b: str):
        a = self.resolve(a) if a else ""
        b = self.resolve(b)
        _, fa = self.files_of(a) if a else ({}, {})
        cb, fb = self.files_of(b)
        added = sorted(set(fb) - set(fa))
        deleted = sorted(set(fa) - set(fb))
        modified = []
        for p in sorted(set(fa) & set(fb)):
            if fa[p]["oid"] != fb[p]["oid"]:
                modified.append({
                    "path": p,
                    "old_oid": fa[p]["oid"],
                    "new_oid": fb[p]["oid"],
                    "old_size": fa[p]["size"],
                    "new_size": fb[p]["size"],
                })
        return {
            "a": a, "b": b, "b_info": cb,
            "added": [{"path": p, "oid": fb[p]["oid"], "size": fb[p]["size"]} for p in added],
            "deleted": [{"path": p, "oid": fa[p]["oid"], "size": fa[p]["size"]} for p in deleted],
            "modified": modified,
            "counts": {"added": len(added), "deleted": len(deleted), "modified": len(modified)},
        }

    def commit_diff_text(self, a_commit: str | None, b_commit: str, path: str,
                         limit: int = 400):
        """按提交号 + 相对路径给出逐行差异"""
        a_commit = self.resolve(a_commit) if a_commit else None
        b_commit = self.resolve(b_commit)
        _, fa = self.files_of(a_commit) if a_commit else ({}, {})
        _, fb = self.files_of(b_commit)
        return self.diff_text((fa.get(path) or {}).get("oid"),
                              (fb.get(path) or {}).get("oid"), path, limit)

    def diff_text(self, a_oid: str, b_oid: str, path: str, limit: int = 400):
        """返回统一 diff 行；二进制或过大文件只给说明"""
        import difflib

        def load(oid):
            if not oid:
                return b""
            kind, payload = self.get(oid)
            if kind != "blob":
                raise RepoError("不是文件对象")
            return payload

        old = load(a_oid)
        new = load(b_oid)
        if max(len(old), len(new)) > 2 << 20:
            return {"binary": True, "reason": "文件过大（>2MB），仅示意"}
        try:
            olds = old.decode("utf-8").splitlines()
            news = new.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return {"binary": True, "reason": "二进制文件，无法逐行对比"}
        lines = list(difflib.unified_diff(
            olds, news, fromfile="旧/" + path, tofile="新/" + path, lineterm="", n=3))
        if len(lines) > limit:
            lines = lines[:limit] + ["… 省略 %d 行" % (len(lines) - limit)]
        return {"binary": False, "path": path, "lines": lines}

    # ------------------------------------------------------------------ 恢复 / 导出
    @_locked("恢复版本", timeout=900)
    def restore_plan(self, commit_oid: str, target: str | None = None,
                     mirror: bool = True):
        """恢复之前先算清楚会发生什么：覆盖几个、新增几个、删掉几个、要写多少字节。

        纯只读，一个文件都不动。恢复是最容易「点错了就没了」的操作，
        所以确认框里得把后果说具体，而不是一句「确定恢复？」。"""
        commit_oid = self.resolve(commit_oid)
        c, files = self.files_of(commit_oid)
        target = os.path.abspath(target or self.root)
        same = os.path.normcase(target) == os.path.normcase(self.root)
        cur = {}
        if same:
            try:
                cur, _ = self.build(write=False)
            except RepoError:
                cur = {}
        plan = {"commit": commit_oid, "message": (c or {}).get("message", ""),
                "time": (c or {}).get("time"), "target": target, "total": len(files),
                "overwrite": 0, "create": 0, "same": 0, "remove": 0, "bytes": 0,
                "overwrite_sample": [], "create_sample": [], "remove_sample": [],
                "blocked": [], "dirty": 0, "dirty_mod": 0, "dirty_new": 0,
                "dirty_sample": [], "busy": []}
        # 工作区里「还没记录」的改动，恢复时会被覆盖/删掉（有备份，但用户得先知道）
        head_files = {}
        if same:
            head = self.head_oid()
            if head:
                try:
                    _, head_files = self.files_of(head)
                except RepoError:
                    head_files = {}

        def _dirty(path, kind):
            plan["dirty"] += 1
            plan["dirty_" + kind] += 1
            if len(plan["dirty_sample"]) < 20:
                plan["dirty_sample"].append({"path": path, "kind": kind})

        for path, info in sorted(files.items()):
            dst = fs_path(os.path.join(target, path.replace("/", os.sep)))
            exists = os.path.exists(dst)
            if same and exists and (cur.get(path) or {}).get("oid") == info["oid"]:
                plan["same"] += 1
                continue
            plan["bytes"] += int(info.get("size") or 0)
            if exists:
                if os.path.isdir(dst) or not _writable(dst):
                    plan["blocked"].append(path)
                if same and path in cur:
                    was = head_files.get(path)
                    if was is None:
                        _dirty(path, "new")
                    elif was.get("oid") != cur[path].get("oid"):
                        _dirty(path, "mod")
                plan["overwrite"] += 1
                if len(plan["overwrite_sample"]) < 8:
                    plan["overwrite_sample"].append(path)
            else:
                plan["create"] += 1
                if len(plan["create_sample"]) < 8:
                    plan["create_sample"].append(path)
        if mirror:
            pats = self.ignore_patterns
            walk_root = fs_path(target)
            for dirpath, dirnames, filenames in os.walk(walk_root, topdown=True):
                rel_dir = norm(os.path.relpath(dirpath, walk_root))
                if rel_dir == ".":
                    rel_dir = ""
                dirnames[:] = [
                    d for d in dirnames
                    if d != META_DIR and not is_link_like(os.path.join(dirpath, d))]
                for fn in filenames:
                    rel = (rel_dir + "/" + fn).lstrip("/")
                    if rel in files or rel.split("/")[0] == META_DIR:
                        continue
                    if ignored(rel, pats) or is_link_like(os.path.join(dirpath, fn)):
                        continue
                    if same and rel in cur:
                        was = head_files.get(rel)
                        if was is None:
                            _dirty(rel, "new")
                        elif was.get("oid") != cur[rel].get("oid"):
                            _dirty(rel, "mod")
                    plan["remove"] += 1
                    if len(plan["remove_sample"]) < 8:
                        plan["remove_sample"].append(rel)
        # 现在读不了的文件（正被 SolidWorks 等程序占着）：恢复会覆盖它们，
        # 而它们的最新改动根本还没被读到——必须让用户在确认框里看见
        if same:
            plan["busy"] = sorted({x.get("path") for x in (self.last_skipped or [])
                                   if x.get("path")})
        return plan

    @_locked("恢复版本", timeout=900)
    def restore(self, commit_oid: str, target: str | None = None,
                mirror: bool = True, backup: bool = True):
        commit_oid = self.resolve(commit_oid)
        c, files = self.files_of(commit_oid)
        target = os.path.abspath(target or self.root)
        same = os.path.normcase(target) == os.path.normcase(self.root)
        if not same:
            t = os.path.normcase(target).rstrip("\\/") + os.sep
            r = os.path.normcase(self.root).rstrip("\\/") + os.sep
            if r.startswith(t) or t.startswith(r):
                raise RepoError("目标目录与项目目录互相包含，拒绝恢复：%s" % target)

        # 先确认所有对象都在，避免写一半才发现缺文件
        missing = [p for p, i in files.items() if not self.has(i["oid"])]
        if missing:
            raise RepoError("缺少 %d 个文件对象（如 %s），已中止，工作区未改动"
                            % (len(missing), missing[0]))

        os.makedirs(target, exist_ok=True)
        cur = {}
        if backup or same:
            try:
                cur, _ = self.build(write=False)
            except RepoError:
                cur = {}

        # 写之前先体检：目标文件能不能写、磁盘够不够。宁可一步都不做，也不能
        # 写到一半发现某个零件正被 SolidWorks 占着，把工作区搞成半新半旧。
        need, blocked = 0, []
        for path, info in sorted(files.items()):
            dst = os.path.join(target, path.replace("/", os.sep))
            unchanged = same and (cur.get(path) or {}).get("oid") == info["oid"]
            if unchanged and os.path.exists(dst):
                continue
            need += int(info.get("size") or 0)
            if not os.path.exists(dst):
                continue
            if os.path.isdir(dst) or not _writable(dst):
                blocked.append(path)
        if blocked:
            raise RepoError(
                "这些文件正被别的程序占用或设成了只读，已中止，工作区一个字节都没动："
                + "、".join(blocked[:5])
                + ("等 %d 个" % len(blocked) if len(blocked) > 5 else ""))
        try:
            free = shutil.disk_usage(target).free
        except OSError:
            free = 0
        if free and need + (64 << 20) > free:
            raise RepoError("磁盘空间不够：这次恢复要写 %.1f GB，目标盘只剩 %.1f GB"
                            % (need / 1e9, free / 1e9))

        backup_dir = None
        if backup:
            stamp = time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid()
            base = self._p("backup", stamp + ("" if same else "-外部"))
            backup_dir = base
            n = 0
            while os.path.exists(backup_dir):     # 同一秒内多次恢复也不能互相覆盖
                n += 1
                backup_dir = "%s-%d" % (base, n)

        written = 0
        written_index = {}
        created, replaced, removed, failed_remove = [], [], [], []
        # 先留个「正在进行」的记号：恢复要是被断电/强杀打断，下次启动能看见，
        # 而不是面对一个半新半旧的工作区不知道发生了什么。
        # 记号里必须带上 mirror/backup 和「新建了哪些」——否则收尾时只能靠猜：
        # 导出（mirror=False）被当成恢复去收尾，会把导出目录里别人的文件当多余删掉。
        journal = {"commit": commit_oid, "target": target, "backup": backup_dir,
                   "time": now_ms(), "pid": os.getpid(), "files": len(files),
                   "mirror": bool(mirror), "with_backup": bool(backup),
                   "created": []}

        def _journal_flush():
            try:
                self._write_json("restore-journal.json", journal)
            except OSError:
                pass

        _journal_flush()
        try:
            for path, info in sorted(files.items()):
                dst = fs_path(os.path.join(target, path.replace("/", os.sep)))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                exists = os.path.exists(dst)
                if backup_dir and exists:
                    # 项目目录内：内容没变的不用备份；外部目标：一律先备份再覆盖
                    unchanged = same and (cur.get(path) or {}).get("oid") == info["oid"]
                    if not unchanged and not os.path.isdir(dst):
                        bdst = fs_path(os.path.join(backup_dir, path.replace("/", os.sep)))
                        os.makedirs(os.path.dirname(bdst), exist_ok=True)
                        shutil.copy2(dst, bdst)
                if same and exists and (cur.get(path) or {}).get("oid") == info["oid"]:
                    pass                                # 内容没变，不用重写
                else:
                    try:
                        self.blob_to_file(info["oid"], dst)
                    except RepoError:
                        raise
                    except OSError as e:
                        raise RepoError("写入失败（可能文件被占用）：%s（%s）"
                                        % (path, e)) from None
                    if exists:
                        replaced.append(path)
                    else:
                        created.append(path)
                        journal["created"] = created
                        if len(created) % 100 == 0:
                            _journal_flush()      # 大批恢复时也随时能看出新建了什么
                    written += 1
                try:
                    st = os.stat(dst)
                    written_index[path] = {"oid": info["oid"], "size": st.st_size,
                                           "mtime": st.st_mtime_ns}
                except OSError:
                    pass

            if mirror:
                pats = self.ignore_patterns
                walk_root = fs_path(target)
                for dirpath, dirnames, filenames in os.walk(walk_root, topdown=True):
                    # 相对路径的基准也要用加了前缀的那个，否则算出来是 ..\..\C:\... 这种鬼东西
                    rel_dir = norm(os.path.relpath(dirpath, walk_root))
                    if rel_dir == ".":
                        rel_dir = ""
                    dirnames[:] = [
                        d for d in dirnames
                        if d != META_DIR and not is_link_like(os.path.join(dirpath, d))
                    ]
                    for fn in filenames:
                        rel = (rel_dir + "/" + fn).lstrip("/")
                        if rel in files or rel.split("/")[0] == META_DIR:
                            continue
                        if ignored(rel, pats):
                            continue
                        ap = os.path.join(dirpath, fn)
                        if is_link_like(ap):
                            continue
                        if backup_dir and os.path.exists(ap):
                            bdst = fs_path(os.path.join(backup_dir, rel.replace("/", os.sep)))
                            os.makedirs(os.path.dirname(bdst), exist_ok=True)
                            shutil.copy2(ap, bdst)
                        try:
                            os.remove(ap)
                            removed.append(rel)
                        except OSError as e:
                            # 删不掉的（被占用/只读）绝不能当成功：否则恢复说「好了」，
                            # 工作区里却还留着不属于这个版本的文件
                            failed_remove.append({"path": rel, "error": str(e)})
                # 清空目录（自底向上，绝不碰 .zgr 和链接目录）
                for dirpath, dirnames, filenames in os.walk(walk_root, topdown=False):
                    if os.path.abspath(dirpath) == os.path.abspath(walk_root):
                        continue
                    rel = norm(os.path.relpath(dirpath, walk_root))
                    if rel == META_DIR or rel.startswith(META_DIR + "/"):
                        continue
                    if ignored(rel, pats) or is_link_like(dirpath):
                        continue
                    try:
                        if not os.listdir(dirpath):
                            os.rmdir(dirpath)
                    except OSError:
                        pass
        except (RepoError, OSError) as e:
            # 写到一半出问题：用刚才的备份把工作区退回原样，别留半新半旧的现场
            self._rollback(target, backup_dir, created, replaced, removed)
            self.clear_pending_restore()
            raise RepoError("恢复中途失败，已回滚到原状态：%s" % e) from None

        if failed_remove and backup_dir:
            # 有备份就退回原样：留着「半新半旧 + 几个多余文件」的工作区比失败更糟
            self._rollback(target, backup_dir, created, replaced, removed)
            self.clear_pending_restore()
            raise RepoError(
                "有 %d 个文件删不掉（多半正被别的程序占用），已回滚到恢复之前，工作区没变。比如：%s"
                % (len(failed_remove), "、".join(x["path"] for x in failed_remove[:5])))

        try:
            idx = self._read_json("index.json", {}) or {}
        except RepoError:
            idx = {}
        self._write_json("index.json", {"files": written_index,
                                        "head": idx.get("head"),
                                        "branch": idx.get("branch", "main")})
        self.clear_pending_restore()
        return {"target": target, "written": written, "removed": removed,
                "backup": backup_dir, "commit": commit_oid,
                "failed_remove": failed_remove,
                "message": c.get("message", "")}

    def _rollback(self, target, backup_dir, created, replaced, removed):
        """把写了一半的恢复退回去：新写出来的删掉，被覆盖/被删的从备份拷回来。

        尽力而为——转失败时也不能再抛出异常盖掉原始错误。
        """
        for path in created:
            dst = fs_path(os.path.join(target, path.replace("/", os.sep)))
            try:
                os.remove(dst)
            except OSError:
                pass
        if not backup_dir:
            return
        for path in list(replaced) + list(removed):
            src = fs_path(os.path.join(backup_dir, path.replace("/", os.sep)))
            dst = fs_path(os.path.join(target, path.replace("/", os.sep)))
            if not os.path.isfile(src):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    os.chmod(dst, 0o666)
                except OSError:
                    pass
                shutil.copy2(src, dst)
            except OSError:
                pass

    # ------------------------------------------------------------------ 备份维护
    _BAK_RE = re.compile(r"^\d{8}-\d{6}-\d+(-外部)?(-\d+)?$")

    def backup_dirs(self):
        """备份批次目录（回收站），按时间从新到旧"""
        d = self._p("backup")
        try:
            names = os.listdir(d)
        except OSError:
            return []
        out = []
        for name in names:
            if not self._BAK_RE.match(name):
                continue            # 只认我们自己建的批次名，别的一律不碰
            p = os.path.join(d, name)
            if os.path.isdir(p):
                try:
                    out.append((os.path.getmtime(p), name, p))
                except OSError:
                    pass
        return [x[1] for x in sorted(out, reverse=True)]

    def _dir_bytes(self, p: str) -> int:
        size = 0
        for dp, _dn, fns in os.walk(fs_path(p)):
            for f in fns:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return size

    def backup_total_bytes(self) -> int:
        """回收站现在占了多大（几十批上万个文件，只读元数据，很快）"""
        total = 0
        for name in self.backup_dirs():
            total += self._dir_bytes(self._p("backup", name))
        return total

    @_locked("清理备份", timeout=300)
    def prune_backups(self, keep: int = 20, dry: bool = False,
                      max_bytes: int | None = None, min_keep: int = 10) -> dict:
        """清理旧备份：按批数（keep）/ 按体积（max_bytes），两个条件谁先到算谁。

        备份是给人反悔用的，不是历史存档；无限涨下去只会把盘塞满——盘满了就
        连一次记录都做不成，那才是真的危险。但**只按批数**删是不够的：
        一次整项目恢复的备份可能就几 GB，20 批能吃掉半个盘；反过来只按体积删，
        几十个小批次也占不了多少却会被清掉。所以两条都留：
        「至少保留最近 min_keep 批」是硬底线，无论多大都不动；
        超过 max_bytes 的，从最旧的开始删到不超为止。
        """
        keep = max(1, int(keep))
        min_keep = max(1, min(int(min_keep or keep), keep)) if max_bytes else 1
        names = self.backup_dirs()          # 从新到旧
        sizes = {n: self._dir_bytes(self._p("backup", n)) for n in names}
        drop = list(names[keep:])
        if max_bytes is not None and max_bytes > 0:
            total = sum(sizes.values())
            for i in range(len(names) - 1, min_keep - 1, -1):   # 从最旧的往前
                if total <= max_bytes:
                    break
                name = names[i]
                if name in drop:
                    continue
                drop.append(name)
                total -= sizes[name]
        freed, removed = 0, []
        for name in drop:
            p = self._p("backup", name)
            size = sizes.get(name, 0)
            if not dry:
                shutil.rmtree(p, ignore_errors=True)
                if os.path.isdir(p):
                    continue        # 删不掉（被占用）就跳过，别谎报
            freed += size
            removed.append(name)
        return {"kept": len(names) - len(removed), "removed": removed,
                "bytes": freed, "dry": bool(dry)}

    def auto_prune_backups(self) -> dict | None:
        """按项目配置自动清理回收站；没配（或设成 0）就什么都不做。

        配置键：`backup_max_mb`（默认 2048，0 = 不限）、`backup_min_keep`（默认 10）、
        `backup_keep`（默认 40）。全都不写在项目目录以外，用户随时能改。
        """
        try:
            cfg = self.config
        except RepoError:
            return None
        if cfg.get("backup_auto", True) is False:
            return None
        try:
            max_mb = int(cfg.get("backup_max_mb", 2048))
            keep = int(cfg.get("backup_keep", 40))
            min_keep = int(cfg.get("backup_min_keep", 10))
        except (TypeError, ValueError):
            max_mb, keep, min_keep = 2048, 40, 10
        if max_mb <= 0:
            return None
        names = self.backup_dirs()
        if len(names) <= min_keep:
            return None
        try:
            res = self.prune_backups(keep=keep, dry=False,
                                     max_bytes=max_mb * 1024 * 1024,
                                     min_keep=min_keep)
        except (RepoError, OSError):
            return None
        if res.get("removed"):
            return res
        return None

    # ------------------------------------------------------------------ 项目体积
    def size_report(self, budget: float = 8.0) -> dict:
        """项目在盘上到底被什么占着：图纸 / 版本历史 / 回收站 / 打包 / BOM 快照。

        只会往大里长的是「历史」和「回收站」，而这两样都能清。先把账算清楚，
        用户才知道点「清理」能省出多少——否则谁也不会去点一个不知道后果的按钮。
        扫描带时间预算：几十万个对象时宁可给「至少这么多」，也不能让界面卡死。
        """
        t0 = time.time()
        state = {"partial": False}

        def walk_size(path, skip_meta=True):
            # os.walk + getsize 是「每文件一次系统调用」；scandir 的 DirEntry
            # 在 Windows 上自带大小（FindFirstFile 一次就带回来了），
            # 两千个文件的项目光这一步差出一半时间。
            total, n, ticks = 0, 0, 0
            top = fs_path(path)
            if not os.path.isdir(top):
                return total, n
            stack = [top]
            while stack:
                d = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        ticks += 1
                        if ticks % 256 == 0 and (state["partial"]
                                                 or time.time() - t0 > budget):
                            state["partial"] = True
                            return total, n
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not (skip_meta and e.name == META_DIR):
                                    stack.append(fs_path(e.path))
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                                n += 1
                        except OSError:
                            pass
            if state["partial"] or time.time() - t0 > budget:
                state["partial"] = True
            return total, n

        def count_dirs(p):
            try:
                return len([x for x in os.listdir(p)
                            if os.path.isdir(os.path.join(p, x))])
            except OSError:
                return 0

        work, work_n = walk_size(self.root, skip_meta=True)
        objs, obj_n = walk_size(self._p("objects"))
        backup, bak_n = walk_size(self._p("backup"))
        packs, pack_n = walk_size(self._p("packs"))
        bom, bom_n = walk_size(self._p("bom"))
        # 缩略图 / DWG 预览 / 3D 网格的缓存：不删会一直长，删了完全不影响版本
        # （下次看图和转模型时重新生成）。网格一个动辄几十 MB，是最容易失控的一项。
        prev, prev_n = walk_size(self._p("previews"))
        mesh, mesh_n = walk_size(self._p("meshes"))
        meta, meta_n = walk_size(self.meta)
        return {
            "root": self.root,
            "worktree": {"bytes": work, "files": work_n},
            "objects": {"bytes": objs, "files": obj_n},
            "backup": {"bytes": backup, "files": bak_n, "batches": count_dirs(self._p("backup"))},
            "packs": {"bytes": packs, "files": pack_n},
            "bom": {"bytes": bom, "files": bom_n},
            "previews": {"bytes": prev, "files": prev_n},
            "meshes": {"bytes": mesh, "files": mesh_n},
            "meta_other": {"bytes": max(0, meta - objs - backup - packs - bom - prev - mesh),
                           "files": max(0, meta_n - obj_n - bak_n - pack_n - bom_n
                                        - prev_n - mesh_n)},
            # 合计按「实际量到的分项」加：扫着扫着超预算时，meta 可能只量了一半，
            # 直接用 work+meta 会让合计比各分项之和还小（面板占比加起来超过 100%）
            "total": work + max(meta, objs + backup + packs + bom + prev + mesh),
            "partial": state["partial"],
            "seconds": round(time.time() - t0, 2),
        }

    # ------------------------------------------------------------------ 全历史搜索
    def search_index(self, force: bool = False) -> dict:
        """把「每个路径出现在哪些版本里、被改过几次、最后一次是谁改的」建一份索引。

        三千个零件、上百个版本的项目，光靠翻清单找不到「那个侧板到底还在不在」。
        建一次索引（几百毫秒到几秒），之后每次搜索都只是内存里扫一遍。

        两处容易搞错、这里专门处理了：
        - **改动次数按提交和它的父提交比**，不是按时间上相邻的两版比。
          两条并行分支的时间交错、或者合并提交，都会让「相邻比」把没改的算成改了。
        - **「还在不在」看所有分支头**，不只看我当前在的那条线：
          「一人一条线」时零件可能只在同事的分支上，那不是删除。
        缓存签名带上当前分支和它的头，切分支立刻重建，不会给出上一分支的结论。
        """
        heads = tuple((b, self.head_oid(b)) for b in self.branches())
        cur_branch = self.branch
        sig = (heads, cur_branch, self.head_oid())
        key = os.path.normcase(self.root)
        now = time.time()
        with _SEARCH_LOCK:
            c = _SEARCH_CACHE.get(key)
            if c and not force and c["sig"] == sig and now - c["time"] < _SEARCH_TTL:
                return c["index"]
        commits = self.log_graph(limit=100000)[0]
        fmap, order = {}, []

        def files_of_cached(oid):
            if oid in fmap:
                return fmap[oid]
            try:
                _info, f = self.files_of(oid)
            except RepoError:
                f = {}
            fmap[oid] = f
            order.append(oid)
            while len(order) > 24:            # 只留最近用到的，几千个版本的整树太吃内存
                fmap.pop(order.pop(0), None)
            return f

        index = {}
        for c in commits:                      # 已经按时间倒序
            cid = c["id"]
            files = files_of_cached(cid)
            parents = [p for p in (c.get("parents") or []) if p and self.has(p)]
            pfiles = [files_of_cached(p) for p in parents]
            t = c.get("time") or 0
            for path, info in files.items():
                oid = info.get("oid") or ""
                e = index.get(path)
                if e is None:
                    e = index[path] = {"path": path, "versions": 0, "changes": 0,
                                       "first": t, "last": t, "last_commit": cid,
                                       "last_msg": c.get("message") or "",
                                       "size": info.get("size") or 0}
                e["versions"] += 1
                # 和**每一个**父提交都不一样，才算这个版本改过它；
                # 合并提交把对方的内容并进来时，和某个父相同 → 不算改动
                if not pfiles:
                    changed = True
                else:
                    changed = all((pf.get(path) or {}).get("oid") != oid for pf in pfiles)
                if changed:
                    e["changes"] += 1
                e["first"] = t                 # 越往后越老，最后落下的就是首次出现
        # 还在不在：当前分支 / 其它分支 / 都没了
        head_files = {}
        h = self.head_oid()
        if h:
            head_files = files_of_cached(h)
        live_any = {}
        for b, ho in heads:
            if ho and ho != h:
                live_any.update(files_of_cached(ho))
        for path, e in index.items():
            hi = head_files.get(path)
            e["in_head"] = hi is not None
            e["in_branches"] = e["in_head"] or path in live_any
            if hi:
                e["size"] = hi.get("size") or e.get("size") or 0
        with _SEARCH_LOCK:
            if len(_SEARCH_CACHE) > 8:         # 开过很多项目也别攒着
                _SEARCH_CACHE.clear()
            _SEARCH_CACHE[key] = {"sig": sig, "index": index, "time": now}
        return index

    def search_history(self, q: str, limit: int = 50, force: bool = False) -> dict:
        """按文件名/路径片段跨所有版本找零件（不区分大小写）"""
        q = (q or "").strip().lower()
        index = self.search_index(force=force)
        if not q:
            return {"query": q, "total": 0, "rows": [], "scanned": len(index)}
        hit = []
        for path, e in index.items():
            low = path.lower()
            if q not in low:
                continue
            name = low.rsplit("/", 1)[-1]
            score = 0 if name.startswith(q) else (1 if q in name else 2)
            # 排序：真的没了（任何分支都没有）垫底，然后文件名命中、最近改过的在前
            hit.append((e.get("in_branches", e["in_head"]) is False, score,
                        -(e["last"] or 0), path, e))
        hit.sort()
        rows = []
        for _dead, _s, _t, _p, e in hit[:max(1, int(limit))]:
            rows.append({"path": e["path"], "versions": e["versions"],
                         "changes": e["changes"], "size": e["size"],
                         "in_head": e["in_head"],
                         "in_branches": e.get("in_branches", e["in_head"]),
                         "last": e["last"],
                         "first": e["first"], "last_commit": e["last_commit"],
                         "last_msg": e["last_msg"]})
        return {"query": q, "total": len(hit), "rows": rows, "scanned": len(index)}

    # ------------------------------------------------------------------ 对象回收
    @_locked("回收对象", timeout=900)
    def gc(self, dry: bool = True, keep_days: int = 7, progress=None) -> dict:
        """删掉「任何分支都走不到」的对象，把项目变小。

        只删两类东西：①删掉的分支/退回旧版本留下的历史；②传了一半又没被引用的对象。
        为了不误删：
        - 先跑一遍体检，**体检不过就一个都不删**（分不清哪些是真孤儿）；
        - 只删「最后改动时间超过 keep_days 天」的（同步是对象先到、分支指针后到，
          刚进来的对象可能正处在别人还没推完的中间状态）；
        - 默认 dry=True，只报数不落手。
        """
        keep_days = max(0, int(keep_days))
        rep = self.fsck()
        # 「库里有对象、但没有任何分支指向它们」不是「都是垃圾」，是 refs 丢了
        # （同步中断、迁移、外部误删）。这种状态下清一次就是整库历史没了。
        if rep["objects"] and not any(v for v in (rep["refs"] or {}).values()):
            raise RepoError(
                "这个项目的分支指针是空的，但库里还有 %d 个对象——这多半是分支信息丢了"
                "（同步中断、搬目录、外部误删），不是「这些数据没人要」。"
                "这次一个对象都没删。先恢复分支指针（或从另一台机器 repair）再来清理。"
                % rep["objects"])
        if not rep["ok"]:
            raise RepoError(
                "体检没通过，这次一个对象都没删（分不清哪些是真没人要的）。"
                "问题：缺 %d 个对象、坏 %d 个、引用问题 %d 条。先跑「体检」修好再来清理。"
                % (len(rep["missing"]), len(rep["bad"]), len(rep["errors"])))
        # 打了标记/标签的版本，分支没了也不能清：标记只存在 marks.json 里，
        # 可达性算法根本看不见它，一跑 GC 就会把「客户确认版」连同内容一起删掉。
        keep_marked = self._reachable_from_marks()
        now = time.time()
        cutoff = now - keep_days * 86400
        victims, kept_recent, total_bytes, kept_by_marks = [], 0, 0, 0
        for oid in rep["dangling"]:
            if oid in keep_marked:
                kept_by_marks += 1
                continue
            try:
                st = os.stat(self._obj_path(oid))
            except OSError:
                continue
            if st.st_mtime > cutoff:
                kept_recent += 1
                continue
            victims.append(oid)
            total_bytes += st.st_size
        plan = {"dry": bool(dry), "objects": rep["objects"], "dangling": len(rep["dangling"]),
                "removed": 0, "bytes": total_bytes if dry else 0, "kept_recent": kept_recent,
                "kept_marked": kept_by_marks, "keep_days": keep_days,
                "sample": victims[:20], "total": len(victims)}
        if dry or not victims:
            return plan
        removed, freed = 0, 0
        for n, oid in enumerate(victims):
            p = self._obj_path(oid)
            try:
                size = os.path.getsize(p)
                os.remove(p)
            except OSError:
                continue                  # 删不掉的（被占用）跳过，不谎报
            removed += 1
            freed += size
            if progress and n % 200 == 0:
                try:
                    progress(n, len(victims))
                except Exception:
                    pass
        self._prune_empty_objdirs()
        if removed:
            # 刚删掉的对象可能还在内存缓存里：不清的话，下一步「取这个对象」
            # 会从缓存里读到一份已经不在盘上的历史，行为看着像鬼打墙。
            obj_cache_clear()
        plan.update({"removed": removed, "bytes": freed})
        return plan

    def _reachable_from_marks(self) -> set:
        """从「打了标记/标签的提交」出发能走到的全部对象。

        标记存在 marks.json 里，不占任何 ref；分支一删，那一段历史在
        `fsck` 眼里就是孤儿。可对用户来说「我标了绿、写了客户确认」的版本
        显然不是可以随便删的垃圾——所以 GC 单独把这一段保护起来。
        """
        marks = self.marks()
        if not marks:
            return set()
        roots = []
        for k in marks:
            try:
                oid = self.resolve(k)
            except (RepoError, OSError):
                continue
            if oid and self.has(oid):
                roots.append(oid)
        if not roots:
            return set()
        keep, stack = set(), [(o, "commit") for o in roots]
        while stack:
            cur, want = stack.pop()
            if not cur or cur in keep or not self.has(cur):
                continue
            keep.add(cur)
            if want == "blob":
                continue
            try:
                kind, payload = self.get(cur)
                data = json.loads(payload.decode("utf-8"))
            except (RepoError, ValueError, UnicodeDecodeError):
                continue
            if kind == "commit" and isinstance(data, dict):
                if data.get("tree"):
                    stack.append((data["tree"], "tree"))
                for p in (data.get("parents") or []):
                    if p:
                        stack.append((p, "commit"))
            elif kind == "tree" and isinstance(data, list):
                for it in data:
                    if isinstance(it, dict) and it.get("h"):
                        stack.append((it["h"], "tree" if it.get("t") == "d" else "blob"))
        return keep

    def _prune_empty_objdirs(self):
        """对象按 oid 前两位分 256 个目录，回收完可能剩一堆空目录"""
        base = fs_path(self._p("objects"))
        try:
            dirs = os.listdir(base)
        except OSError:
            return
        for d in dirs:
            p = os.path.join(base, d)
            try:
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
            except OSError:
                pass

    # ------------------------------------------------------------------ 恢复中断日志
    def _journal_path(self) -> str:
        return self._p("restore-journal.json")

    def pending_restore(self):
        """上次恢复是不是做到一半就断电/被杀掉了？有就返回当时的现场

        记号文件存在但读不出来，也要报出来（corrupt=True）：那说明确实断过，
        只是内容坏了；当成「什么都没有」会让人对着半新半旧的工作区毫不知情。
        """
        p = self._journal_path()
        if not os.path.isfile(p):
            return None
        try:
            j = self._read_json("restore-journal.json", None)
        except (RepoError, OSError):
            j = None
        if not isinstance(j, dict) or not j.get("commit"):
            return {"commit": None, "target": self.root, "backup": None,
                    "corrupt": True, "time": None,
                    "backup_exists": False, "target_exists": True}
        j["backup_exists"] = bool(j.get("backup")) and os.path.isdir(j["backup"])
        j["target_exists"] = bool(j.get("target")) and os.path.isdir(j["target"])
        return j

    def clear_pending_restore(self):
        try:
            os.remove(self._journal_path())
        except OSError:
            pass

    @_locked("收尾中断的恢复", timeout=900)
    def finish_pending_restore(self, mode: str = "again") -> dict:
        """收尾上次中断的恢复。

        again  —— 把同一个版本再恢复一遍（恢复是幂等的，跑完状态一定一致）
        backup —— 把备份批次里的文件原样拷回去（回到恢复之前的样子）
        """
        j = self.pending_restore()
        if not j:
            raise RepoError("没有中断的恢复需要处理")
        if j.get("corrupt") and mode != "backup":
            raise RepoError("上次恢复的记号文件读不出来了，不知道当时恢复到哪个版本。"
                            "可以先用「体检」确认对象都在，再看工作区是否需要重新恢复；"
                            "确认无误后清掉这个记号即可。")
        target = j.get("target") or self.root
        if mode == "backup":
            src = j.get("backup") or ""
            if not src or not os.path.isdir(fs_path(src)):
                raise RepoError("这批备份已经找不到了，只能重新恢复到这个版本")
            n = 0
            srcp = fs_path(src)
            for dp, _dn, fns in os.walk(srcp):
                for f in fns:
                    a = os.path.join(dp, f)
                    rel = norm(os.path.relpath(a, srcp))
                    dst = fs_path(os.path.join(target, rel.replace("/", os.sep)))
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        try:
                            os.chmod(dst, 0o666)
                        except OSError:
                            pass
                        shutil.copy2(a, dst)
                        n += 1
                    except OSError as e:
                        raise RepoError("从备份拷回 %s 失败：%s" % (rel, e)) from None
            # 中断的恢复「新建」出来的文件不在备份里（备份只存被覆盖的旧文件），
            # 只拷回备份的话它们会留在工作区，状态里显示成一批莫名其妙的新增。
            # 记号里记着当时新建了哪些，按内容再核一次才删——用户事后自己改过的不动。
            commit_oid = j.get("commit") or ""
            cfiles = {}
            if commit_oid and self.has(commit_oid):
                try:
                    _, cfiles = self.files_of(commit_oid)
                except RepoError:
                    cfiles = {}
            gone, kept = [], []
            for rel in (j.get("created") or []):
                dst = fs_path(os.path.join(target, rel.replace("/", os.sep)))
                if not os.path.isfile(dst):
                    continue
                want = (cfiles.get(rel) or {}).get("oid")
                try:
                    oid, _ = self._hash_file(dst)
                except (RepoError, OSError):
                    oid = None
                if want and oid != want:
                    kept.append(rel)       # 恢复之后用户又改过它，别删
                    continue
                try:
                    os.remove(dst)
                    gone.append(rel)
                except OSError as e:
                    raise RepoError("删掉恢复时新建的 %s 失败（可能被占用）：%s"
                                    % (rel, e)) from None
            # 索引跟工作区对齐，否则界面上会显示一堆假的「未记录改动」
            try:
                cur, _ = self.build(write=False)
                idx = self._read_json("index.json", {}) or {}
                self._write_json("index.json", {
                    "files": {r: {"oid": v["oid"], "size": v["size"], "mtime": v["mtime"]}
                              for r, v in cur.items()},
                    "head": idx.get("head"), "branch": idx.get("branch", "main")})
            except (RepoError, OSError):
                pass
            self.clear_pending_restore()
            return {"mode": "backup", "restored": n, "target": target,
                    "deleted": gone, "kept_modified": kept}
        # 默认：重新恢复一遍（沿用当时那次恢复的 mirror/backup 选择——
        # 导出被强杀后按「恢复」去收尾，会顺手删掉导出目录里别人的文件）
        r = self.restore(j["commit"], target,
                         mirror=j.get("mirror", True), backup=j.get("with_backup", True))
        return {"mode": "again", **r}

    # ------------------------------------------------------------------ 标记 / 标签
    @_locked("打标记")
    def set_mark(self, commit_oid: str, mark=None, tags=None):
        """给某个版本打颜色/标签。

        整份 marks.json 的读-改-写：命令行 `zgr mark` 和网页同时打两个版本时，
        后写的会把前一个标记整份盖掉。这不只是「少了个颜色」——回收（gc）正是靠
        标记保护「客户确认过的版本」，标记丢了，那个版本就有可能被当成孤儿清掉。
        同时它和 gc 抢同一把项目锁，也就不会出现「gc 正在算可达、这边刚打完标记」
        的中间状态。
        """
        commit_oid = self.resolve(commit_oid)
        m = self._read_json("marks.json", {}) or {}
        for k in list(m):                      # 清理历史上前缀写法留下的键
            if k != commit_oid and len(k) != 64 and commit_oid.startswith(k):
                m.pop(k, None)
        e = m.setdefault(commit_oid, {})
        if mark is not None:
            if mark in MARKS:
                e["mark"] = mark
            else:
                e.pop("mark", None)
        if tags is not None:
            e["tags"] = [str(t).strip() for t in tags if str(t).strip()]
        if not e:
            m.pop(commit_oid, None)
        self._write_json("marks.json", m)
        return m.get(commit_oid, {})

    def marks(self) -> dict:
        return self._read_json("marks.json", {}) or {}

    # ------------------------------------------------------------------ 同步辅助
    def is_ancestor(self, older: str, newer: str) -> bool:
        if not older or not newer:
            return False
        if older == newer:
            return True
        seen, oid = set(), newer
        while oid and oid not in seen and self.has(oid):
            seen.add(oid)
            try:
                c = self.commit_info(oid)
            except RepoError:
                return False
            if older in (c.get("parents") or []):
                return True
            oid = (c.get("parents") or [None])[0]
        return False

    def all_objects(self):
        seen, stack = set(), [self.head_oid(b) for b in self.branches()]
        while stack:
            oid = stack.pop()
            if not oid or oid in seen or not self.has(oid):
                continue
            seen.add(oid)
            kind, payload = self.get(oid)
            if kind == "commit":
                c = json.loads(payload)
                stack.extend(c.get("parents") or [])
                stack.append(c["tree"])
            elif kind == "tree":
                for it in json.loads(payload):
                    if it["t"] == "d":
                        stack.append(it["h"])
                    else:
                        seen.add(it["h"])
        return sorted(seen)
