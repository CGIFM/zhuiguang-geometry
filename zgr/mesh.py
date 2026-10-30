"""3D 预览：用 SolidWorks 把零件/装配体导出为 3MF，再转成网页能直接渲染的网格。

设计要点
--------
- 只在装了 SolidWorks 的 Windows 机器上生成；结果按内容对象号缓存，
  同一个零件在哪台机器、哪个版本都复用同一份网格。
- 3MF 是 zip + XML，标准库就能解析，不引入第三方依赖。
- SolidWorks 很吃内存，同一时刻只跑一个导出；已有别的 SolidWorks 实例在跑时
  直接拒绝，绝不抢用户的实例、更不会把它关掉。
- 导出过程有任何异常都不会动工作区文件：历史版本会落到 .zgr/tmp 下的临时目录。
"""

from __future__ import annotations

import array
import contextlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

from .store import META_DIR, RepoError

MAGIC = b"ZGM1"
MAX_TRIS = 3_000_000          # 超过这个面数网页转不动，宁可明确报错
# 顶点数也要单独卡：面数是三角面，顶点可以比它多得多（几百 KB 的文件声明
# 5 亿个顶点，长度对不上就该拒绝，但那是在算过一次宏大的乘法之后）。
MAX_VERTS = 3_000_000
MAX_ZGM_BYTES = 128 << 20     # 真实图纸导出的网格最大也就几十 MB
MATERIALIZE_CAP = 600 << 20   # 历史装配体重建目录的上限
EXPORT_TIMEOUT = 300          # 秒；大装配体实测 100~190 秒

# 能让 SolidWorks 打开的模型格式。STEP/IGES/Parasolid 是中性格式，没有内部引用，
# 让 SW 导一遍比在网页里跑 occt 慢，但胜在**不需要有人开着网页**也能出图。
MESHABLE_EXTS = (".sldprt", ".sldasm",
                 ".step", ".stp", ".igs", ".iges", ".x_t", ".x_b")
_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_HEX = re.compile(r"^[0-9a-f]{64}$")

# 装配体里的引用件通常以 ASCII 或 UTF-16LE 字符串存在 OLE 流里。
# 这两种形式都扫一遍，只认 SolidWorks 的 .sldprt/.sldasm 文件名。
_REF_ASCII = re.compile(rb"[A-Za-z0-9_\- .]{1,160}\.(?:sldprt|sldasm)", re.I)
_REF_UTF16 = re.compile(
    rb"(?:[A-Za-z0-9_\- .]\x00){1,160}\.\x00"
    rb"(?:s\x00l\x00d\x00p\x00r\x00t\x00|s\x00l\x00d\x00a\x00s\x00m\x00)", re.I)
_REF_MAX_BYTES = 64 << 20
_REF_MAX_SCAN = 20000

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_EXPORT_LOCK = threading.Lock()          # 只在同一进程内互斥（网页服务自己并发请求）
_STATE_LOCK = threading.Lock()           # zgr-sw-state.json 的读改写
# 前台请求最多同时排几个导出任务。SolidWorks 本来就是一个一个导，
# 排再多也只是把线程挂着；超过这个数直接告诉调用方「稍后再看」。
MAX_FOREGROUND_FLIGHT = 6
_FLIGHT = {}

# 批量补图和网页请求是**不同进程**：进程内的锁管不到对方，谁都可能在对方
# SolidWorks 还开着的时候抢进来，然后把对方当成「用户正在编辑」直接拒掉。
# 所以再多加一把跨进程的锁文件，同一台机器同一时刻只让一个任务驱动 SolidWorks。
SW_LOCK_WAIT = 240            # 排队等别人用完 SolidWorks 的上限（秒）
SW_EXIT_GRACE = 12            # 拿到锁后，等残留的 SolidWorks 退出的宽限（秒）
SW_POLL = 0.15                # 网页请求抢锁的间隔
SW_POLL_BATCH = 0.5           # 批量补图抢锁的间隔
SW_BATCH_YIELD = 1.0          # 有人在等的时候，批量补图让出的一段时间

# 批量补图进程（zgr warm-sw / warm174.py）把它置 True。
# 补图是「不着急、可以补一整晚」的活，网页上那个请求才是有人在等的；
# SolidWorks 一次只能一个任务用，所以批量这边要主动让路，否则网页请求
# 永远抢不到锁（实测卡 3 分钟一次都没轮上）。
BATCH = False

_FLIGHT_LOCK = threading.Lock()

# 对端回源的网格上限。3D 网格再大也不该超过这个数；超了直接拒绝，
# 免得对端一个错误响应把本机内存吃掉。
PEER_MAX_BYTES = 256 << 20
PEER_TIMEOUT = EXPORT_TIMEOUT + 60      # 对端可能要现场跑 SolidWorks
_SW_CACHE = None


class MeshError(Exception):
    """3D 预览生成失败（消息直接给用户看）"""


class MeshBusy(MeshError):
    """这次没轮到，但**不代表模型有问题**。

    网页一口气要几十张缩略图时，SolidWorks 只能一个一个导；排在后面的请求
    等下去只会把线程挂十几分钟（用户根本等不到）。这种就直接回「稍后再看」，
    关键是**不能**写成「没有图」的失败标记，下次打开还要再试。
    """


class EmptyMesh(MeshError):
    """导出来了，但模型里没有可显示的实体（空装配、纯曲面、全部压缩）。

    这种情况**重试多少次都是同一个结果**，所以要单独标出来，别再拿它去反复
    起 SolidWorks（一个空装配耗 5 秒，440 个装配体里有一批是空的）。
    """


# --------------------------------------------------------------------- 缓存位置
def mesh_dir(repo) -> str:
    return os.path.join(repo.root, META_DIR, "meshes")


def cache_file(repo, oid: str) -> str:
    return os.path.join(mesh_dir(repo), oid + ".zgm")


def meta_file(repo, oid: str) -> str:
    return os.path.join(mesh_dir(repo), oid + ".json")


def read_meta(repo, oid: str):
    try:
        with open(meta_file(repo, oid), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_cached(repo, oid: str) -> bool:
    return _HEX.match(oid or "") is not None and os.path.exists(cache_file(repo, oid))


# --------------------------------------------------------------------- 本机能力
def solidworks_available() -> bool:
    """这台机器能不能自己跑 SolidWorks 导出。

    174 这类只同步、不装 SolidWorks 的机器必须能明确回答 False，
    否则界面会把「本机不能生成」误报成「模型有问题」。
    """
    global _SW_CACHE
    if os.name != "nt":
        return False
    if _SW_CACHE is not None:
        return _SW_CACHE
    ok = False
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\Classes\SldWorks.Application\CLSID",
                        r"SOFTWARE\SolidWorks\Setup"):
                try:
                    with winreg.OpenKey(root, key):
                        ok = True
                        break
                except OSError:
                    pass
            if ok:
                break
    except (ImportError, OSError):
        pass
    if not ok:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            if base and os.path.isfile(os.path.join(
                    base, "SOLIDWORKS Corp", "SOLIDWORKS", "SLDWORKS.exe")):
                ok = True
                break
    _SW_CACHE = ok
    return ok


def _valid_zgm(raw: bytes):
    """校验对端回来的网格，返回 (顶点数, 三角面数) 或 None。"""
    if not raw or len(raw) < 12 or raw[:4] != MAGIC:
        return None
    if len(raw) > MAX_ZGM_BYTES:
        return None
    nv, nt = struct.unpack("<II", raw[4:12])
    # 顶点/面数为 0 是**合法**的（空装配体导出来就是一个空网格），
    # 长度对得上就算有效；越界才是坏数据。
    if nv > MAX_VERTS or nt > MAX_TRIS:
        return None
    if len(raw) != 12 + nv * 12 + nt * 12:
        return None
    return nv, nt


def _peer_error(e) -> str:
    """把对端返回的错误 JSON 变成一句能直接给用户看的话。"""
    try:
        raw = e.read(64 << 10)
        j = json.loads(raw.decode("utf-8", "replace"))
        if j.get("error"):
            return str(j["error"])
    except Exception:  # noqa: BLE001 - 读不到就用 HTTP 状态兜底
        pass
    return "HTTP %s" % getattr(e, "code", "?")


def fetch_peer(repo, peer: str, token: str, oid: str, commit: str = None):
    """向对端要 oid 对应的 ZGM 网格，成功后落到本地缓存。

    commit 是可选的提交号：老版本里的零件已经不在 HEAD 时，对端靠它定位路径。
    """
    url = "%s/api/mesh/peer?oid=%s" % (peer.rstrip("/"), oid)
    if commit and _HEX.match(commit):
        url += "&commit=" + commit
    headers = {"X-ZGR-Token": token} if token else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=PEER_TIMEOUT) as r:
            data = r.read(PEER_MAX_BYTES + 1)
            tris_h = r.headers.get("X-ZGR-Tris")
            objects_h = r.headers.get("X-ZGR-Objects")
    except urllib.error.HTTPError as e:
        raise MeshError("对端没能提供这个模型：%s" % _peer_error(e))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MeshError("连不上对端 %s：%s" % (peer, e))
    if len(data) > PEER_MAX_BYTES:
        raise MeshError("对端返回的网格超过 %dMB，已拒绝" % (PEER_MAX_BYTES >> 20))
    head = _valid_zgm(data)
    if not head:
        raise MeshError("对端返回的网格数据损坏")
    nv, nt = head
    tmp = cache_file(repo, oid) + ".peer%d" % os.getpid()
    os.makedirs(mesh_dir(repo), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, cache_file(repo, oid))
    try:
        tris = int(tris_h) if tris_h else nt
        objects = int(objects_h or 0)
    except (TypeError, ValueError):
        tris, objects = nt, 0
    meta = {
        "oid": oid,
        "kind": "",
        "verts": nv,
        "tris": tris or nt,
        "bytes": len(data),
        "objects": objects,
        "source": "peer:" + peer,
        "seconds": 0,
        "exported": int(time.time() * 1000),
    }
    tmp_meta = meta_file(repo, oid) + ".peer%d" % os.getpid()
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp_meta, meta_file(repo, oid))
    return meta


def resolve_rel(repo, oid: str, commit: str = None) -> str:
    """对端只有 oid 时，反查它属于哪个模型文件（先在 HEAD，再翻最近历史）。"""
    def scan(cid):
        try:
            _c, files = repo.files_of(cid)
        except RepoError:
            return ""
        for p, info in files.items():
            if info.get("oid") == oid and p.lower().endswith(MESHABLE_EXTS):
                return p
        return ""

    if commit:
        hit = scan(commit)
        if hit:
            return hit
    try:
        hit = scan(repo.head_oid())
        if hit:
            return hit
    except RepoError:
        pass
    try:
        for c in repo.log(50):
            hit = scan(c["id"])
            if hit:
                return hit
    except RepoError:
        pass
    return ""


def _meta_from_cache(repo, oid: str) -> dict:
    """缓存文件没有配套 meta 时，从 ZGM 头补一份最小元信息。"""
    meta = {"oid": oid, "verts": 0, "tris": 0, "objects": 0,
            "bytes": 0, "source": "", "seconds": 0}
    p = cache_file(repo, oid)
    try:
        with open(p, "rb") as f:
            head = f.read(12)
        meta["bytes"] = os.path.getsize(p)
        if len(head) == 12 and head[:4] == MAGIC:
            nv, nt = struct.unpack("<II", head[4:12])
            meta["verts"], meta["tris"] = nv, nt
    except OSError:
        pass
    return meta


def peer_mesh(repo, oid: str, commit: str = None):
    """对端接口用：保证网格存在并返回路径。只在本机生成，不再向别人回源（避免环路）。"""
    if not _HEX.match(oid or ""):
        raise MeshError("对象号不合法")
    if is_cached(repo, oid):
        return cache_file(repo, oid), (read_meta(repo, oid) or _meta_from_cache(repo, oid))
    rel = resolve_rel(repo, oid, commit if commit and _HEX.match(commit) else None)
    if not rel:
        raise MeshError("对端项目里找不到这个对象对应的模型文件")
    meta = ensure_mesh(repo, rel, oid)
    p = cache_file(repo, oid)
    if not os.path.exists(p):
        raise MeshError("网格文件没生成出来")
    return p, meta


# --------------------------------------------------------------------- 对外入口
@contextlib.contextmanager
def _export_slot(budget: float = None):
    """进程内导出互斥。budget 给前台请求一个排队上限，排不上就抛 MeshBusy。"""
    if budget is None:
        _EXPORT_LOCK.acquire()
    elif not _EXPORT_LOCK.acquire(timeout=max(1.0, budget)):
        raise MeshBusy("SolidWorks 正忙着导别的模型，这个先跳过，稍后再看就有图了")
    try:
        yield
    finally:
        _EXPORT_LOCK.release()


def ensure_mesh(repo, rel: str, oid: str, commit: str = None, force: bool = False,
                peer: str = "", peer_token: str = "", budget: float = None):
    """确保 oid 对应网格已经生成，返回元信息 dict。失败抛 MeshError。

    rel        项目内相对路径（用来判断零件/装配体、找同目录引用）
    oid        文件内容的对象号（缓存键）
    commit     需要历史版本时传提交号；不传就用当前分支
    peer       对端地址；本机没有 SolidWorks（或生成失败）时按 oid 回源
    peer_token 对端令牌
    budget     前台请求能接受的排队秒数（None=后台任务，按老规矩慢慢排）。
               网页那边一口气要几十张缩略图时，一个个排下去能把线程挂十几分钟，
               浏览器早就不等了；给它一个上限，排不上就立刻回「稍后再试」，
               图片下次打开自然就有了（生成的网格会留在缓存里）。
    """
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if ".." in rel.split("/"):
        raise MeshError("路径不合法")
    if not _HEX.match(oid or ""):
        raise MeshError("对象号不合法")
    if not rel.lower().endswith(MESHABLE_EXTS):
        raise MeshError("只有 SolidWorks 零件/装配体和 STEP 类模型能做 3D 预览")
    if not force and is_cached(repo, oid):
        return read_meta(repo, oid)

    # 同一个对象号同时只生成一次，其它请求等它做完直接读缓存
    with _FLIGHT_LOCK:
        flight = _FLIGHT.get(oid)
        owner = flight is None
        if owner and budget is not None and len(_FLIGHT) >= MAX_FOREGROUND_FLIGHT:
            # 机器已经被一堆缩略图请求占满了：这一次先生成不了，但也别占着线程干等，
            # 更别把它记成「没有图」——下次打开还会再试一次。
            raise MeshBusy("SolidWorks 正忙着导别的模型，这个先跳过，稍后再看就有图了")
        if owner:
            flight = {"ev": threading.Event(), "err": None}
            _FLIGHT[oid] = flight
    if not owner:
        wait = EXPORT_TIMEOUT + 60 if budget is None else max(1.0, budget)
        if not flight["ev"].wait(wait):
            raise MeshBusy("这个模型还在导，先不等了，稍后再看就有图了")
        if is_cached(repo, oid):
            return read_meta(repo, oid)
        # 生成的人失败了就复述它的原因（“SolidWorks 正开着”比“正在生成”有用）
        raise flight["err"] or MeshError("这个模型正在生成，请稍后再试")

    try:
        with _export_slot(budget):
            if not force and is_cached(repo, oid):
                return read_meta(repo, oid)
            peer_err = None
            has_sw = solidworks_available()
            # 没装 SolidWorks 的机器（例如 174）没必要白等导出超时，直接回源
            if peer and not force and not has_sw:
                try:
                    return fetch_peer(repo, peer, peer_token, oid, commit)
                except MeshError as e:
                    peer_err = e
                    raise MeshError("本机没有 SolidWorks，从对端取网格也失败：%s" % e)
            try:
                return _generate(repo, rel, oid, commit, budget=budget)
            except MeshError as e:
                # 本机装了 SolidWorks 但这次生成失败（开着/缺引用件）时，
                # 对端可能早就缓存过同一份网格，值得再试一次。
                if peer and not force and peer_err is None:
                    try:
                        return fetch_peer(repo, peer, peer_token, oid, commit)
                    except MeshError:
                        raise e
                raise
    except MeshError as e:
        flight["err"] = e
        raise
    finally:
        with _FLIGHT_LOCK:
            _FLIGHT.pop(oid, None)
        flight["ev"].set()


def _generate(repo, rel, oid, commit, budget=None):
    t0 = time.time()
    tmpdir = os.path.join(repo.meta, "tmp", "mesh-%s-%d" % (oid[:12], os.getpid()))
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    try:
        src, source = _materialize(repo, rel, oid, commit, tmpdir)
        missing = []
        if rel.lower().endswith(".sldasm"):
            root = os.path.dirname(src) if source == "working" else tmpdir
            try:
                missing = missing_refs(src, root)
            except Exception:  # noqa: BLE001 - 预检失败绝不影响正常导出
                missing = []
        out3mf = os.path.join(tmpdir, "__export.3mf")
        _export_3mf(src, out3mf, "asm" if rel.lower().endswith(".sldasm") else "part",
                    budget=budget)
        verts, tris, stats = parse_3mf(out3mf)
        os.makedirs(mesh_dir(repo), exist_ok=True)
        tmp_out = cache_file(repo, oid) + ".tmp%d" % os.getpid()
        _write_zgm(tmp_out, verts, tris)
        os.replace(tmp_out, cache_file(repo, oid))
        meta = {
            "oid": oid,
            "file": rel,
            "kind": "asm" if rel.lower().endswith(".sldasm") else "part",
            "verts": stats["verts"],
            "tris": stats["tris"],
            "bytes": os.path.getsize(cache_file(repo, oid)),
            "objects": stats["objects"],
            "source": source,
            "seconds": round(time.time() - t0, 1),
            "exported": int(time.time() * 1000),
            "missing": missing,
        }
        tmp_meta = meta_file(repo, oid) + ".tmp%d" % os.getpid()
        with open(tmp_meta, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp_meta, meta_file(repo, oid))
        return meta
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------- 源文件
def _materialize(repo, rel, oid, commit, tmpdir):
    """返回 (可打开的磁盘路径, 来源说明)。工作区版本直接用原路径。"""
    ap = os.path.join(repo.root, *rel.split("/"))
    if os.path.isfile(ap):
        try:
            if repo._hash_file(ap)[0] == oid:
                return ap, "working"
        except (OSError, RepoError):
            pass

    kind = "asm" if rel.lower().endswith(".sldasm") else "part"
    dst = os.path.join(tmpdir, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if kind == "part":
        repo.blob_to_file(oid, dst)
        return dst, "commit"

    # 装配体：连同同目录的引用件一起落地，否则 SolidWorks 找不到零件
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    try:
        _c, files = repo.files_of(commit or repo.head_oid())
    except RepoError as e:
        raise MeshError("读不到这个版本的目录：%s" % e)
    total = 0
    put = 0
    for p, info in files.items():
        if parent:
            if not p.startswith(parent + "/"):
                continue
            relp = p[len(parent) + 1:]
        else:
            if "/" in p:
                continue
            relp = p
        if not relp or relp.split("/")[0] == META_DIR:
            continue
        total += int(info.get("size") or 0)
        if total > MATERIALIZE_CAP:
            raise MeshError("这个装配体所在的目录超过 %dMB，无法在临时目录重建"
                            % (MATERIALIZE_CAP >> 20))
        out = os.path.join(tmpdir, *relp.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            repo.blob_to_file(oid if p == rel else info["oid"], out)
        except RepoError:
            continue
        put += 1
    if not put:
        raise MeshError("这个版本里找不到装配体文件")
    return dst, "commit"


# --------------------------------------------------------------------- 缺引用件预检
def _refs_in_file(path):
    """从 .sldasm 二进制里扒出引用件文件名（不做 OLE 解析，只扫字符串）。"""
    names = set()
    try:
        with open(path, "rb") as f:
            raw = f.read(_REF_MAX_BYTES)
    except OSError:
        return names
    for m in _REF_ASCII.finditer(raw):
        names.add(m.group(0).decode("latin-1").strip().strip('"'))
    for m in _REF_UTF16.finditer(raw):
        names.add(m.group(0).decode("utf-16-le", "ignore").strip().strip('"'))
    return names


def _known_files(root):
    """目录下能找到的文件名（小写）。历史版本会先在临时目录铺开整个目录树。"""
    known = set()
    if not root or not os.path.isdir(root):
        return known
    for dirpath, _dirs, files in os.walk(root):
        for n in files:
            known.add(n.lower())
            if len(known) > _REF_MAX_SCAN:
                return known
    return known


def missing_refs(src, root):
    """返回装配体引用了、但同目录树里找不到的文件名列表。

    只是警告用：SolidWorks 可能通过库路径/Toolbox 找到它们，
    所以绝不能因为这里报缺件就拒绝导出——那会把正常模型也挡掉。
    """
    own = os.path.basename(src).lower()
    known = _known_files(root)
    miss = set()
    for ref in _refs_in_file(src):
        base = os.path.basename(ref.replace("\\", "/")).lower().strip()
        if not base or base == own:
            continue
        if base not in known:
            miss.add(base)
    return sorted(miss)[:20]


# --------------------------------------------------------------------- 导出
_VBS = r"""On Error Resume Next
Dim a, sw, m, ok, ids, one, idata, errs, tries, k
Set a = WScript.Arguments
tries = Array(CInt(a(1)), 1, 2, 4)
If a.Count > 3 Then
  ids = Split(a(3), ",")
Else
  ids = Array("SldWorks.Application")
End If
Set sw = Nothing
For Each one In ids
  ' 先问「是不是已经开着了」：CreateObject 遇到已运行的实例会**接上去**，
  ' 而本脚本结尾会 ExitApp —— 那就成了把用户正在编辑的 SolidWorks 关掉。
  ' 调用方在前面查过一次进程，但那之后用户完全可能自己打开，这里再挡一道。
  Dim pre
  pre = False
  On Error Resume Next
  Dim alive
  Set alive = GetObject(, one)
  If Err.Number = 0 And Not (alive Is Nothing) Then pre = True
  Err.Clear
  Set alive = Nothing
  On Error Resume Next
  If pre Then
    WScript.Echo "BUSY " & one
    WScript.Quit 5
  End If
  Set sw = CreateObject(one)
  If Err.Number = 0 Then
    If Not sw Is Nothing Then
      ' 告诉调用方是哪版接的活：下次先试它，崩过的那版先让路
      WScript.Echo "PROGID " & one
      Exit For
    End If
  End If
  Err.Clear
  Set sw = Nothing
Next
If sw Is Nothing Then
  WScript.Echo "ERR create " & Err.Number
  WScript.Quit 2
End If
sw.Visible = False
sw.UserControl = False
sw.CommandInProgress = True
' 零件/装配体/图纸都试一遍：STEP 里的东西到底按哪种类型打开，得问 SolidWorks
For Each k In tries
  Err.Clear
  Set m = Nothing
  Set m = sw.OpenDoc(a(0), k)
  If Err.Number <> 0 Then
    WScript.Echo "TRY kind=" & k & " err=" & Err.Number & " " & Err.Description
  ElseIf m Is Nothing Then
    WScript.Echo "TRY kind=" & k & " nothing"
  Else
    WScript.Echo "OPEN kind=" & k
    Exit For
  End If
Next
' STEP/IGES 这类中性格式在开了「3D Interconnect」的版本上 OpenDoc 会返回空，
' 再走 LoadFile4 + 导入参数这条路
If m Is Nothing Then
  Err.Clear
  Set m = sw.LoadFile2(a(0), "r")
  If Err.Number <> 0 Then
    WScript.Echo "TRY loadfile2 err=" & Err.Number
    Set m = Nothing
  ElseIf m Is Nothing Then
    WScript.Echo "TRY loadfile2 nothing"
  Else
    WScript.Echo "OPEN loadfile2"
  End If
End If
If m Is Nothing Then
  Err.Clear
  Set idata = sw.GetImportFileData(a(0))
  If Err.Number <> 0 Or idata Is Nothing Then
    WScript.Echo "TRY importdata err=" & Err.Number
  End If
  Set m = sw.LoadFile4(a(0), "r", idata, errs)
  If Err.Number <> 0 Then
    WScript.Echo "TRY loadfile err=" & Err.Number & " " & Err.Description
    Set m = Nothing
  ElseIf m Is Nothing Then
    WScript.Echo "TRY loadfile nothing"
  Else
    WScript.Echo "OPEN loadfile"
  End If
End If
If m Is Nothing Then
  WScript.Echo "ERR open " & Err.Number
  sw.ExitApp
  WScript.Quit 3
End If
Err.Clear
ok = m.SaveAs(a(2))
If Not ok Then
  WScript.Echo "ERR save " & Err.Number
  sw.ExitApp
  WScript.Quit 4
End If
sw.ExitApp
WScript.Echo "OK"
"""


_PROGID_CACHE = None


def sldworks_progids():
    """已安装的 SolidWorks 版本，从新到旧。

    默认 ProgID `SldWorks.Application` 指向的是**安装顺序里最早**那版，
    而零件往往是被更新的版本存过的 —— 拿旧版去开新版存的文件，SolidWorks
    只会回一句"打不开这个模型"，看起来像文件坏了，其实是选错了版本。
    """
    global _PROGID_CACHE
    if _PROGID_CACHE is not None:
        return _PROGID_CACHE
    found = []
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes") as k:
                i = 0
                while i < 5000:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    if name.startswith("SldWorks.Application."):
                        tail = name.rsplit(".", 1)[-1]
                        if tail.isdigit():
                            found.append((int(tail), name))
                found.sort(reverse=True)
        except (ImportError, OSError):
            found = []
    ids = [n for _v, n in found]
    ids.append("SldWorks.Application")
    _PROGID_CACHE = ids
    return ids


def _sldworks_pids():
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20,
            # tasklist 在中文 Windows 上输出 GBK；用 utf-8 解码会抛错，stdout 变 None，
            # 结果「有没有 SolidWorks 在跑」永远判成没有，就抢了用户的实例。
            encoding="mbcs", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "sldworks.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def _kill(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                           timeout=15, creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass


def _wait_exit(seconds=20, pids=None):
    """等 SolidWorks 退出；等不到就算了，别把请求卡太久。

    传了 pids 就只等这几个：用户自己开着的 SolidWorks 不会退出，
    以前无参等「所有 SLDWORKS」会白等满 20 秒，下一个请求还会被误判成「用户开着」。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        cur = _sldworks_pids()
        if not (pids & cur if pids else cur):
            return
        time.sleep(0.4)


def _proc_start_times(pids):
    """pid → 进程启动时刻（epoch 秒）。取不到就返回 {}。

    为什么需要它：导出一个坏零件要等到超时（最长几分钟），这期间车间师傅完全可能
    **自己双击打开了 SolidWorks** 开始改图。只看「导出前没有、导出后有」就当成自己的
    进程去 taskkill，会把人家没保存的改动一起杀掉。
    """
    if os.name != "nt" or not pids:
        return {}
    filt = " or ".join("ProcessId=%d" % int(p) for p in sorted(pids))
    script = ("$ErrorActionPreference='SilentlyContinue'; "
              "Get-CimInstance Win32_Process -Filter \"%s\" | ForEach-Object { "
              "\"$($_.ProcessId) $(([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds())\" }"
              % filt)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return {}
    times = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            times[int(parts[0])] = int(parts[1])
    return times


def _cleanup_own(before, started=None):
    """收掉**本次导出自己起的** SolidWorks：先礼后兵，退不掉再强杀一次。

    判据是「这个进程的启动时刻在本次导出开始之后」——不是「导出前没有、现在有」。
    取不到启动时刻（PowerShell/CIM 不可用）时**一个都不杀**：宁可留一个没人用的
    SolidWorks 让用户手动关，也不能杀错人正在编辑的图。
    返回 {"killed": n, "skipped": n}，skipped 是把「不确定是不是自己的」放过的个数。
    """
    ours = _sldworks_pids() - before
    if not ours:
        return {"killed": 0, "skipped": 0}
    times = _proc_start_times(ours)
    if started is not None and times:
        mine = {p for p in ours if times.get(p, 0) >= int(started) - 1}
        skipped = len(ours) - len(mine)
    elif started is None:
        mine, skipped = set(ours), 0          # 老调用点没给时刻：保持旧行为
    else:
        # 拿不到启动时刻：不敢杀，如实说有几个没确认的
        return {"killed": 0, "skipped": len(ours)}
    if not mine:
        return {"killed": 0, "skipped": skipped}
    _kill(mine)
    deadline = time.time() + 8
    while time.time() < deadline and (mine & _sldworks_pids()):
        time.sleep(0.4)
    left = mine & _sldworks_pids()
    if left:                       # 卡在缺引用件的对话框里时 taskkill 也要补一刀
        _kill(left)
    return {"killed": len(mine), "skipped": skipped}


SW_CRASH_LIMIT = 2            # 连续崩这么多次就先停手
SW_CRASH_COOLDOWN = 600       # 冷却 10 分钟，别把用户的机器当沙包
SW_CRASH_FORGET = 6 * 3600    # 6 小时没有新崩溃就把账清掉
SW_ORPHAN_MIN_AGE = 90        # 我们留下的、超过这么久还没退的，才认作残留


def _state_path() -> str:
    return os.path.join(tempfile.gettempdir(), "zgr-sw-state.json")


def _state_read() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _state_write(d: dict) -> None:
    try:
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError:
        pass


def export_order():
    """这次导出按什么顺序试 SolidWorks 版本。

    上次成功的那版排最前（大文件重开后多半还是它行），连续崩过两版的先跳过
    一个冷却期。全被跳光也不把功能锁死，冷却到期自然会回来。
    """
    ids = list(sldworks_progids())
    st = _state_read()
    bad = st.get("bad") if isinstance(st.get("bad"), dict) else {}
    now = time.time()
    keep = [i for i in ids
            if not (isinstance(bad.get(i), dict)
                    and int(bad[i].get("n") or 0) >= SW_CRASH_LIMIT
                    and float(bad[i].get("until") or 0) > now)]
    if not keep:
        keep = ids
    good = st.get("good")
    if good in keep:
        keep.remove(good)
        keep.insert(0, good)
    return keep


def _note_result(progid: str, ok: bool, crashed: bool) -> None:
    """记一笔：哪版成功了、哪版崩了、连续崩到第几次该停手。"""
    with _STATE_LOCK:
        st = _state_read()
        now = time.time()
        bad = st.get("bad") if isinstance(st.get("bad"), dict) else {}
        last = float(st.get("when") or 0)
        if last and now - last > SW_CRASH_FORGET:
            st.pop("fails", None)
            st.pop("hold_until", None)
            bad = {}
        if ok:
            st["fails"] = 0
            st.pop("hold_until", None)
            if progid:
                st["good"] = progid
                bad.pop(progid, None)
        elif crashed:
            # 只有「SolidWorks 自己没了」才算崩溃。模型打不开、引用件缺失这类
            # 失败是文件的问题，崩一百次也不该把功能停掉。
            fails = int(st.get("fails") or 0) + 1
            st["fails"] = fails
            if progid:
                b = bad.get(progid) if isinstance(bad.get(progid), dict) else {}
                n = int(b.get("n") or 0) + 1
                bad[progid] = {"n": n,
                               "until": now + SW_CRASH_COOLDOWN if n >= SW_CRASH_LIMIT else 0}
            if fails >= SW_CRASH_LIMIT:
                st["hold_until"] = now + SW_CRASH_COOLDOWN
        st["bad"] = bad
        st["when"] = int(now)
        _state_write(st)


def _crash_guard() -> None:
    """连着崩过就先别动 SolidWorks 了 —— 崩一次要重新加载整个 SW，用户那边
    只会看到「刚修好又崩了」，而且崩溃日志刷屏会把真正的原因埋掉。"""
    st = _state_read()
    until = float(st.get("hold_until") or 0)
    now = time.time()
    if until > now:
        raise MeshError(
            "这台机器的 SolidWorks 生成 3D 预览时连续崩了 %d 次，先停手 %d 分钟再试，"
            "免得把整台机器拖垮。图纸本身没动过；这多半是 SolidWorks 的插件、显卡驱动"
            "或者版本不匹配造成的，修好后自动恢复。"
            % (int(st.get("fails") or 0) or SW_CRASH_LIMIT,
               max(1, int(math.ceil((until - now) / 60.0)))))


def _proc_window_handles(pids):
    """pid → 主窗口句柄。有窗口 = 人在用，绝不碰。"""
    if os.name != "nt" or not pids:
        return {}
    script = ("$ErrorActionPreference='SilentlyContinue'; "
              "Get-Process -Id %s | ForEach-Object { \"$($_.Id) $($_.MainWindowHandle)\" }"
              % (",".join(str(int(p)) for p in sorted(pids))))
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            res[int(parts[0])] = int(parts[1])
    return res


def _remember_own(pids) -> None:
    """记下「这几个 SolidWorks 是我们起的」，下次启动好收拾。"""
    if not pids:
        return
    times = _proc_start_times(pids)
    if not times:
        return
    with _STATE_LOCK:
        st = _state_read()
        own = st.get("own") if isinstance(st.get("own"), dict) else {}
        for p, t in times.items():
            own[str(int(p))] = int(t)
        st["own"] = own
        _state_write(st)


def _forget_own(pids) -> None:
    if not pids:
        return
    with _STATE_LOCK:
        st = _state_read()
        own = st.get("own") if isinstance(st.get("own"), dict) else {}
        for p in pids:
            own.pop(str(int(p)), None)
        st["own"] = own
        _state_write(st)


def _reap_orphans() -> int:
    """收拾**我们上次**没退干净的 SolidWorks。

    判据三条同时成立才动手：pid 是我们记过的、启动时刻对得上（排除 pid 复用）、
    没有主窗口（用户开着的 SolidWorks 一定有窗口）。服务自己在导出途中被重启时，
    上一轮的 cscript/SolidWorks 会变成没有窗口的孤儿，卡住后面所有导出。
    """
    st = _state_read()
    own = st.get("own") if isinstance(st.get("own"), dict) else {}
    if not own:
        return 0
    alive = _sldworks_pids()
    cand = {}
    for k, v in own.items():
        try:
            p, t = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if p in alive:
            cand[p] = t
    if not cand:
        _forget_own([int(k) for k in own if str(k).isdigit()])
        return 0
    times = _proc_start_times(set(cand))
    if not times:
        return 0
    hwnds = _proc_window_handles(set(cand))
    if hwnds is None:
        return 0
    now = time.time()
    victims = {p for p, t in cand.items()
               if abs(times.get(p, 0) - t) <= 2
               and hwnds.get(p, 0) == 0
               and now - t >= SW_ORPHAN_MIN_AGE}
    if not victims:
        return 0
    _kill(victims)
    deadline = time.time() + 8
    while time.time() < deadline and (victims & _sldworks_pids()):
        time.sleep(0.4)
    left = victims & _sldworks_pids()
    if left:
        _kill(left)
    _forget_own(victims)
    return len(victims)


def _settle_own(pids) -> None:
    """导出结束后的记账：已经退了的销账，还赖着不走的留着，下次启动收拾。"""
    pids = {int(p) for p in (pids or set())}
    if not pids:
        return
    alive = _sldworks_pids() & pids
    _forget_own(pids - alive)
    if alive:
        _remember_own(alive)


def _progid_used(text: str, order) -> str:
    """VBS 会打一行 `PROGID xxx`，告诉我们是哪版 SolidWorks 接的活。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("PROGID "):
            return line.split(None, 1)[1].strip()
    return order[0] if order else ""


def _looks_crashed(text: str) -> bool:
    """判断这次失败是「模型本身打不开」还是「SolidWorks 自己没了」。

    VBS 走完任何一条分支都会打 OK 或者 ERR xxx；只打了 TRY/OPEN 就断了、
    或者一个字都没打，说明进程在中途消失——崩了，不是文件的问题。
    """
    t = (text or "").strip()
    if not t:
        return True
    if "BUSY" in t:          # 用户的 SolidWorks 开着，主动让路，不是崩溃
        return False
    return ("OK" not in t) and ("ERR " not in t)


def _export_hint(text):
    """把 VBS 的返回码翻译成用户能照着处理的话"""
    last = (text or "").strip().splitlines()[-1] if (text or "").strip() else ""
    for code, msg in (("ERR open", "SolidWorks 打不开这个模型：多半是缺了它引用的零件/装配体"
                                  "（引用件要和主文件放在同一个目录里），也可能是文件损坏"
                                  "，或者是用更高版本的 SolidWorks 存的"),
                      ("ERR create", "SolidWorks 没能启动，先确认它能正常打开（有时试几次就好）"),
                      ("BUSY ", "这台机器的 SolidWorks 刚被打开（多半是你自己开的），3D 预览先不生成；"
                                "关掉它再点一次就行——不会去动你正在编辑的文档"),
                      ("ERR save", "SolidWorks 打开了模型，但导出 3MF 失败（文件可能只读或被占用）")):
        if code in last:
            extra = last.split(code, 1)[1].strip()
            # 只剩错误码就没必要再贴一遍；有具体说明就带上
            if extra and not extra.isdigit():
                return msg + "（SolidWorks 说：" + extra + "）"
            return msg
    return "SolidWorks 导出失败" + ("：" + last if last else "（可能缺引用件或文件损坏）")


_WANTED_TTL = 20              # 「有人在等」标记的新鲜期（秒）


def _wanted_path() -> str:
    return os.path.join(tempfile.gettempdir(), "zgr-sw-wanted")


def _wanted_fresh() -> bool:
    try:
        return time.time() - os.path.getmtime(_wanted_path()) < _WANTED_TTL
    except OSError:
        return False


@contextlib.contextmanager
def _sw_gate(timeout: int = SW_LOCK_WAIT):
    """跨进程排他：同一台机器同一时刻只让一个任务驱动 SolidWorks。

    用 %TEMP% 下的一个小锁文件（msvcrt 字节区间锁）。批量补图那个计划任务是
    独立进程，网页服务根本看不见它，只能靠文件锁排队。锁只保证不撞车、不排队，
    所以再加一个「有人在等」的标记：批量补图看到它就让一拍，网页请求才排得上。
    """
    import msvcrt

    path = os.path.join(tempfile.gettempdir(), "zgr-sw-export.lock")
    mine = not BATCH
    if mine:
        try:
            with open(_wanted_path(), "w") as w:
                w.write(str(os.getpid()))
        except OSError:
            pass
    f = open(path, "a+b")
    deadline = time.time() + timeout
    try:
        while True:
            if BATCH and _wanted_fresh():
                time.sleep(SW_BATCH_YIELD)    # 让网页那边先上
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() >= deadline:
                    raise MeshError("另一项任务正在用 SolidWorks 导出，排队等了 %d 秒还没轮到，"
                                    "稍后再试" % timeout)
                if mine:
                    try:
                        os.utime(_wanted_path(), None)    # 还在等，继续占位
                    except OSError:
                        pass
                time.sleep(SW_POLL if mine else SW_POLL_BATCH)
        try:
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        f.close()
        if mine:
            try:
                os.remove(_wanted_path())
            except OSError:
                pass


def _export_3mf(src, out, kind, budget=None):
    if os.name != "nt":
        raise MeshError("这台机器不能生成 3D 预览（3D 预览需要装了 SolidWorks 的 Windows）")
    cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "cscript.exe")
    if not os.path.exists(cscript):
        raise MeshError("找不到 cscript.exe，无法驱动 SolidWorks")

    with _sw_gate(int(budget) if budget else SW_LOCK_WAIT):
        _crash_guard()
        # 上一轮导出中途被重启（换版本、服务重启、断电）会留下没有窗口的 SolidWorks，
        # 它既不退出也不干活，后面的导出全被判成「用户正开着」——先把它收掉。
        _reap_orphans()
        before = _sldworks_pids()
        if before:
            # 拿到锁还看得见 SolidWorks，多半是上一个任务刚导完在退出，
            # 给它几秒；真的有用户开着，才拒绝（绝不关它）。
            _wait_exit(SW_EXIT_GRACE)
            before = _sldworks_pids()
        if before:
            raise MeshError("这台机器的 SolidWorks 正开着，先关掉它再生成 3D 预览"
                            "（不会去动你正在编辑的文档）")
        started = time.time()          # 本次导出的起点：只认这之后起来的 SolidWorks
        vbs = os.path.join(os.path.dirname(out), "__export.vbs")
        with open(vbs, "w", encoding="ascii", errors="replace") as f:
            f.write(_VBS)
        order = export_order()
        cmd = [cscript, "//nologo", "//T:%d" % (EXPORT_TIMEOUT + 30), vbs,
               src, "2" if kind == "asm" else "1", out, ",".join(order)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, creationflags=CREATE_NO_WINDOW)
        # 开跑后头半分钟盯着：SolidWorks 一起来就记下 pid。记在这里的意义是
        # **服务自己被杀掉**时下一轮还知道哪个是我们起的（否则就是那堆孤儿）。
        seen = set()
        watch = time.time() + 30
        while proc.poll() is None and time.time() < watch:
            new = (_sldworks_pids() - before) - seen
            if new:
                _remember_own(new)
                seen |= new
            time.sleep(1.0)
        timed_out = False
        try:
            left = max(1.0, EXPORT_TIMEOUT + 60 - (time.time() - started))
            o, e = proc.communicate(timeout=left)
            out_text = ((o or "") + (e or "")).strip()
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                o, _e = proc.communicate(timeout=15)
                out_text = (o or "").strip()
            except (subprocess.SubprocessError, OSError):
                out_text = ""
        used = _progid_used(out_text, order)
        ours = (_sldworks_pids() - before) | seen
        if ours:
            times = _proc_start_times(ours)
            if times:
                ours = {p for p in ours if times.get(p, 0) >= int(started) - 1} or ours
        if timed_out:
            _cleanup_own(before, started)
            _settle_own(ours)
            _note_result(used, False, True)
            raise MeshError("生成超时：模型太大或引用件缺失，SolidWorks 没能在 %d 秒内导出"
                            % EXPORT_TIMEOUT)
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            crashed = _looks_crashed(out_text)
            _cleanup_own(before, started)
            _settle_own(ours)
            _note_result(used, False, crashed)
            detail = (out_text or "").strip().replace(chr(13), "")
            try:
                with open(os.path.join(os.path.dirname(out), "export.log"), "w",
                          encoding="utf-8", errors="replace") as lg:
                    lg.write(detail)
            except OSError:
                pass
            msg = _export_hint(out_text)
            if detail:
                tail = "；".join(x.strip() for x in detail.splitlines()[-5:])
                msg += "（SolidWorks 回报：%s）" % tail[:400]
            raise MeshError(msg)
        _note_result(used, True, False)
        _settle_own(ours)
        # 导出完了 SolidWorks 还要几秒才真正退出。这期间紧接着的下一个请求会把它
        # 当成“用户的 SolidWorks 正开着”拒掉（实测连点两下第二个必失败），
        # 所以等它退干净再把锁放开 —— 只等**我们这几个**，用户自己开着的那个不等。
        _wait_exit(pids=ours or None)


def put_zgm(repo, oid: str, data: bytes, source: str = "browser", rel: str = "") -> dict:
    """存一份外部（无头浏览器）解析出来的网格，格式与 SolidWorks 那条路一致。

    STEP/IGES 这类中性格式 SolidWorks 在本机打不开（实测 2024 全部返回 Nothing），
    但网页里的 occt-import-js 能解。让无头 Edge 解一次、按 oid 存成 ZGM，
    之后两台机器都用同一条 thumb3d 管线出图，样式也和 SolidWorks 零件一致。
    """
    if not _HEX.match(oid or ""):
        raise MeshError("对象号不合法")
    if not data or len(data) < 12 or data[:4] != MAGIC:
        raise MeshError("网格数据不是 ZGM 格式")
    if len(data) > MAX_ZGM_BYTES:
        raise MeshError("网格数据太大（%.0f MB，上限 %d MB）"
                        % (len(data) / 1048576.0, MAX_ZGM_BYTES >> 20))
    nverts, ntris = struct.unpack("<II", data[4:12])
    if not nverts or not ntris or ntris > MAX_TRIS:
        raise MeshError("网格面数不合法（%d 面）" % ntris)
    if nverts > MAX_VERTS:
        raise MeshError("网格顶点数不合法（%d 个）" % nverts)
    need = 12 + nverts * 12 + ntris * 12
    if len(data) != need:
        raise MeshError("网格数据长度不对（应为 %d，收到 %d）" % (need, len(data)))
    os.makedirs(mesh_dir(repo), exist_ok=True)
    dst = cache_file(repo, oid)
    tmp = dst + ".tmp%d" % os.getpid()
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)
    meta = {"oid": oid, "file": rel, "kind": "neutral", "verts": nverts, "tris": ntris,
            "bytes": len(data), "objects": 0, "source": source, "seconds": 0,
            "exported": int(time.time() * 1000), "missing": []}
    tmp_meta = meta_file(repo, oid) + ".tmp%d" % os.getpid()
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp_meta, meta_file(repo, oid))
    return meta


# --------------------------------------------------------------------- 3MF 解析
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _tf(s):
    """3MF 的 12 个数是 4x3 行向量矩阵；空表示单位矩阵"""
    if not s:
        return None
    v = [float(x) for x in s.split()]
    if len(v) != 12:
        return None
    return v


def _compose(a, b):
    """先做 a 再做 b"""
    if a is None:
        return b
    if b is None:
        return a
    a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11 = a
    b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11 = b
    return [
        a0 * b0 + a1 * b3 + a2 * b6, a0 * b1 + a1 * b4 + a2 * b7, a0 * b2 + a1 * b5 + a2 * b8,
        a3 * b0 + a4 * b3 + a5 * b6, a3 * b1 + a4 * b4 + a5 * b7, a3 * b2 + a4 * b5 + a5 * b8,
        a6 * b0 + a7 * b3 + a8 * b6, a6 * b1 + a7 * b4 + a8 * b7, a6 * b2 + a7 * b5 + a8 * b8,
        a9 * b0 + a10 * b3 + a11 * b6 + b9,
        a9 * b1 + a10 * b4 + a11 * b7 + b10,
        a9 * b2 + a10 * b5 + a11 * b8 + b11,
    ]


def parse_3mf(path):
    """返回 (verts:array('f'), tris:array('I'), stats)"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            model = "3D/3dmodel.model"
            if model not in names:
                cands = [n for n in names if n.lower().endswith(".model")]
                if not cands:
                    raise MeshError("3MF 里没有模型数据")
                model = cands[0]
            with z.open(model) as fp:
                return _parse_model(fp)
    except zipfile.BadZipFile:
        raise MeshError("SolidWorks 导出的 3MF 损坏")


def _parse_model(fp):
    objects = {}
    build = []
    cur = None
    verts = None
    tris = None
    scale = 1.0
    unit = "millimeter"
    seen = 0
    for ev, el in ET.iterparse(fp, events=("start", "end")):
        tag = _local(el.tag)
        if ev == "start":
            if tag == "model":
                unit = (el.get("unit") or "millimeter").lower()
            elif tag == "object":
                cur = {"id": el.get("id"), "mesh": None, "comps": []}
            elif tag == "vertices":
                verts = array.array("f")
            elif tag == "triangles":
                tris = array.array("I")
            continue
        if tag == "vertex" and verts is not None:
            try:
                verts.extend((float(el.get("x") or 0), float(el.get("y") or 0),
                              float(el.get("z") or 0)))
            except (TypeError, ValueError):
                verts.extend((0.0, 0.0, 0.0))
        elif tag == "triangle" and tris is not None:
            try:
                tris.extend((int(el.get("v1") or 0), int(el.get("v2") or 0),
                             int(el.get("v3") or 0)))
            except (TypeError, ValueError):
                tris.extend((0, 0, 0))
            seen += 1
            if seen > MAX_TRIS:
                raise MeshError("模型面数超过 %d 万，网页预览带不动" % (MAX_TRIS // 10000))
        elif tag == "mesh" and cur is not None:
            cur["mesh"] = (verts, tris)
            verts, tris = None, None
        elif tag == "component" and cur is not None:
            cur["comps"].append((el.get("objectid"), _tf(el.get("transform"))))
        elif tag == "object" and cur is not None:
            objects[cur["id"]] = cur
            cur = None
            el.clear()
        elif tag == "item":
            build.append((el.get("objectid"), _tf(el.get("transform"))))
    scale = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0,
             "inch": 25.4, "foot": 304.8, "meter": 1000.0}.get(unit, 1.0)

    out_v = array.array("f")
    out_t = array.array("I")
    stats = {"objects": 0}

    def emit(obj_id, tf, depth=0):
        if depth > 32:
            return
        o = objects.get(obj_id)
        if not o:
            return
        if o["mesh"]:
            v, t = o["mesh"]
            if not v:
                return
            base = len(out_v) // 3
            if tf is None:
                if scale != 1.0:
                    out_v.extend(array.array("f", [x * scale for x in v]))
                else:
                    out_v.extend(v)
            else:
                m0, m1, m2, m3, m4, m5, m6, m7, m8, m9, m10, m11 = tf
                for i in range(0, len(v), 3):
                    x, y, z = v[i], v[i + 1], v[i + 2]
                    out_v.extend((
                        (x * m0 + y * m3 + z * m6 + m9) * scale,
                        (x * m1 + y * m4 + z * m7 + m10) * scale,
                        (x * m2 + y * m5 + z * m8 + m11) * scale,
                    ))
            out_t.extend([x + base for x in t])
            stats["objects"] += 1
            # 装配体靠组件实例展开：源网格可能只有几十万面，展开后能到几千万面。
            # 这里按展开后的总量卡住，别等数组吃满内存再报错（实测有个 24MB 的
            # 装配体展开出 5470 万面、单次解析 1GB 内存）。
            if len(out_t) // 3 > MAX_TRIS:
                raise MeshError("展开后模型面数超过 %d 万（零件被大量引用），网页预览带不动"
                                % (MAX_TRIS // 10000))
        for cid, ctf in o["comps"]:
            emit(cid, _compose(ctf, tf), depth + 1)

    if build:
        for oid, tf in build:
            emit(oid, tf)
    else:
        for oid in objects:
            emit(oid, None)

    if len(out_t) < 3:
        raise EmptyMesh("模型里没有可显示的实体（可能是曲面/空装配）")
    return out_v, out_t, {"verts": len(out_v) // 3, "tris": len(out_t) // 3,
                          "objects": stats["objects"]}


# --------------------------------------------------------------------- 落盘
def _write_zgm(path, verts, tris):
    if sys.byteorder != "little":
        verts = array.array("f", verts)
        tris = array.array("I", tris)
        verts.byteswap()
        tris.byteswap()
    with open(path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<II", len(verts) // 3, len(tris) // 3))
        f.write(verts.tobytes())
        f.write(tris.tobytes())
