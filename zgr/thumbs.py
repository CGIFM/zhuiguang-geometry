"""追光几何 · 零件缩略图服务

思路：缩略图按**内容指纹（blob oid）**缓存，所以
  - 同一个零件不管出现在哪个版本、哪台机器，图都是同一张；
  - 装了 SolidWorks 的机器（167）出图，没装的机器（174）按 oid 向对端要一次，之后就本地命中。

取图顺序：缓存 → 对象内嵌预览流（Office/老格式）→ Windows 外壳缩略图 → 对端回源。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import struct
import threading
import weakref
import time
import urllib.error
import urllib.request

from . import preview, thumb3d
from .mesh import MeshBusy
from .store import Repo

SIZES = (128, 256, 512)
# 这些格式文件里可能根本没存预览图，外壳处理器也给空白；只能靠 SolidWorks
# 把模型导成网格，再由 thumb3d 渲成图（见 _render_sw）。
SW_EXTS = (".sldprt", ".sldasm")
# 中性几何格式：网页里能渲（快的路），但没人开着网页时就永远没图；
# 装上 SolidWorks 的机器可以顺手导一遍，保证「不看也先有」。
NEUTRAL_EXTS = (".step", ".stp", ".igs", ".iges", ".x_t", ".x_b")
MAX_MATERIALIZE = 256 << 20      # 超过这个大小的模型不做缩略图（拷一份太亏）
MISS_TTL = 6 * 3600              # 失败结果缓存 6 小时（装了 SolidWorks 后能自愈）


MAX_RENDERERS = 3                 # 同时最多 3 个外壳缩略图任务，别把 SolidWorks 拖死
# 网页要图时最多陪它等这么久（秒）。SolidWorks 导一个零件约 20 秒，几十个零件
# 排下去要十几分钟，浏览器早就不等了；超时就让这一次空手回去，图下次打开再补。
FOREGROUND_WAIT = 25
MAX_MEM_CACHE = 300               # 工作区实时预览的内存缓存条数

# 「这个模型没有实体」的标记以前是永久的：一次误判（SolidWorks 还在装载中就
# 导出、或者刚好赶上外壳超时）会让这个零件**这一辈子**都没有图，而且换了电脑、
# 重装了 SolidWorks 也不会自愈。现在给个 TTL：过期就重试一次，重试很便宜
# （有网格就是纯 Python 渲一下），换来的却是「图能自己长出来」。
EMPTY_TTL = 30 * 24 * 3600


def _png_dims(png: bytes):
    """读 PNG 的宽高；不是我们自己的 8 位 RGB 图就返回 None"""
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 33:
        return None
    try:
        w, h = struct.unpack(">II", png[16:24])
    except struct.error:
        return None
    return int(w), int(h)


class Thumbs:
    def __init__(self):
        # 弱引用：谁在用谁持有强引用，用完自动回收。
        # 以前是个普通 dict，几万个对象之后光锁对象就常驻几 MB，永远不会释放。
        self._locks = weakref.WeakValueDictionary()   # key -> Lock，没人用就自动回收
        self._guard = threading.Lock()
        self._render_gate = threading.Semaphore(MAX_RENDERERS)
        self._mem: dict[tuple, bytes] = {}

    # ---------------------------------------------------------------- 路径
    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._locks[key] = lk       # 调用方返回后仍持有强引用，锁不会被提前回收
            return lk

    @staticmethod
    def clamp(size) -> int:
        try:
            n = int(size or 512)
        except (TypeError, ValueError):
            n = 512
        return min(SIZES, key=lambda s: abs(s - n))

    @staticmethod
    def _dir(repo: Repo) -> str:
        d = os.path.join(repo.meta, "previews")
        os.makedirs(d, exist_ok=True)
        return d

    def cache_path(self, repo: Repo, oid: str, size: int) -> str:
        return os.path.join(self._dir(repo), oid[:2], "%s-%d.png" % (oid, size))

    def _miss_path(self, repo: Repo, oid: str, size: int) -> str:
        return os.path.join(self._dir(repo), oid[:2], "%s-%d.miss" % (oid, size))

    def _empty_path(self, repo: Repo, oid: str) -> str:
        """模型本身没有可显示的实体：永久标记，别再反复起 SolidWorks"""
        return os.path.join(self._dir(repo), oid[:2], "%s.empty" % oid)

    def empty(self, repo: Repo, oid: str) -> bool:
        p = self._empty_path(repo, oid)
        try:
            st = os.stat(p)
        except OSError:
            return False
        if time.time() - st.st_mtime > EMPTY_TTL:
            return False        # 过期了：当作没记过，让下一次请求重新试一遍
        return True

    def _write_empty_mark(self, repo: Repo, oid: str,
                          reason: str = "空装配或纯曲面（没有可以画的面）"):
        """记下「这个对象确实没东西可画」，带时间和原因（有 TTL，会自己过期）"""
        p = self._empty_path(repo, oid)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"reason": reason, "time": int(time.time() * 1000),
                           "ttl_days": EMPTY_TTL // 86400}, f, ensure_ascii=False)
        except OSError:
            pass

    def empty_reason(self, repo: Repo, oid: str) -> str:
        """标记里记着什么原因（给界面用；读不出来就返回空串）"""
        if not self.empty(repo, oid):
            return ""
        try:
            with open(self._empty_path(repo, oid), "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("reason") or ""
        except (OSError, ValueError):
            return ""

    def _read_cache(self, repo, oid, size):
        p = self.cache_path(repo, oid, size)
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _write_cache(self, path: str, data: bytes):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    # ---------------------------------------------------------------- 回存
    def cached(self, repo: Repo, oid: str, size) -> bool:
        """这个对象在本地是否已经有图（决定要不要让浏览器端去生成）"""
        return os.path.isfile(self.cache_path(repo, oid, self.clamp(size)))

    def cached_set(self, repo: Repo, size):
        """一次列目录拿到这一档尺寸的「已经有图」集合；列不出来就返回 None。

        清单页要问三千个对象「有没有图」，一个对象一次 stat 就是三千次系统调用。
        预览图按 oid 前两位分桶放在 256 个子目录里，把子目录列一遍总共才两百多次；
        代价是这个集合会漏掉「刚被别人写进来」的图——所以调用方只把它当
        「确定有」的正向集合用，没命中的再单独 stat 一次（见 server.preview_missing）。
        """
        root = self._dir(repo)
        suf = "-%d.png" % self.clamp(size)
        out = set()
        try:
            for d in os.listdir(root):
                sub = os.path.join(root, d)
                if not os.path.isdir(sub):
                    continue
                for fn in os.listdir(sub):
                    if fn.endswith(suf):
                        out.add(fn[:-len(suf)])
        except OSError:
            return None
        return out

    def put(self, repo: Repo, oid: str, data: bytes, size=512) -> bool:
        """浏览器端渲染好的 PNG 回存。

        STEP/STL 这些格式服务端解不了，但浏览器里已经有 occt/three 的引擎；
        让它出图后存进同一个按 oid 索引的缓存，两台机器就都秒出了。
        """
        if not oid or len(oid) != 64 or set(oid) - set('0123456789abcdef'):
            return False
        if not data or len(data) > preview.MAX_PNG_BYTES:
            return False
        # 只查魔数不够：宽高是文件自己写的，后面会按它分配内存
        if preview.png_gate(data) is None:
            return False
        if not repo.has(oid):
            return False
        size = self.clamp(size)
        self._write_cache(self.cache_path(repo, oid, size), data)
        for stale in (self._miss_path(repo, oid, size), self._empty_path(repo, oid)):
            try:
                os.remove(stale)
            except OSError:
                pass
        return True

    def forget(self, repo: Repo, oid: str):
        """有了新网格之后，把之前的失败/空实体标记清掉，让缩略图能重新生成"""
        for size in SIZES:
            try:
                os.remove(self._miss_path(repo, oid, size))
            except OSError:
                pass
        try:
            os.remove(self._empty_path(repo, oid))
        except OSError:
            pass

    # ---------------------------------------------------------------- 取图
    def get(self, repo: Repo, oid: str, name: str = "", size=512, peer: str = "",
            peer_token: str = "", force: bool = False, budget: float = None):
        """返回 PNG bytes 或 None"""
        if not oid or len(oid) != 64 or set(oid) - set('0123456789abcdef') or not repo.has(oid):
            return None
        size = self.clamp(size)
        hit = self._read_cache(repo, oid, size)
        if hit and not force:
            return hit
        miss = self._miss_path(repo, oid, size)
        if not force and os.path.isfile(miss):
            try:
                if time.time() - os.path.getmtime(miss) < MISS_TTL:
                    return None
            except OSError:
                pass
        if not force and self.empty(repo, oid):
            return None
        # 锁按**对象**加，不按尺寸：同一张图的 128/256/512 连着要的时候，
        # 以前三个线程各渲一遍（还共用同一个临时文件互相踩）。串行之后，
        # 后两个会用「更大的那张降采样」，既快又不会打架。
        with self._lock("%s/%s" % (repo.meta, oid)):
            hit = self._read_cache(repo, oid, size)
            if hit and not force:
                return hit
            # 这个尺寸没图、但更大的尺寸有：降采样一份就行，别再让它重渲一遍
            blob = None if force else self._reuse_bigger(repo, oid, size)
            if blob is None:
                try:
                    blob = self._render(repo, oid, name, size, peer, peer_token,
                                        budget=budget)
                except MeshBusy:
                    # 这次没轮到（SolidWorks 正忙）：绝不写失败标记，
                    # 否则这张图会被拉黑 6 小时，下次打开还是没有
                    return None
            if blob is None and peer:
                # 前台请求向对端要不超预算，否则对端一慢就把这张图的线程挂满一分钟
                blob = self._fetch_peer(peer, peer_token, oid, size,
                                        timeout=60 if budget is None
                                        else max(5.0, budget * 0.6))
            if blob:
                self._write_cache(self.cache_path(repo, oid, size), blob)
                try:
                    os.remove(miss)
                except OSError:
                    pass
                return blob
            try:
                open(miss, "wb").close()
            except OSError:
                pass
            return None

    def _render(self, repo: Repo, oid: str, name: str, size: int,
                peer: str = "", peer_token: str = "", budget: float = None):
        ext = os.path.splitext(name or "")[1].lower()
        # 已经有网格就直接渲：可能是 SolidWorks 导的，也可能是无头浏览器用 occt 解的。
        # 这条抢在 client_only 前面，STL/OBJ 这类原本只靠网页的格式也能在服务端出图了。
        got = self._render_zgm(repo, oid, size, budget)
        if got:
            return got
        # STEP/IGES：本机能开 SolidWorks 就直接导，省得非等网页跑一遍
        if ext in NEUTRAL_EXTS:
            return self._render_sw(repo, oid, size, peer, peer_token, budget)
        # STL/OBJ 这类服务端没有解析器，交给浏览器端渲染后再回存（见 put()）
        if preview.is_client_only(name):
            return None
        try:
            total = os.path.getsize(repo.object_path(oid))
        except OSError:
            return None
        if total > MAX_MATERIALIZE:
            return None
        tmpdir = os.path.join(repo.meta, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        # 临时名必须带尺寸和线程号：以前只有 oid 前 16 位，128 和 512 两个请求
        # 同时来时写的是同一个文件，一边的 finally 还会把另一边的输入删掉，
        # 结果这张图被写成「6 小时内没有图」。
        tmp = os.path.join(tmpdir, "prev-%s-%d-%d%s" % (
            oid[:16], size, threading.get_ident(), ext or ".bin"))
        try:
            repo.blob_to_file(oid, tmp)
            with self._render_slot(budget):
                res = preview.render(tmp, size)
            # 只给 oid、没给文件名的调用方（部分接口/旧前端）拿不到扩展名，
            # 但 SolidWorks 零件是最需要走网格补图的那一类，别因为没名字就放弃
            if res is None and ext in SW_EXTS + ("",):
                got = self._render_sw(repo, oid, size, peer, peer_token, budget)
                if got:
                    return got
            return res[0] if res else None
        except MeshBusy:
            raise                       # 「这次没轮到」要原样往上抛，别当成没图
        except Exception:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _render_sw(self, repo: Repo, oid: str, size: int, peer: str, peer_token: str,
                   budget: float = None):
        """SolidWorks 零件/装配体没有内嵌预览时：导出网格再自己渲一张。

        本机没装 SolidWorks（或装的那台正被占着）就找对端要网格，仍然只算一次。
        """
        from . import mesh as mesh_mod
        # 网格已经有了（可能来自对端，或者无头浏览器解出来的）就先用它，
        # 没装 SolidWorks 的机器也能出图
        if not mesh_mod.is_cached(repo, oid) and not peer                 and not mesh_mod.solidworks_available():
            return None
        try:
            rel = mesh_mod.resolve_rel(repo, oid)
        except Exception:
            rel = None
        if not rel or not rel.lower().endswith(SW_EXTS + NEUTRAL_EXTS):
            return None
        try:
            mesh_mod.ensure_mesh(repo, rel, oid, peer=peer, peer_token=peer_token,
                                 budget=budget)
            return self._render_zgm(repo, oid, size, budget)
        except MeshBusy:
            raise                       # 没轮到 ≠ 没图，交给 get() 决定不写失败标记
        except mesh_mod.EmptyMesh:
            # 空装配/纯曲面：图本来就不存在，记下来免得每次点开都白等 5 秒
            # 写原因而不是空文件：半年后看到这个标记的人（包括我们自己）
            # 能一眼知道当初是「真没实体」还是「当时没轮到」，而不是瞎猜
            self._write_empty_mark(repo, oid)
            return None
        except Exception:
            return None

    def _reuse_bigger(self, repo: Repo, oid: str, size: int):
        """请求的尺寸没缓存、但更大的尺寸有：降采样一份，省掉一次纯 Python 光栅化"""
        for s in SIZES:
            if s <= size:
                continue
            png = self._read_cache(repo, oid, s)
            if not png:
                continue
            small = thumb3d.shrink(png, size)
            if _png_dims(small) != (size, size):
                continue
            try:
                self._write_cache(self.cache_path(repo, oid, size), small)
            except OSError:
                pass
            return small
        return None

    def _render_zgm(self, repo: Repo, oid: str, size: int, budget: float = None):
        """用已经存下来的 ZGM 网格渲一张图"""
        from . import mesh as mesh_mod
        try:
            if not mesh_mod.is_cached(repo, oid):
                return None
            with open(mesh_mod.cache_file(repo, oid), "rb") as f:
                zgm = f.read(256 << 20)
            with self._render_slot(budget):   # 纯 Python 光栅化很吃 CPU，别同时开一堆
                png = thumb3d.render_zgm(zgm, size)
            self._store_derived(repo, oid, png, size)
            return png
        except MeshBusy:
            raise                            # 没轮到 ≠ 没图
        except Exception:
            return None

    def _store_derived(self, repo: Repo, oid: str, png: bytes, size: int):
        """渲好一张大图，顺手把小尺寸也存下来（降采样几乎不花时间）。

        以前悬停要 512、列表要 128，两边各渲一次；现在渲一次大的就都有了。
        """
        if size <= min(SIZES) or _png_dims(png) != (size, size):
            return
        for s in SIZES:
            if s >= size or os.path.isfile(self.cache_path(repo, oid, s)):
                continue
            small = thumb3d.shrink(png, s)
            if _png_dims(small) != (s, s):
                continue
            try:
                self._write_cache(self.cache_path(repo, oid, s), small)
            except OSError:
                pass

    @contextlib.contextmanager
    def _render_slot(self, budget: float = None):
        """光栅化闸门。前台请求排不上就抛 MeshBusy，别把线程挂在那儿等。"""
        if budget is None:
            self._render_gate.acquire()
        elif not self._render_gate.acquire(timeout=max(1.0, budget)):
            raise MeshBusy("渲染任务排满了，这个稍后再看")
        try:
            yield
        finally:
            self._render_gate.release()

    @staticmethod
    def _fetch_peer(peer: str, token: str, oid: str, size: int, timeout: float = 60):
        url = "%s/api/preview?oid=%s&size=%d&nopeer=1" % (peer.rstrip("/"), oid, size)
        headers = {"X-ZGR-Token": token} if token else {}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return None
                data = r.read((16 << 20) + 1)    # 缩略图不该超过 16MB，防止对端返回超大响应
            if len(data) > (16 << 20):
                return None
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return None

    # ---------------------------------------------------------------- 工作区实时文件
    def _file_key(self, path: str):
        """工作区文件没有内容指纹，用 (规范路径, mtime, 大小) 当键；文件一改自然失效"""
        st = os.stat(path)
        digest = hashlib.sha1(
            os.path.normcase(os.path.abspath(path)).encode("utf-8", "ignore")).hexdigest()[:20]
        return digest, st.st_mtime_ns, st.st_size

    def _file_cache_path(self, repo: Repo, path: str, size: int) -> str:
        digest, mtime_ns, fsize = self._file_key(path)
        d = os.path.join(self._dir(repo), "work")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "%s-%d-%d-%d.png" % (digest, mtime_ns, fsize, size))

    def from_file(self, repo: Repo, path: str, size=512):
        size = self.clamp(size)
        if not os.path.isfile(path):
            return None
        try:
            digest, mtime_ns, fsize = self._file_key(path)
        except OSError:
            return None
        if fsize > MAX_MATERIALIZE:
            return None
        key = (digest, mtime_ns, fsize, size)
        with self._guard:
            hit = self._mem.get(key)
        if hit:
            return hit
        # 浏览器端渲好回存的图（STEP/STL 这些服务端解不了的格式）
        disk = self._file_cache_path(repo, path, size)
        try:
            with open(disk, "rb") as f:
                blob = f.read()
            if blob:
                with self._guard:
                    if len(self._mem) < MAX_MEM_CACHE:
                        self._mem[key] = blob
                return blob
        except OSError:
            pass
        if preview.is_client_only(path):
            return None
        with self._render_gate:
            res = preview.render(path, size)
        if not res:
            return None
        try:
            self._write_cache(disk, res[0])
        except OSError:
            pass
        with self._guard:
            if len(self._mem) >= MAX_MEM_CACHE:
                self._mem.clear()
            self._mem[key] = res[0]
        return res[0]

    def put_file(self, repo: Repo, path: str, data: bytes, size=512, expect_mtime=None) -> bool:
        """浏览器端为「工作区文件」渲好的图回存；带 mtime 校验，文件改过就不收"""
        if not data or len(data) > preview.MAX_PNG_BYTES:
            return False
        # 只查魔数不够：宽高是文件自己写的，后面会按它分配内存
        if preview.png_gate(data) is None:
            return False
            return False
        if not os.path.isfile(path):
            return False
        try:
            digest, mtime_ns, fsize = self._file_key(path)
        except OSError:
            return False
        # 前端拿到的 mtime 是 JS number，超过 2^53 会丢精度（st_mtime_ns 有 19 位），
        # 所以按 1 毫秒容差比，既能挡掉「文件已经被改过」，又不会因为浮点误差误杀
        if expect_mtime is not None:
            try:
                if abs(int(expect_mtime) - int(mtime_ns)) > 1000000:
                    return False
            except (TypeError, ValueError):
                pass
        size = self.clamp(size)
        try:
            self._write_cache(self._file_cache_path(repo, path, size), data)
        except OSError:
            return False
        with self._guard:
            self._mem[(digest, mtime_ns, fsize, size)] = data
        return True


_THUMBS = Thumbs()


def get_service() -> Thumbs:
    return _THUMBS
