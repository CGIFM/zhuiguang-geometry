"""追光几何 · 本地 HTTP 服务 + 内网同步端点 + 网页界面"""
from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque

from . import APP_NAME, __version__
from . import autopeer
from . import cache as cache_mod
from . import dxfconvert
from . import live as live_mod
from . import merge as merge_mod
from . import bom as bom_mod
from . import mesh as mesh_mod
from . import nativepick
from . import peerjoin
from . import preview
from . import remotes as remotes_mod
from . import swstep
from . import watchfs
from .registry import Registry, data_dir
from .store import (DEFAULT_IGNORE, META_DIR, Repo, RepoError, fs_path,
                     safe_branch)
from .sync import (SyncError, contains, local_urls, make_backend, reachable,
                   sync as do_sync, verify_complete)
from .thumbs import FOREGROUND_WAIT as THUMB_FOREGROUND_WAIT
from .thumbs import get_service as thumbs_service

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_BODY = 512 << 20          # 原始对象上传走流式，按字节算
# JSON 接口整包读进内存再解析：没人会一口气发 32 MB 的 JSON 请求，
# 但局域网里有人乱来就能把服务撑爆。单独卡一道（同步大对象走 /api/sync/object，不受影响）
MAX_JSON_BODY = 32 << 20
MAX_OBJECT_BODY = 8 << 30     # 单个同步对象 8 GB（真实图纸最大也只有几百 MB）
# 早退（404/401/415…）时最多陪客户端把请求体吃完多大；再大就别占着线程了
DRAIN_CAP = 8 << 20
MAX_WORKFILE = 256 << 20      # 工作区文件预览最多读这么多进内存
# 全历史搜索的索引最多存这么多行。索引是常驻内存的（每个项目一份，最多留 8 个），
# 50000 个版本按一天记一版也要 137 年——真到了那个量级，先翻最近的就够了。
MAX_INDEX_ROWS = 50000
_HEXOID = re.compile(r"\A[0-9a-f]{64}\Z")
# 本机绝对路径（盘符或 UNC 开头）。给**别的机器**回错误信息时要把这些抹掉：
# 「[WinError 32] 另一个程序正在使用此文件: 'D:\客户项目\某型号\底盘.sldprt'」
# 一句就把服务机的目录结构、客户名、盘符全说出去了。
_ABS_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s'\"<>|,]*")


def scrub_paths(msg) -> str:
    return _ABS_PATH.sub("<本机路径>", str(msg))

# ---- Host 校验（挡 DNS rebinding）
# 攻击页面把恶意域名解析到 127.0.0.1，浏览器就把它当同源，能读走
# /api/projects 这类本机接口（里面带项目路径）。浏览器必带 Host 头，
# 而那种情况下 Host 是恶意域名 —— 只认本机名/回环/本机网卡地址即可挡住。
_ALLOWED_HOSTS: tuple = (0.0, set())
_ALLOWED_HOSTS_LOCK = threading.Lock()
_ALLOW_ANY_HOST = (os.environ.get("ZGR_ALLOW_ANY_HOST") or "").strip().lower() in (
    "1", "true", "yes", "on")


def _host_names() -> set:
    names = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    try:
        h = socket.gethostname().lower()
        names.add(h)
        names.add(socket.getfqdn(h).lower())
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            names.add(ip.lower())
    except OSError:
        pass
    # 有些机器的主机名解析不全（多网卡 / 改了 DNS），再用出口地址兜一下
    for probe in (("8.8.8.8", 53), ("10.255.255.255", 1), ("192.168.255.255", 1)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect(probe)
            names.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    for x in (os.environ.get("ZGR_ALLOW_HOSTS") or "").split(","):
        x = x.strip().lower()
        if x:
            names.add(x)
    return {n for n in names if n}


# 静态资源 gzip 缓存：<(路径, mtime_ns, 大小)> → 压缩后的字节。
# app.js 153 KB 压完约 40 KB，style.css 52 KB 压完约 10 KB；
# 文件一升级 mtime 就变，key 随之失效，不会发出旧内容。
_GZ_CACHE: dict = {}
_GZ_CACHE_BYTES = [0]
_GZ_CACHE_MAX = 32 << 20
_GZ_LOCK = threading.Lock()


def _static_gzip(path: str, st, body: bytes):
    key = (path, st.st_mtime_ns, st.st_size)
    with _GZ_LOCK:
        hit = _GZ_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        c = zlib.compressobj(6, zlib.DEFLATED, 31)   # 31 = gzip 容器
        out = c.compress(body) + c.flush()
    except zlib.error:
        return None
    if len(out) >= len(body):                        # 压不动就别压
        return None
    with _GZ_LOCK:
        if _GZ_CACHE_BYTES[0] + len(out) > _GZ_CACHE_MAX:
            _GZ_CACHE.clear()
            _GZ_CACHE_BYTES[0] = 0
        _GZ_CACHE[key] = out
        _GZ_CACHE_BYTES[0] += len(out)
    return out


# 令牌猜错计数：一台机器一分钟内错 30 次就先停一会儿再理它。
# 令牌本身是 32 位随机十六进制，猜不出来；这一层是防「被当靶子一直打」时
# 白烧 CPU 和日志，顺便让日志里留下「谁在猜」。
_AUTH_FAILS: dict = {}          # ip -> [窗口起点, 次数, 上次写日志时间]
_AUTH_FAILS_LOCK = threading.Lock()
AUTH_FAIL_LIMIT = 30
AUTH_FAIL_WINDOW = 60.0


def auth_blocked(ip: str) -> bool:
    now = time.time()
    with _AUTH_FAILS_LOCK:
        row = _AUTH_FAILS.get(ip)
        if not row:
            return False
        if now - row[0] > AUTH_FAIL_WINDOW:
            _AUTH_FAILS.pop(ip, None)
            return False
        return row[1] >= AUTH_FAIL_LIMIT


def note_auth_fail(ip: str, route: str) -> None:
    now = time.time()
    with _AUTH_FAILS_LOCK:
        row = _AUTH_FAILS.get(ip)
        if not row or now - row[0] > AUTH_FAIL_WINDOW:
            row = [now, 1, 0.0]
            _AUTH_FAILS[ip] = row
        else:
            row[1] += 1
        if len(_AUTH_FAILS) > 512:            # 别让被扫的机器把内存撑起来
            for k in [k for k, v in _AUTH_FAILS.items() if now - v[0] > AUTH_FAIL_WINDOW]:
                _AUTH_FAILS.pop(k, None)
        # 日志别刷屏：同一台机器 30 秒最多一条
        should_log = now - row[2] > 30
        if should_log:
            row[2] = now
        n = row[1]
    if should_log:
        log("拒绝访问：%s 的请求没带对令牌（%s，本分钟第 %d 次）" % (ip, route, n))


def allowed_hosts() -> set:
    """允许的 Host 名集合。缓存 5 分钟——插拔网线换 IP 后不用重启服务。"""
    global _ALLOWED_HOSTS
    now = time.time()
    with _ALLOWED_HOSTS_LOCK:
        ts, names = _ALLOWED_HOSTS
        if names and now - ts < 300:
            return names
    names = _host_names()
    with _ALLOWED_HOSTS_LOCK:
        _ALLOWED_HOSTS = (now, names)
    return names


def host_allowed(raw: str) -> bool:
    if _ALLOW_ANY_HOST:
        return True
    raw = (raw or "").strip()
    if not raw:
        return True          # HTTP/1.0 客户端（同步脚本）不带 Host
    host = raw.lower()
    if host.startswith("["):                 # [::1]:8795
        host = host.split("]")[0] + "]"
    elif host.count(":") == 1:               # 10.0.0.5:8795
        host = host.rsplit(":", 1)[0]
    host = host.strip().lower().rstrip(".")
    return host in allowed_hosts() or host.strip("[]") in allowed_hosts()


# 会驱动 SolidWorks 的接口（现生成缩略图 / 网格）按来源限速。
# 局域网里任何一台机器都能不带令牌请求 /api/preview?oid=…，而一次没缓存的请求
# 就会在这台机器上起一个 SolidWorks 导出（几十秒、霸占整机）。正常协作不会
# 一分钟要几十张新图；但如果有人在刷，我们就得有个上限，而不是陪着它把机器烧掉。
_RENDER_BUDGET: dict = {}          # ip -> [窗口起点, 次数]
_RENDER_LOCK = threading.Lock()
# 一分钟 120 次「新生成」：正常打开一个大项目，第一屏几十个零件同时要图是常态，
# 不能因为自家两台的正常协作被限住；但持续刷（每分钟几百次）就会被挡住。
RENDER_PER_MIN = 120
# 非本机、又没配令牌时，同时**真正在生成**的预览最多几张。
# 生成一张要拉 SolidWorks、前台最多等 25 秒；不限并发的话，局域网里一个人
# 拿几百个 oid 循环请求就能把 CPU 和渲染槽全占住（每分钟 120 次的上限挡不住
# 「排队」这件事，只会让队伍更长）。满了直接让他稍后再试。
_REMOTE_RENDER = threading.BoundedSemaphore(4)


def render_allowed(ip: str) -> bool:
    now = time.time()
    with _RENDER_LOCK:
        row = _RENDER_BUDGET.get(ip)
        if not row or now - row[0] > 60:
            _RENDER_BUDGET[ip] = [now, 1]
            if len(_RENDER_BUDGET) > 256:
                for k in [k for k, v in _RENDER_BUDGET.items() if now - v[0] > 60]:
                    _RENDER_BUDGET.pop(k, None)
            return True
        if row[1] >= RENDER_PER_MIN:
            return False
        row[1] += 1
        return True


def truthy(v) -> bool:
    """查询参数里的布尔：只认 1/true/yes/on。

    `bool("0")` 是 True —— 以前 `?force=0` 也会强制重新生成网格/缩略图，
    本机一次就能把 SolidWorks 又叫起来重导一遍。
    """
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def valid_oid(oid) -> bool:
    """对象号必须是 64 位十六进制，否则 _obj_path 会被拼出越界路径"""
    return bool(oid) and bool(_HEXOID.match(oid))


def check_remote_url(url, resolve=True) -> str:
    """只放行内网/本机地址，避免服务被当成跳板去访问任意主机（SSRF）。

    resolve=False 时只做字符串检查（缩略图回源是热路径，DNS 留给设置地址那次做）。
    """
    u = urllib.parse.urlsplit((url or "").strip())
    if u.scheme not in ("http", "https"):
        raise SyncError("只支持 http:// 或 https:// 开头的地址：%s" % (url or "（空）"))
    if u.username or u.password:
        raise SyncError("地址里不要带用户名或密码")
    host = (u.hostname or "").strip()
    if not host:
        raise SyncError("地址缺少主机名")
    if host == "localhost" or host.endswith(".local") or ":" in host:
        return url            # localhost / mDNS 名字 / IPv6 字面量（不在这里做公网判定）
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise SyncError("只允许内网地址，%s 是公网地址" % host)
        return url
    if "." not in host:
        return url            # 不带点的 NetBIOS 计算机名，只在内网有意义
    if not resolve:
        return url
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise SyncError("无法解析主机名：%s" % host)
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            raise SyncError("%s 解析到公网地址 %s，已拒绝" % (host, ip))
    return url


def check_share_path(share: str) -> str:
    """共享目录（NAS / 网络共享）的路径校验。

    「共享目录」是个可以写东西的路径：给个盘符根、给个系统目录，服务端就会
    在那儿建仓库、往里写对象。正常的用法是 \\\\服务器\\共享\\追光 或一个挂载盘，
    这两种都放行；磁盘根和系统目录一律拒绝（那两种路径不可能是用户想要的，
    一旦被写满，整台机器的软件都会开始失败）。
    """
    raw = (share or "").strip()
    if not raw:
        raise SyncError("共享目录是空的")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if os.path.dirname(path) == path:
        raise SyncError("共享目录不能是磁盘根目录：%s" % path)
    roots = []
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(env)
        if v:
            roots.append(os.path.normcase(os.path.abspath(v)))
    low = os.path.normcase(path)
    for bad in roots:
        if low == bad or low.startswith(bad + os.sep):
            raise SyncError("共享目录不能放在系统目录里：%s" % path)
    return raw


def _atomic_json(path: str, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


_LOG_PATH = None


def _log_path():
    global _LOG_PATH
    if _LOG_PATH:
        return _LOG_PATH
    try:
        from .registry import appdata_dir
        _LOG_PATH = os.path.join(appdata_dir(), "server.log")
    except Exception:
        _LOG_PATH = os.path.join(os.path.expanduser("~"), "zhuiguang-server.log")
    return _LOG_PATH


def log(msg: str):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    # 无论有没有控制台都落文件：pythonw 下自动记录/同步的结果才查得到
    try:
        p = _log_path()
        if os.path.isfile(p) and os.path.getsize(p) > (5 << 20):
            os.replace(p, p + ".1")
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is None:
        return
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("gbk", "replace").decode("gbk"), flush=True)


# --------------------------------------------------------------------- 分支状态
_bs_cache = {}
_bs_lock = threading.Lock()
_BS_TTL = 8.0


def branch_status(repo: Repo, heads: dict) -> dict:
    """每条分支现在什么状态：{分支名: {"merged_into": 分支名或None, "same_as": ...}}

    「已合并」按 git 的口径算：这条分支的头提交能不能从另一条分支的头提交沿**全部父链**走到。
    合并提交父链有两条，只沿第一父走会漏判，所以用 commit_reachable。
    结果缓存 8 秒——界面每 3 秒轮询一次，不能每次都把整条历史重新走一遍。
    """
    key = (repo.root, tuple(sorted((k, v or "") for k, v in (heads or {}).items())))
    now = time.time()
    with _bs_lock:
        hit = _bs_cache.get(key)
        if hit and now - hit[0] < _BS_TTL:
            return hit[1]
    reach = {}
    for name, h in (heads or {}).items():
        if h and h not in reach:
            reach[h] = repo.commit_reachable(h)
    out = {}
    for name, h in (heads or {}).items():
        merged_into, same_as = None, None
        if h:
            for other, oh in (heads or {}).items():
                if other == name or not oh:
                    continue
                if oh == h:
                    same_as = other
                    break
                if h in reach.get(oh, ()):
                    merged_into = other
                    break
        out[name] = {"head": h or "", "merged_into": merged_into, "same_as": same_as,
                     "merged": bool(merged_into or same_as)}
    with _bs_lock:
        if len(_bs_cache) > 64:
            _bs_cache.clear()
        _bs_cache[key] = (now, out)
    return out


# --------------------------------------------------------------------- 自动记录
def auto_commit_message(st: dict, now_ms: float | None = None) -> str:
    """自动记录的说明：除了时间，还要写清「改了几个、改的是谁」。

    以前只有「自动记录 10-30 14:05」，一天记十几版之后，时间线上每一行都长得一样，
    事后根本分不清哪一版是那次改底盘、哪一版只是 SolidWorks 保存了一下。
    """
    label = time.strftime("%m-%d %H:%M", time.localtime(
        (float(now_ms) if now_ms else time.time() * 1000) / 1000.0))
    added = st.get("added") or []
    modified = st.get("modified") or []
    deleted = st.get("deleted") or []
    n = len(added) + len(modified) + len(deleted)
    if not n:
        return "自动记录 %s" % label
    names = []
    for p in (modified + added + deleted):
        base = os.path.basename(str(p).replace("\\", "/"))
        if len(base) > 18:
            base = base[:17] + "…"
        if base and base not in names:
            names.append(base)
        if len(names) >= 2:
            break
    tail = "、".join(names) + (" 等" if n > len(names) else "")
    return "自动记录 %s · 改 %d 个（%s）" % (label, n, tail)


class Watcher(threading.Thread):
    """轮询工作区，改动稳定后自动记录一次"""

    def __init__(self, repo: Repo, interval: int = 30, lock: threading.RLock | None = None):
        super().__init__(daemon=True)
        self.repo = repo
        self.interval = max(5, int(interval))
        self.stop_flag = threading.Event()
        self.lock = lock or threading.RLock()
        self.last_sig = None
        self.last_commit = None

    def run(self):
        log("自动记录已开启：%s（每 %d 秒检查）" % (self.repo.name, self.interval))
        while not self.stop_flag.is_set():
            try:
                st = self.repo.status()
                sig = (tuple(st["added"]), tuple(st["modified"]), tuple(st["deleted"]))
                pending_pull = os.path.isfile(os.path.join(self.repo.meta, "PULL_NEEDED"))
                if pending_pull:
                    # 有对方的更新还没落地时绝不自动记录：否则会把旧工作区
                    # 提交成一个「把对方改动改回去」的版本，还能被快进推回去
                    if not getattr(self, "_warned_pull", False):
                        log("自动记录暂停：%s 有对方的新版本待更新工作区，请先在网页里处理"
                            % self.repo.name)
                        self._warned_pull = True
                    self.stop_flag.wait(self.interval)
                    continue
                self._warned_pull = False
                if not st["clean"] and sig == self.last_sig:
                    with self.lock:
                        oid = self.repo.commit(auto_commit_message(st))
                    if oid:
                        self.last_commit = oid
                        log("自动记录 %s ← %s（+%d ~%d -%d）" % (
                            oid[:10], self.repo.name,
                            st["counts"]["added"], st["counts"]["modified"],
                            st["counts"]["deleted"]))
                        live_mod.append_event(
                            self.repo, "auto_commit", head=oid,
                            message="自动记录了一次改动",
                            counts=st["counts"],
                            files=([{"path": p, "st": "A"} for p in st["added"]]
                                   + [{"path": p, "st": "M"} for p in st["modified"]]
                                   + [{"path": p, "st": "D"} for p in st["deleted"]])[:30],
                            total=(st["counts"]["added"] + st["counts"]["modified"]
                                   + st["counts"]["deleted"]))
                    self.last_sig = None
                else:
                    self.last_sig = sig
            except Exception as e:  # 单次失败不影响后续
                log("自动记录异常：%s" % e)
            self.stop_flag.wait(self.interval)

    def stop(self):
        self.stop_flag.set()


class CloneJob(threading.Thread):
    """把一个对方机器上的项目克隆到本机（后台跑，前端轮询进度）

    克隆几千个文件要几分钟，不能占着 HTTP 请求不放；进度存在这个对象里，
    /api/peer/clone/status 直接读快照。
    """

    def __init__(self, url: str, project: str, dest: str, token: str = "",
                 on_success=None):
        super().__init__(daemon=True)
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.project = project
        self.dest = dest
        self.token = token
        self.on_success = on_success
        self.state = "running"        # running / done / error
        self.phase = "排队"
        self.done = 0
        self.total = 0
        self.detail = ""
        self.error = ""
        self.result = None
        self.started = time.time()
        self._lk = threading.Lock()

    def snapshot(self):
        with self._lk:
            return {"id": self.id, "state": self.state, "phase": self.phase,
                    "done": self.done, "total": self.total, "detail": self.detail,
                    "error": self.error, "result": self.result,
                    "project": self.project, "dest": self.dest, "url": self.url,
                    "elapsed": round(time.time() - self.started, 1)}

    def progress(self, phase=None, done=None, total=None, detail=None):
        with self._lk:
            if phase is not None:
                self.phase = phase
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = detail

    def run(self):
        try:
            res = peerjoin.clone(self.url, self.project, self.dest, self.token,
                                 progress=self.progress)
            self.result = res
            if self.on_success:
                self.on_success(res)
            self.progress(phase="完成", done=res.get("objects") or 0,
                          total=res.get("objects") or 0)
            with self._lk:
                self.state = "done"
        except peerjoin.JoinError as e:
            with self._lk:
                self.error = str(e)
                self.state = "error"
            log("克隆失败：%s ← %s（%s）" % (self.project, self.url, e))
        except Exception as e:                                # noqa: BLE001
            with self._lk:
                self.error = "克隆失败：%s" % e
                self.state = "error"
            log("克隆异常：%s" % traceback.format_exc())


class StepJob(threading.Thread):
    """一批零件导出 STEP（后台跑，前端轮询进度）

    SolidWorks 一次只能打开一个文档，几十个零件要跑好几分钟，
    不能占着 HTTP 请求；进度和各项的成败都存在这个对象里。
    """

    def __init__(self, root: str, pairs, dest: str):
        super().__init__(daemon=True)
        self.id = uuid.uuid4().hex[:12]
        self.root = root
        self.pairs = list(pairs)
        self.dest = dest
        self.state = "running"          # running / done / error
        self.phase = "准备"
        self.done = 0
        self.total = len(self.pairs)
        self.detail = ""
        self.error = ""
        self.result = None
        self.started = time.time()
        self._lk = threading.Lock()

    def snapshot(self):
        with self._lk:
            return {"id": self.id, "state": self.state, "phase": self.phase,
                    "done": self.done, "total": self.total, "detail": self.detail,
                    "error": self.error, "result": self.result, "dest": self.dest,
                    "elapsed": round(time.time() - self.started, 1)}

    def progress(self, phase=None, done=None, total=None, detail=None):
        with self._lk:
            if phase is not None:
                self.phase = phase
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = detail

    def run(self):
        try:
            res = swstep.export(self.pairs, self.dest, progress=self.progress, log=log)
            with self._lk:
                self.result = res
                self.done = self.total = len(self.pairs)
                self.phase = "完成"
                self.state = "done" if (res["ok"] or not res["failed"]) else "error"
                if self.state == "error":
                    self.error = res["failed"][0]["error"] if res["failed"] else "导出失败"
        except swstep.StepError as e:
            with self._lk:
                self.error = str(e)
                self.state = "error"
            log("导出 STEP 失败：%s" % e)
        except Exception as e:                                # noqa: BLE001
            with self._lk:
                self.error = "导出 STEP 出错：%s" % e
                self.state = "error"
            log("导出 STEP 异常：%s" % traceback.format_exc())


def _prune_jobs(jobs: dict, keep_done: int = 8, max_age: float = 6 * 3600):
    """清掉过期的、以及堆得太多已完成的后台任务

    每个 BOM 任务都把整张物料表存在内存里（几千行），任务对象只增不减的话
    一天下来就是几百 MB。留最近 keep_done 个已完成的够前端回看就行。"""
    now = time.time()
    for k, v in list(jobs.items()):
        if now - getattr(v, "started", now) > max_age:
            jobs.pop(k, None)
    done = sorted((getattr(v, "started", 0), k) for k, v in jobs.items()
                  if getattr(v, "state", "running") != "running")
    for _t, k in done[:-keep_done] if len(done) > keep_done else []:
        jobs.pop(k, None)


class BomJob(threading.Thread):
    """读一个装配体出物料清单（后台跑，前端轮询进度）

    和导 STEP 一样要独占 SolidWorks 几分钟，不能占着 HTTP 请求；
    采到的行留在对象里，前端一次拿一屏做预览，长表不整包回传。
    """

    def __init__(self, project_root: str, assembly: str, dest: str,
                 include_children=True, merge_by="code"):
        super().__init__(daemon=True)
        self.id = uuid.uuid4().hex[:12]
        self.project_root = project_root
        self.assembly = assembly
        self.dest = dest
        self.include_children = bool(include_children)
        self.merge_by = merge_by or "code"
        self.state = "running"          # running / done / error
        self.phase = "准备"
        self.done = 0
        self.total = 1
        self.detail = ""
        self.error = ""
        self.columns = []
        self.rows = []
        self.result = None
        self.started = time.time()
        self._lk = threading.Lock()

    def snapshot(self, preview=300):
        with self._lk:
            return {"id": self.id, "state": self.state, "phase": self.phase,
                    "done": self.done, "total": self.total, "detail": self.detail,
                    "error": self.error, "dest": self.dest,
                    "columns": [[t, f] for t, f in self.columns],
                    "rows": self.rows[:max(0, int(preview))],
                    "total_rows": len(self.rows),
                    "result": self.result,
                    "elapsed": round(time.time() - self.started, 1)}

    def run(self):
        try:
            with self._lk:
                self.phase = "打开装配体、逐层读零件"
            res = bom_mod.export(
                self.assembly, self.dest,
                include_children=self.include_children,
                merge_by=self.merge_by, project_root=self.project_root,
                log=log)
            with self._lk:
                self.columns = res["columns"]
                self.rows = res["rows"]
                self.result = {k: v for k, v in res.items()
                               if k not in ("columns", "rows")}
                self.phase = "完成"
                self.done = self.total = 1
                self.state = "done"
            log("BOM：%s 共 %d 个零件（%d 行），%s"
                % (os.path.basename(self.assembly), res["parts_seen"],
                   res["files"], os.path.basename(res["xlsx"])))
        except bom_mod.BomError as e:
            with self._lk:
                self.error = str(e)
                self.state = "error"
            log("BOM 失败：%s" % e)
        except Exception as e:                                  # noqa: BLE001
            with self._lk:
                self.error = "生成 BOM 出错：%s" % e
                self.state = "error"
            log("BOM 异常：%s" % traceback.format_exc())


class App:
    def __init__(self, registry: Registry, token: str | None = None,
                 default_project: str | None = None):
        self.registry = registry
        self.token = (token or "").strip() or None
        self.default_project = default_project
        self.watchers: dict[str, Watcher] = {}
        # 文件系统通知（只用来判断「有没有必要重新扫」；用不了就自动退回轮询）
        self.fs_watch = watchfs.Manager()
        self.lives: dict[str, live_mod.LiveSync] = {}
        self.auto_watches: set[str] = set()  # 实时同步顺带拉起的自动记录
        self.lock = threading.RLock()      # 串行化同一进程内的写入（自动记录 vs 手动记录）
        self.started = time.time()
        self.port = 8795
        self.host = socket.gethostname()
        self._status_cache: dict[str, tuple[float, str, dict]] = {}
        self._status_flight: dict[str, threading.Lock] = {}
        self._prewarmed: dict[str, str] = {}
        self._peer_cache: tuple[float, list[dict]] = (0.0, [])
        self.clones: dict[str, "CloneJob"] = {}      # 正在从对方机器克隆的项目
        self.steps: dict[str, "StepJob"] = {}        # 正在导出的 STEP 批次
        self.boms: dict[str, "BomJob"] = {}          # 正在生成的物料清单
        self._roots_cache: dict[str, tuple[str, list[str]]] = {}
        self._objects_cache: dict[str, tuple[float, int]] = {}   # 对象个数（只用于显示）
        self._filelog_cache: dict[tuple, tuple[float, dict]] = {}
        self._find_index: dict[str, tuple[tuple, list]] = {}   # 全历史搜索的说明索引
        self._find_locks: dict[str, threading.Lock] = {}       # 建索引时按项目串行
        # 每个提交「改了哪几个文件」：提交对象不可变，算一次就能一直用。
        # 时间线一屏 400 个版本、三千个零件的项目，每次刷新都重算就是半秒。
        self._changes_cache: dict[tuple, dict] = {}
        self._changes_lock = threading.Lock()
        # 「今天记了几个版本」：比画整条时间线还贵（5000 版的老项目一次 ~125 ms），
        # 而界面每隔几秒就问一次。各分支头没动就说明没有新版本，答案必然一样。
        self._day_cache: dict[str, tuple[tuple, tuple[int, bool]]] = {}
        self._day_lock = threading.Lock()
        self._autopair_stop = threading.Event()
        self._autopair_thread: threading.Thread | None = None
        if default_project:
            self.registry.add(default_project)

    # ------------------------------------------------------------ 改动数缓存
    def changes_cache_get(self, key):
        with self._changes_lock:
            hit = self._changes_cache.get(key)
        return hit

    def changes_cache_put(self, key, info):
        with self._changes_lock:
            self._changes_cache[key] = dict(info)
            # 每个版本一条，上限给足（几万条 = 几 MB）；超了就扔最早的 1/8，
            # 不需要 LRU 那么精细——重算一次也就是几十微秒
            if len(self._changes_cache) > 40000:
                for k in list(self._changes_cache)[:5000]:
                    self._changes_cache.pop(k, None)

    def clear_changes_cache(self):
        with self._changes_lock:
            self._changes_cache.clear()

    # -------------------------------------------------------- 今天的版本数缓存
    def day_count(self, repo: Repo, since: int, cap: int = 20000):
        """「今天记了几个版本」：从各分支头往回数，比画整条时间线还贵。

        实测（隔离掉文件数影响后）：200 版 5.5 ms、500 版 12.8 ms、
        时间线画 400 版反而只要 11.3 ms。界面每隔几秒刷新一次，
        每次都白走一遍就是纯浪费。提交对象不可变、各分支头没动
        就说明没有新版本，答案必然一样，所以按「项目 + 各分支头 + since」记住。

        任何一个分支指向新提交，指纹就变，自然重算——不存在「给出旧数字」
        的窗口，也就不需要 TTL。分支被删/回退同样是指纹变化。
        """
        try:
            heads = tuple(sorted((b, repo.head_oid(b) or "") for b in repo.branches()))
        except RepoError:
            heads = ()
        key = os.path.normcase(repo.root)
        sig = (heads, int(since), max(1, int(cap)))
        with self._day_lock:
            hit = self._day_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        res = repo.count_since(since, cap=cap)
        with self._day_lock:
            self._day_cache[key] = (sig, res)
        return res

    # ------------------------------------------------------------------ 基础
    def repo(self, path: str, must_exist=True) -> Repo:
        if not path:
            raise RepoError("缺少项目路径")
        r = Repo(path)
        if must_exist and not r.exists:
            raise RepoError("这里不是追光几何项目：%s" % path)
        return r

    def filelog(self, repo: Repo, file: str, with_work: bool = True) -> dict:
        """单个文件的历史版本。按 (项目, HEAD, 文件) 缓存 20 秒：

        3D 弹窗会一次拉完整列表、再按版本逐个要点，重复请求不该重新走一遍提交链。
        """
        file = (file or "").replace("\\", "/").lstrip("/")
        key = (os.path.normcase(repo.root), repo.head_oid() or "", file, bool(with_work))
        now = time.time()
        with self.lock:
            hit = self._filelog_cache.get(key)
        if hit and now - hit[0] < 20:
            return hit[1]
        data = Handler.file_history(repo, file, with_work=with_work)
        with self.lock:
            self._filelog_cache[key] = (now, data)
            if len(self._filelog_cache) > 96:
                old = sorted(self._filelog_cache, key=lambda k: self._filelog_cache[k][0])[:32]
                for k in old:
                    self._filelog_cache.pop(k, None)
        return data

    def sync_repo(self, project: str | None, allow_path: bool = False) -> Repo:
        """找到「这次同步说的是哪个项目」。

        allow_path=False 是**对端从局域网来问**的场合：对方给的 project 只能当名字用，
        绝不能当路径。否则局域网里任何一台机器写一句 `project = "\\\\别的机器\\共享"`
        或 `D:\\项目`，本机就去开那个目录读写了 —— 等于把「同步协议」变成
        「按对方指定的任意路径建仓库/写对象」。本机发起的（网页、脚本）才认路径。
        """
        if project:
            entry = self.registry.get(project)
            if entry:
                return self.repo(entry["path"])
            matches = []
            for p in self.registry.projects:
                r = Repo(p["path"])
                if project in (p.get("name"), r.name):
                    matches.append(r)
            if len(matches) == 1:
                return self.repo(matches[0].root)
            if len(matches) > 1:
                # 本机调用者给路径方便消歧；对端只回个数，不回本机路径
                if allow_path:
                    raise RepoError("本机有 %d 个同名项目「%s」，请改用完整路径指定：%s" % (
                        len(matches), project, "、".join(m.root for m in matches)))
                raise RepoError("本机有 %d 个同名项目「%s」，没法判断要哪个；"
                                "请在同步设置里改用不重名的项目名" % (len(matches), project))
            if allow_path:
                r = Repo(project)
                if r.exists:
                    return r
            # 绝不能默默挑一个项目同步，否则会写进错误的仓库
            raise RepoError("没有项目「%s」；请先在建立项目的那台机器上建好同名项目，"
                            "或在同步设置里填写对方的项目名" % project)
        if self.default_project:
            return self.repo(self.default_project)
        for p in self.registry.projects:
            r = Repo(p["path"])
            if r.exists:
                return r
        raise RepoError("还没有任何追光几何项目")

    def repo_with(self, oid: str):
        """找一个包含该对象的项目（对端按 oid 取缩略图时用）"""
        if not valid_oid(oid):
            return None
        for p in self.registry.projects:
            r = Repo(p["path"])
            if r.exists and r.has(oid):
                return r
        return None

    def watcher_on(self, path: str) -> bool:
        w = self.watchers.get(os.path.normcase(os.path.abspath(path)))
        return bool(w and w.is_alive())

    def start_watch(self, repo: Repo, interval: int | None = None, persist=True):
        key = os.path.normcase(repo.root)
        with self.lock:
            old = self.watchers.pop(key, None)
            if old:
                old.stop()
            def _tune(cfg):
                if interval:
                    cfg["watch_interval"] = int(interval)
                if persist:
                    cfg["watch"] = True
            cfg = repo.update_config(_tune)
            w = Watcher(repo, int(cfg.get("watch_interval") or 30), self.lock)
            self.watchers[key] = w
        w.start()
        return w

    def stop_watch(self, path: str, persist=True):
        key = os.path.normcase(os.path.abspath(path))
        w = self.watchers.pop(key, None)
        if w:
            w.stop()
        if persist and os.path.isdir(os.path.join(path, META_DIR)):
            r = Repo(path)
            # /api/watch 也可能从另一台带令牌的机器发起：读-改-写必须进跨进程锁
            r.update_config(lambda cfg: cfg.update({"watch": False}))
        return bool(w)

    def resume_watches(self):
        for p in self.registry.projects:
            r = Repo(p["path"])
            if not r.exists:
                continue
            try:
                if r.config.get("watch"):
                    self.start_watch(r, persist=False)
            except RepoError:
                pass

    # ------------------------------------------------------------------ 对方机器上的项目
    def peer_projects(self, url: str = "", token: str = "") -> dict:
        """列出对方机器上有哪些项目（带版本数/大小/最后记录人），并标出本机已有的

        url 为空时扫一遍局域网（结果缓存 30 秒，左右两栏同时刷新不会重复扫）。
        """
        peers_meta = []
        bases = []
        if url:
            url = check_remote_url(url)
            bases = [url]
        else:
            now = time.time()
            peers = self._peer_cache[1] if (
                now - self._peer_cache[0] < 30 and self._peer_cache[1]) else []
            if not peers:
                try:
                    # 发现阶段不带令牌（带了就是把口令广播给整个网段）
                    peers = autopeer.discover(self.port, timeout=0.6)
                except Exception:                             # noqa: BLE001
                    peers = []
                self._peer_cache = (now, peers)
            me = (self.host or "").lower()
            bases = [p.get("url") for p in peers
                     if p.get("url") and (p.get("host") or "").lower() != me]

        known = set()
        for p in self.registry.projects:
            name = (p.get("name") or "").strip().lower()
            if name:
                known.add(name)
            try:
                known.add((Repo(p["path"]).name or "").strip().lower())
            except Exception:                                 # noqa: BLE001
                pass

        items = []
        for base in bases[:6]:
            try:
                d = peerjoin.list_projects(base, token)
            except peerjoin.JoinError as e:
                peers_meta.append({"url": base, "host": "", "error": str(e)})
                continue
            host = d.get("host") or ""
            peers_meta.append({"url": base, "host": host, "error": ""})
            for proj in d.get("projects") or []:
                name = (proj.get("name") or "").strip()
                if not name:
                    continue
                items.append({
                    "name": name,
                    "known": name.lower() in known,
                    "url": base,
                    "peer_host": host,
                    "branch": proj.get("branch") or "main",
                    "head": proj.get("head") or "",
                    "files": proj.get("files") or 0,
                    "bytes": proj.get("bytes") or 0,
                    "time": proj.get("time"),
                    "author": proj.get("author") or "",
                    "message": proj.get("message") or "",
                })
        items.sort(key=lambda x: (x["known"], -(x.get("time") or 0)))
        return {"ok": True, "host": self.host, "peers": peers_meta, "projects": items}

    def start_clone(self, url: str, project: str, dest: str, token: str = "") -> "CloneJob":
        # 令牌跟着闭包走，不写进 result（那个会原样返回给浏览器）
        job = CloneJob(url, project, dest, token=token,
                       on_success=lambda res: self._after_clone(res, token))
        with self.lock:
            for k, v in list(self.clones.items()):
                if time.time() - v.started > 3600:
                    self.clones.pop(k, None)
            self.clones[job.id] = job
        job.start()
        return job

    # ------------------------------------------------------------------ 导出 STEP
    def start_step(self, root: str, rels, dest: str) -> "StepJob":
        """校验一批相对路径，然后后台导 STEP。

        路径只允许是项目内的相对路径：前端传什么都行，落盘前必须挡住
        `..` / 盘符 / 绝对路径，否则等于给了个读全盘的导出接口。
        """
        root = os.path.abspath(root)
        real_root = os.path.realpath(root)
        seen, pairs = set(), []
        for rel in (rels or []):
            rel = str(rel or "").replace("\\", "/").strip()
            while rel.startswith("./"):
                rel = rel[2:]
            rel = rel.lstrip("/")
            if not rel:
                continue
            if ".." in rel.split("/") or ":" in rel:
                raise RepoError("文件名不合法：%s" % rel)
            if os.path.splitext(rel)[1].lower() not in swstep.STEP_EXTS:
                raise RepoError("只有 SolidWorks 零件/装配体/图纸能导 STEP：%s" % rel)
            src = os.path.join(root, *rel.split("/"))
            real = os.path.realpath(src)
            if real != real_root and not real.startswith(real_root + os.sep):
                raise RepoError("文件名不合法：%s" % rel)
            if rel in seen:
                continue
            seen.add(rel)
            pairs.append((rel, src))
        if not pairs:
            raise RepoError("没有选中任何零件")
        dest = swstep.check_dest(dest, root)
        busy = self._sw_job_busy()
        if busy:
            raise RepoError(busy)
        job = StepJob(root, pairs, dest)
        with self.lock:
            _prune_jobs(self.steps, 8)
            self.steps[job.id] = job
        job.start()
        return job

    # ------------------------------------------------------------------ 物料清单
    def _sw_job_busy(self) -> str:
        """同一时刻只让一个任务驱动 SolidWorks。

        两个任务一起跑会接到同一个 SolidWorks 实例上（CreateObject 是接到已有实例的），
        一个在 SwitchDoc、另一个在 CommandInProgress，两个都以为自己独占 —— 结果是
        两边各读半棵树或者都卡住。与其让用户等到一半才发现，不如现在就说明白。"""
        now = time.time()
        for label, jobs in (("导出 STEP", self.steps), ("物料清单", self.boms)):
            for v in list(jobs.values()):
                if getattr(v, "state", "") == "running" and now - v.started < 6 * 3600:
                    what = getattr(v, "assembly", None) or (
                        "%d 个文件" % getattr(v, "total", 0))
                    return ("%s 还在跑（%s），一台机器同时只能动一个 SolidWorks。"
                            "等它完成再来，或者先在任务列表里把那个关掉。"
                            % (label, what))
        return ""

    def start_bom(self, root: str, rel: str, dest: str = "",
                  include_children=True, merge_by="code") -> "BomJob":
        """校验一个装配体相对路径，后台生成 BOM（xlsx + csv）"""
        root = os.path.abspath(root)
        rel = str(rel or "").replace("\\", "/").strip().lstrip("/")
        if not rel or ".." in rel.split("/") or ":" in rel:
            raise RepoError("文件名不合法：%s" % rel)
        if os.path.splitext(rel)[1].lower() not in bom_mod.ASSEMBLY_EXT:
            raise RepoError("只有 .sldasm 装配体能出物料清单：%s" % rel)
        real_root = os.path.realpath(root)
        src = os.path.join(root, *rel.split("/"))
        real = os.path.realpath(src)
        if real != real_root and not real.startswith(real_root + os.sep):
            raise RepoError("文件名不合法：%s" % rel)
        if not os.path.isfile(src):
            raise RepoError("工作区里没有这个装配体（可能还没更新到本机）：%s" % rel)
        if dest:
            dest = os.path.abspath(dest)
            if not os.path.isdir(dest):
                try:
                    os.makedirs(dest, exist_ok=True)
                except OSError as e:
                    raise RepoError("导出目录用不了：%s（%s）" % (dest, e)) from None
        else:
            dest = os.path.join(os.path.dirname(root.rstrip("\\/")),
                                os.path.basename(root.rstrip("\\/")) + "-BOM")
        busy = self._sw_job_busy()
        if busy:
            raise RepoError(busy)
        job = BomJob(root, src, dest, include_children=include_children,
                     merge_by=merge_by)
        with self.lock:
            _prune_jobs(self.boms, 8)
            self.boms[job.id] = job
        job.start()
        return job

    def _after_clone(self, res: dict, token: str = ""):
        """克隆成功后：登记成本机项目，并直接和对方开实时同步（不用再去设置页点一次）"""
        try:
            self.registry.add(res["path"], res.get("name"))
            log("克隆完成：%s ← %s（%d 个对象，%s 个文件）" % (
                res["path"], res.get("peer"), res.get("objects") or 0, res.get("files") or 0))
            repo = self.repo(res["path"])
            try:
                self.start_live(repo, res["peer"], token=token, interval=15,
                                auto_watch=False, auto=True,
                                mode=live_mod.MODE_SPLIT)
            except RepoError as e:
                log("克隆后开实时同步失败：%s" % e)
        except Exception:                                     # noqa: BLE001
            log("克隆后登记项目失败：%s" % traceback.format_exc())

    # ------------------------------------------------------------------ 实时同步
    def live_status(self, repo: Repo):
        key = os.path.normcase(repo.root)
        w = self.lives.get(key)
        cfg = {}
        try:
            cfg = repo.config.get("live") or {}
        except RepoError:
            pass
        watching = key in self.watchers
        extra = {"auto": cfg.get("auto", True),
                 "peer_host": cfg.get("peer_host") or "",
                 "peer_author": cfg.get("peer_author") or ""}
        # 没存过方式的（老项目）按新默认「一人一条线」显示，免得一开又回到自动合并
        mode = cfg.get("mode") or live_mod.MODE_SPLIT
        if w and w.is_alive():
            return dict(w.status, enabled=True, watch=watching,
                        my_branch=repo.branch, **extra)
        return {"enabled": False, "running": False, "url": cfg.get("url") or "",
                "interval": cfg.get("interval") or 15,
                "mode": mode, "my_branch": repo.branch, "peer_branches": [],
                "auto_pull": bool(cfg.get("auto_pull", True)),
                "auto_push": bool(cfg.get("auto_push", True)),
                "auto_merge": bool(cfg.get("auto_merge", True)),
                "checkout": bool(cfg.get("checkout", True)),
                "watch": watching, "state": "off", **extra}

    def start_live(self, repo: Repo, url: str, token: str = "", interval: int = 15,
                   auto_pull=True, auto_push=True, auto_merge=True, checkout=True,
                   auto_watch=False, auto: bool | None = None,
                   mode: str = live_mod.MODE_SHARED):
        if not url:
            raise RepoError("请填写对方地址")
        check_remote_url(url)
        mode = (live_mod.MODE_SPLIT if str(mode).lower() == live_mod.MODE_SPLIT
                else live_mod.MODE_SHARED)
        key = os.path.normcase(repo.root)
        old = self.lives.pop(key, None)
        if old:
            old.stop()
        branch_note = None
        if mode == live_mod.MODE_SPLIT:
            # 一人一条线：先确保「我」有自己的分支，否则两个人还会往同一条线上摞
            try:
                branch_note = live_mod.ensure_personal_branch(repo)
                if branch_note.get("created"):
                    log("一人一条线：%s 从 %s 分出分支「%s」并切过去"
                        % (repo.name, branch_note.get("from"),
                           branch_note.get("branch")))
            except RepoError as e:
                log("一人一条线：分分支失败（%s），继续用当前分支" % e)
        # 自动记录是不是我们替用户拉起来的（暂停实时同步时要一并停掉）。
        # 只要这次开了「自动记录改动」，就把这台机器的记录线程算作实时同步的一部分，
        # 否则之前遗留的 watch=True 会让暂停后还在后台记账。
        own_watch = bool(auto_watch)
        base = {"enabled": True, "url": url.rstrip("/"),
                "interval": int(interval), "mode": mode,
                "auto_pull": bool(auto_pull), "auto_push": bool(auto_push),
                "auto_merge": bool(auto_merge) and mode == live_mod.MODE_SHARED,
                "checkout": bool(checkout),
                "auto_watch": bool(auto_watch), "own_watch": own_watch}

        def _save(c):
            # 在锁里读当前 live 再合并：auto / peer_host / peer_author 是别的路径
            # （用户点过暂停、自动配对认出对方是谁）写进去的，不能拿外面读到的旧值盖掉
            prev = c.get("live") or {}
            live = dict(base)
            live["auto"] = bool(prev.get("auto", True)) if auto is None else bool(auto)
            live["peer_host"] = prev.get("peer_host") or ""
            live["peer_author"] = prev.get("peer_author") or ""
            c["live"] = live
            if token:
                c["live_token"] = token
            c["auto_checkout"] = bool(checkout)
        with self.lock:
            repo.update_config(_save)
        w = live_mod.LiveSync(repo, url, token=token, interval=interval,
                              auto_pull=auto_pull, auto_push=auto_push,
                              auto_merge=auto_merge, checkout=checkout,
                              mode=mode, lock=self.lock, logger=log)
        self.lives[key] = w
        w.start()
        # 「对方改了并保存，这边不用点就能看到」：实时同步顺带开启自动记录。
        # 连续两次扫描签名一致才提交，避免把还在写入的 CAD 文件记成半截版本。
        if own_watch:
            if key not in self.watchers:
                self.start_watch(repo, max(5, min(30, int(interval))))
                log("实时同步：%s 同时开启自动记录（每 %d 秒）"
                    % (repo.name, max(5, min(30, int(interval)))))
            self.auto_watches.add(key)
        elif key in self.auto_watches:
            # 这次明确不勾「自动记录」：把之前由实时同步拉起的线程停掉
            self.auto_watches.discard(key)
            self.stop_watch(repo.root)
        return w

    def stop_live(self, path: str, manual: bool = False):
        key = os.path.normcase(os.path.abspath(path))
        w = self.lives.pop(key, None)
        if w:
            w.stop()
        if os.path.isdir(os.path.join(path, META_DIR)):
            r = Repo(path)
            try:
                live = r.config.get("live") or {}
            except RepoError:
                live = {}
            own = key in self.auto_watches or bool(live.get("own_watch"))
            # 只停我们在开启实时同步时顺带拉起的自动记录；用户自己开的不动
            if own:
                self.auto_watches.discard(key)
                self.stop_watch(path)
                log("实时同步已暂停：%s 的自动记录同时停止" % r.name)

            def _save(c):
                cur = dict(c.get("live") or {})
                cur["enabled"] = False
                if manual:
                    # 用户亲手点了「暂停」：自动配对不再把它拉起来
                    cur["auto"] = False
                if own:
                    cur["own_watch"] = False
                c["live"] = cur
            r.update_config(_save)
        return bool(w)

    def resume_lives(self):
        for p in self.registry.projects:
            r = Repo(p["path"])
            if not r.exists:
                continue
            try:
                cfg = r.config.get("live") or {}
                if cfg.get("enabled", True) and cfg.get("url"):
                    self.start_live(
                        r, cfg["url"], token=r.config.get("live_token") or "",
                        interval=int(cfg.get("interval") or 15),
                        auto_pull=bool(cfg.get("auto_pull", True)),
                        auto_push=bool(cfg.get("auto_push", True)),
                        auto_merge=bool(cfg.get("auto_merge", True)),
                        checkout=bool(cfg.get("checkout", True)),
                        auto_watch=bool(cfg.get("auto_watch", False)),
                        mode=cfg.get("mode") or live_mod.MODE_SHARED)
            except (RepoError, SyncError) as e:
                log("实时同步恢复失败（%s）：%s" % (r.name, e))

    # ------------------------------------------------------------------ 未保存状态（带体积）
    def status_cached(self, repo: Repo, ttl: float = 1.2):
        """工作区状态：1.2 秒内重复轮询直接复用，避免界面每 3 秒全盘重扫。

        过期之后也只让**一个**线程真去全盘重扫，其余线程先吃上一次的结果。
        以前没这道单飞：缓存一过期，并发的请求会同时重扫同一个目录，
        GIL 下直接串成好几秒（实测 8 并发时 /api/status p50 5.4 秒、吞吐 1.5 req/s）。
        """
        key = os.path.normcase(repo.root)
        now = time.time()
        hit = self._status_cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2]
        # 有人动文件系统会主动告诉我们：这一轮没人动，就把上次的答案原样给出去，
        # 一个目录都不用扫（大项目里这是每几秒省下的两百多毫秒）。
        # 但只宽限 MAX_IDLE 秒——通知可能漏、网络盘可能没有通知，到点必须真看一眼。
        sig = self._watch_version(repo)
        if hit and len(hit) > 3 and sig is not None and hit[3] == sig \
                and now - hit[0] < watchfs.MAX_IDLE:
            return hit[1], hit[2]
        # setdefault 在 GIL 下是原子的，同一个项目永远拿到同一把锁
        lk = self._status_flight.setdefault(key, threading.Lock())
        if not lk.acquire(blocking=False):
            # 别的线程已经在扫：有旧值就先给旧值（stale-while-revalidate），
            # 不陪着等；连旧值都没有，才等它这一轮扫完。
            if hit:
                return hit[1], hit[2]
            with lk:
                hit = self._status_cache.get(key)
                if hit:
                    return hit[1], hit[2]
                return self._refresh_status(repo, key)
        try:
            return self._refresh_status(repo, key)
        finally:
            lk.release()

    def commit_index(self, repo: Repo, scan: int = 20000):
        """全部历史的「说明索引」：[(提交号, 第一行说明, 时间, 记录人, 文件数)]。

        搜索是边打字边触发的，每敲一下就从对象库里把几千个提交读一遍太重
        （5000 个版本全读一遍实测 2.6 秒）。所以索引留在内存里，而且是**增量**的：
        记了新的一版之后，从新的分支头往下走，一碰到已经认识的提交就停。

        三处容易写错的地方，这里都防着：
        - 「行数满了就不扫」会让新版本永远进不了搜索 → 每次都走，新增条数收在 cap 内；
        - 第一次用很小的 scan 建了索引，之后再要更大的补不上 → 把「走不完、还没展开」
          的提交记在 rest 里，下次从那儿接着走；
        - 删分支 / 分支指针被回退之后，老索引里会留着**已经不可达**的版本，
          搜索报出来点进去还报错 → 记下「老的哪个分支头这次还走得到」，有一个走不到
          就整段重建。

        返回 (rows, 是否还有更早的没装下)。
        """
        key = os.path.normcase(repo.root)
        try:
            heads = {}
            for b in repo.branches():
                h = repo.head_oid(b)
                if h:
                    heads[b] = h
        except RepoError:
            heads = {}
        cap = max(1, int(scan))
        # 界面边打字边发请求：两个请求同时进来，一个重建、另一个还在走老的 ent，
        # 后写的会把重建结果覆盖掉，删分支留下的幽灵行又回来了。按项目串行。
        lk = self._find_locks.setdefault(key, threading.Lock())
        with lk:
            return self._commit_index_locked(repo, key, heads, cap)

    def _commit_index_locked(self, repo, key, heads, cap):
        def walk(rows, known, rest, limit):
            """从各分支头（和上次没走完的地方）往下走。

            先来的先出：父提交一定比子提交旧，天然从新到旧。
            碰到认识的提交就停并记下「走到了」——那是还能从分支头够到的证据。
            """
            reached = set()
            queue = deque(heads.values())    # 出队用 deque：list.pop(0) 每次整体挪一遍
            for oid in rest:
                if oid not in known:
                    queue.append(oid)
            seen = set()
            added = []
            while queue and len(added) < limit:
                oid = queue.popleft()
                if oid in seen:
                    continue
                seen.add(oid)
                if oid in known:
                    reached.add(oid)
                    continue
                try:
                    c = repo.commit_info(oid)
                except RepoError:
                    continue
                # 存第一行的前 500 个字：搜索要按它比（以前只存 200，
                # 于是「网页搜不到、命令行搜得到」——说明长一点就露馅）。
                # 显示的时候由接口自己截到 200。
                msg = (c.get("message") or "").split(chr(10))[0][:500]
                added.append((oid, msg, c.get("time") or 0, c.get("author") or "",
                              ((c.get("stats") or {}).get("files")) or 0))
                for p in (c.get("parents") or []):
                    if p and p not in seen:
                        queue.append(p)
            if added:
                rows = sorted(rows + added, key=lambda r: r[2], reverse=True)
                known = known | {r[0] for r in added}
            # 「这次没走完」= 撞到 cap 停了，队列里还有没展开的。
            # 这个信息很关键：没走完就不能断言「某个老分支头不可达」，
            # 否则分支头一前进（超过 cap）就会误判成分支被删，把整段索引白清掉。
            truncated = bool(queue) and len(added) >= limit
            # 队列里剩下的就是「还没展开的边界」：这次没轮到，下次从这儿接着走
            return rows, known, reached, [o for o in queue if o not in known], truncated

        ent = self._find_index.get(key)
        if not ent:
            rows, known, _reached, rest, _cut = walk([], set(), [], cap)
        else:
            old_heads, rows, known, old_rest = ent[0], ent[1], ent[2], ent[3]
            rows, known, reached, rest, truncated = walk(rows, known, old_rest, cap)
            # 分支被删（名字没了）一定重建：那些版本已经不可达，留着点进去就报错。
            # 指针回退/强移只能靠「走完了还够不到」判断；没走完（truncated）时
            # 不能下这个结论——那只是这次没轮到。
            vanished = set(old_heads) - set(heads)
            stale = (not truncated) and any(
                h not in (set(heads.values()) | reached) for h in old_heads.values())
            if vanished or stale:
                rows, known, _reached, rest, _cut = walk([], set(), [], cap)
        self._find_index[key] = (heads, rows, known, rest)
        # 索引本身可能上万行，别留太多：留最近用过的 8 个项目就够
        if len(self._find_index) > 8:
            keep = self._find_index[key]
            self._find_index.clear()
            self._find_index[key] = keep
        # 「还有没装下的」= 队列里还留着没展开的，或者索引本来就有比 cap 更多的行
        # （只返回前 cap 条）。用 len(rows) >= cap 判断会把「正好全读完了」
        # 也报成「还没翻完」，界面白写一句「更早的还没翻」。
        return rows[:cap], bool(rest) or len(rows) > cap

    def forget_project(self, path: str):
        """项目从列表里去掉之后，按项目根目录做的缓存也一起丢掉。

        这些缓存（状态、搜索索引、根提交、对象个数、预热标记）都以目录为 key 常驻内存。
        项目删了/搬走了还留着，一来白占内存，二来同路径再建一个项目时，
        可能先读到**旧项目**的索引——搜索会报出这个新项目里根本不存在的版本。
        """
        key = os.path.normcase(os.path.abspath(path))
        self._status_cache.pop(key, None)
        self._status_flight.pop(key, None)
        self._find_index.pop(key, None)
        self._find_locks.pop(key, None)
        self._roots_cache.pop(key, None)
        self._objects_cache.pop(key, None)
        self._prewarmed.pop(key, None)
        for k in [k for k in list(self._filelog_cache) if k and k[0] == key]:
            self._filelog_cache.pop(k, None)

    def invalidate_status(self, repo: Repo):
        """记完一版 / 恢复完之后让状态缓存立刻作废。

        缓存 1.2 秒本来是为了挡住界面每 3 秒一次的轮询，但**刚记完那一刻**必须重算：
        否则「未保存 N 个文件」还挂在那儿，用户以为没记上，回头又点一次记录
        （第二次会得到「没有变化」，更让人怀疑）。
        """
        self._status_cache.pop(os.path.normcase(repo.root), None)

    def _watch_version(self, repo: Repo):
        """文件通知的当前版本号；这台机器/这个盘用不了通知就返回 None（=照旧轮询）"""
        try:
            w = self.fs_watch.get(repo.root)
        except Exception:                                  # noqa: BLE001
            return None
        if w is None:
            return None
        st = w.state()
        return None if st is None else st[0]

    def _refresh_status(self, repo: Repo, key: str):
        # 版本号要在**扫描之前**取：扫描期间万一有人改了文件，版本号会对不上，
        # 下一次询问就会重扫一次。取之前的版本最多多扫一回，绝不会漏掉改动。
        sig = self._watch_version(repo)
        st = repo.status()
        self._status_cache[key] = (time.time(), st.get("head") or "", st, sig)
        if len(self._status_cache) > 64:
            # 两个不同项目的请求会同时走到这里，一个在 sorted() 里遍历，
            # 另一个刚好插入把它撑大 → RuntimeError（接口直接 500）。
            # 快照一次、出错就跳过多余的清理：下次刷新还会再清。
            try:
                old = sorted(list(self._status_cache.items()), key=lambda kv: kv[1][0])[:16]
            except RuntimeError:
                old = []
            for k, _v in old:
                if k != key:
                    self._status_cache.pop(k, None)
                    # 只在没人攥着这把锁时才丢：正在扫的项目还等着它释放
                    lk = self._status_flight.get(k)
                    if lk is not None and not lk.locked():
                        self._status_flight.pop(k, None)
        return st.get("head") or "", st

    # ------------------------------------------------------------------ 缩略图预热（秒开）
    def prewarm(self, repo: Repo):
        """把当前版本里能出图的文件在后台先生成 128 缩略图，翻时间线时就是秒出。"""
        try:
            head = repo.head_oid()
        except RepoError:
            return
        if not head:
            return
        key = os.path.normcase(repo.root)
        if self._prewarmed.get(key) == head:
            return
        self._prewarmed[key] = head
        threading.Thread(target=self._prewarm_worker, args=(repo.root, head),
                         daemon=True, name="zgr-prewarm").start()

    def _prewarm_worker(self, root: str, head: str):
        key = os.path.normcase(os.path.abspath(root))
        try:
            r = Repo(root)
            _, files = r.files_of(head)
        except (RepoError, OSError):
            return
        # 超大文件（上百 MB 的装配体）先不预热，避免开项目就拖慢整机
        # SolidWorks 零件缺内嵌预览时要起 SolidWorks 现导一遍（每个约 20 秒），
        # 开个项目就让 1600 个零件排队会把机器霸占一整晚，所以预热不碰它们；
        # 攒到晚上用 `zgr.py warm-sw` 一次性补，或者谁打开哪个就补哪个。
        items = [(p, v) for p, v in files.items()
                 if preview.is_previewable(p) and int(v.get("size") or 0) <= 120 << 20
                 and not p.lower().endswith((".sldprt", ".sldasm"))]
        # 大文件多半是装配体/大图：先出小的，保证列表第一屏立刻有图
        items.sort(key=lambda kv: -(int(kv[1].get("size") or 0)))
        svc = thumbs_service()
        big = 0
        for i, (p, v) in enumerate(items):
            if self._autopair_stop.is_set() or self._prewarmed.get(key) != head:
                return
            try:
                name = p.rsplit("/", 1)[-1]
                svc.get(r, v.get("oid") or "", name, 128)
                if preview.is_model(p) and big < 50:
                    svc.get(r, v.get("oid") or "", name, 512)
                    big += 1
            except Exception:              # 单个文件失败不能拖垮预热线程
                pass
            time.sleep(0.02)
            # 以前这里卡了个 900 条上限，大项目（2979 个文件）会有三分之二永远没图；
            # 现在一路做完，只在换版本/停止时中断，中间让出 CPU 不影响正常使用

    # ------------------------------------------------------------------ 局域网自动配对
    def _roots_of(self, repo: Repo, head: str) -> list[str]:
        """沿父提交走到没有父提交的根提交；用于判断两边历史是否同源。"""
        if not head:
            return []
        key = os.path.normcase(repo.root)
        cached = self._roots_cache.get(key)
        if cached and cached[0] == head:
            return cached[1]
        roots, seen, stack = set(), set(), [head]
        while stack and len(seen) < 5000:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            try:
                c = repo.commit_info(oid)
            except (RepoError, OSError):
                continue
            ps = [p for p in (c.get("parents") or []) if p]
            if not ps:
                roots.add(oid)
            else:
                stack.extend(ps)
        out = sorted(roots)
        self._roots_cache[key] = (head, out)
        if len(self._roots_cache) > 32:
            for k in list(self._roots_cache)[:8]:
                if k != key:
                    self._roots_cache.pop(k, None)
        return out

    def _lineage_ok(self, repo: Repo, peer: dict, token: str = "") -> bool:
        """两边都有提交时，至少共享一个根提交才自动配对。

        防的是「两台机器各自建了同名项目」被自动配上后互相合并，
        把两个毫不相干的目录搅在一起。只是同名、历史无关就不碰。
        """
        try:
            head = repo.head_oid()
        except (RepoError, OSError):
            return True
        if not head:
            return True
        d = autopeer.refs(peer.get("url") or "", token, repo.name, timeout=2.0)
        if not d or d.get("roots") is None:
            return True                      # 对方是旧版/取不到：保持原来的行为
        theirs = set(d.get("roots") or [])
        if not theirs:
            return True
        mine = set(self._roots_of(repo, head))
        if not mine or (mine & theirs):
            return True
        log("跳过自动配对：%s 与 %s 的项目同名但历史无关" %
            (repo.name, peer.get("host") or peer.get("url")))
        return False

    def remember_peer(self, repo: Repo, url: str, token: str = ""):
        """把对方是谁（主机名、作者）记进项目配置，界面直接显示人名而不是 IP。"""
        p = autopeer.probe(url, token, timeout=1.5)
        if not p or (p.get("host") or "").lower() == self.host.lower():
            return
        author = ""
        for x in (p.get("projects") or []):
            if (x.get("name") or "").lower() == repo.name.lower():
                author = x.get("author") or ""
                break
        with self.lock:
            def _who(cfg):
                live = cfg.get("live") or {}
                live["peer_host"] = p.get("host") or ""
                live["peer_author"] = author
                cfg["live"] = live
            repo.update_config(_who)

    def scan_peers(self, token: str = "", force: bool = False) -> list[dict]:
        """扫局域网找对端。**绝不带令牌**。

        以前扫不到时会「换个带令牌的再扫一遍」，那等于把访问口令明文发给
        整个 /24 网段：任何一台机器开个只记请求头的监听器就能拿到，然后
        拿到本机的全部权限。发现阶段只读项目名/记录人（本来就是广播信息），
        令牌只在**用户配好、确认过的那个地址**上使用。
        """
        now = time.time()
        when, peers = self._peer_cache
        if not force and now - when < 30:
            return peers
        got = autopeer.discover(port=self.port)
        self._peer_cache = (now, got)
        return got

    def auto_pair_repo(self, repo: Repo, force_scan: bool = False):
        """给一个项目自动找到对端并开启实时同步；返回对端信息或 None。"""
        key = os.path.normcase(repo.root)
        cfg = repo.config
        live = cfg.get("live") or {}
        if live.get("auto") is False:      # 用户明确暂停过
            return None
        token = cfg.get("live_token") or ""
        peer = None
        url = (live.get("url") or "").rstrip("/")
        w = self.lives.get(key)
        if w and w.is_alive() and live.get("enabled", True):
            # 已经在跑：只有「连续 3 分钟一次都没对上」才怀疑对方换了地址，重新找
            last_ok = (w.status or {}).get("last_ok") or 0
            if last_ok and time.time() * 1000 - last_ok < 180000:
                # 连接一直正常，但老配置里没记对方是谁：补一次，不重启同步线程
                if url and not (live.get("peer_host") and live.get("peer_author")):
                    self.remember_peer(repo, url, token)
                return None
            if url and autopeer.probe(url, token, timeout=1.5):
                return None
        if url:
            p = autopeer.probe(url, token, timeout=2.5)
            if p and (p.get("host") or "").lower() == self.host.lower():
                p = None                       # 配置里填的其实是自己
            names = [(x.get("name") or "").lower() for x in (p.get("projects") or [])] if p else []
            if p and repo.name.lower() in names:
                peer = p
        if not peer:
            peers = self.scan_peers(token, force=force_scan)
            peer = autopeer.pick(peers, repo.name, local_host=self.host)
        if not peer:
            return None
        if w and w.is_alive() and (peer.get("url") or "").rstrip("/") == url:
            return None          # 找到的还是原来那个地址，没必要重启线程
        if not self._lineage_ok(repo, peer, token):
            return None
        try:
            self.start_live(
                repo, peer["url"], token=token,
                interval=int(live.get("interval") or 8),
                auto_pull=bool(live.get("auto_pull", True)),
                auto_push=bool(live.get("auto_push", True)),
                auto_merge=bool(live.get("auto_merge", True)),
                checkout=bool(live.get("checkout", True)),
                auto_watch=bool(live.get("auto_watch", False)), auto=True,
                mode=live.get("mode") or live_mod.MODE_SPLIT)
        except (RepoError, SyncError) as e:
            log("自动配对失败（%s ← %s）：%s" % (repo.name, peer.get("url"), e))
            return None
        # 记下对方是谁，界面直接显示「已自动配对：姓名」而不是一串 IP
        author = ""
        for x in (peer.get("projects") or []):
            if (x.get("name") or "").lower() == repo.name.lower():
                author = x.get("author") or ""
                break
        with self.lock:
            def _who(cfg):
                live = cfg.get("live") or {}
                live["peer_host"] = peer.get("host") or ""
                live["peer_author"] = author
                cfg["live"] = live
            repo.update_config(_who)
        log("自动配对：%s ↔ %s（%s）" % (repo.name, peer.get("url"),
                                       peer.get("host") or "对方电脑"))
        return peer

    def auto_pair_all(self, force_scan: bool = False):
        out = []
        for p in list(self.registry.projects):
            r = Repo(p["path"])
            if not r.exists:
                continue
            try:
                peer = self.auto_pair_repo(r, force_scan=force_scan)
            except (RepoError, OSError) as e:
                log("自动配对检查失败（%s）：%s" % (r.name, e))
                peer = None
            if peer:
                out.append({"project": r.name, **peer})
                force_scan = False          # 已扫过一次，后面的项目用缓存
        return out

    def start_background(self, interval: int = 60):
        if os.environ.get("ZGR_NO_AUTOPAIR"):
            return
        if self._autopair_thread and self._autopair_thread.is_alive():
            return

        def loop():
            # 等服务完全起来、对端也开机完成，再扫一次
            self._autopair_stop.wait(5)
            last_prune = 0.0
            while not self._autopair_stop.is_set():
                try:
                    self.auto_pair_all()
                except Exception:
                    log(traceback.format_exc())
                # 回收站自动瘦身：开机先做一次，之后每 6 小时一次。
                # 盘满的时候连一次记录都做不成，这件事不能等着用户想起来点按钮。
                now = time.time()
                if now - last_prune > 6 * 3600:
                    last_prune = now
                    try:
                        self.prune_backups_all()
                    except Exception:
                        log(traceback.format_exc())
                    # 缓存（预览图 / 网格）同样只会越用越大，而且长在没人看的地方。
                    # 它们丢了完全不影响版本，所以清起来比回收站更没有顾虑——
                    # 但同样只在超上限时才动，并且从最旧的删起。
                    try:
                        self.prune_caches_all()
                    except Exception:
                        log(traceback.format_exc())
                self._autopair_stop.wait(max(20, int(interval)))

        self._autopair_thread = threading.Thread(target=loop, daemon=True,
                                                 name="zgr-autopair")
        self._autopair_thread.start()

    def prune_backups_all(self) -> list:
        """按每个项目的配置清理回收站；有删掉的就记一行日志。"""
        out = []
        for p in list(self.registry.projects):
            r = Repo(p["path"])
            if not r.exists:
                continue
            try:
                res = r.auto_prune_backups()
            except (RepoError, OSError) as e:
                log("自动清理回收站失败（%s）：%s" % (r.name, e))
                continue
            if res and res.get("removed"):
                log("自动清理回收站：%s 删掉 %d 批旧备份（释放 %.1f MB），"
                    "保留最近 %d 批" % (r.name, len(res["removed"]),
                                        res["bytes"] / 1e6, res["kept"]))
                out.append({"project": r.name, **res})
        return out

    def prune_caches_all(self) -> list:
        """按每个项目的配置清理预览图与网格缓存；真删了才记日志"""
        out = []
        for p in list(self.registry.projects):
            r = Repo(p["path"])
            if not r.exists:
                continue
            try:
                res = cache_mod.prune_repo(r)
            except (RepoError, OSError) as e:
                log("自动清理缓存失败（%s）：%s" % (r.name, e))
                continue
            for kind, label in (("previews", "预览图"), ("meshes", "网格")):
                d = res.get(kind) or {}
                if d.get("removed"):
                    log("自动清理缓存：%s 删掉 %d 个%s（释放 %.1f MB），现在剩 %.1f MB"
                        % (r.name, len(d["removed"]), label, d["bytes"] / 1e6,
                           d.get("total", 0) / 1e6))
                    out.append({"project": r.name, "kind": kind,
                                "removed": len(d["removed"]), "bytes": d["bytes"]})
        return out

    def stop_background(self):
        self._autopair_stop.set()

    # ------------------------------------------------------------------ 项目视图
    def project_list(self):
        out = []
        for p in self.registry.projects:
            r = Repo(p["path"])
            item = {"path": p["path"], "name": p.get("name") or r.name,
                    "added": p.get("added"), "exists": r.exists, "is_repo": False}
            if r.exists:
                try:
                    head = r.head_oid()
                    item.update({
                        "is_repo": True,
                        "repo_name": r.name,
                        "branch": r.branch,
                        "head": head,
                        "watching": self.watcher_on(p["path"]),
                        "watch_interval": r.config.get("watch_interval", 30),
                        "live": self.live_status(r).get("running", False),
                    })
                    if head:
                        c = r.commit_info(head)
                        item["last_time"] = c.get("time")
                        item["last_message"] = c.get("message")
                        item["files"] = (c.get("stats") or {}).get("files")
                        item["bytes"] = (c.get("stats") or {}).get("bytes")
                    try:
                        _, _st = self.status_cached(r)
                        item["dirty"] = not _st["clean"]
                        item["changes"] = sum((_st.get("counts") or {}).values())
                        item["delta"] = (_st.get("totals") or {}).get("delta", 0)
                    except RepoError:
                        item["dirty"] = None
                except Exception as e:      # 单个项目出问题不能拖垮整个列表
                    item["error"] = str(e)
            out.append(item)
        return out

    def project_detail(self, path: str):
        r = self.repo(path)
        head = r.head_oid()
        info = {"path": r.root, "name": r.name, "branch": r.branch, "head": head,
                "branches": [], "watching": self.watcher_on(r.root),
                "watch_interval": r.config.get("watch_interval", 30),
                "ignore": r.ignore_patterns, "marks": r.marks(),
                "author": r.config.get("author") or "",
                "host": self.host,
                "auto_checkout": bool(r.config.get("auto_checkout", True)),
                "live": self.live_status(r)}
        for b in r.branches():
            info["branches"].append({"name": b, "head": r.head_oid(b)})
        if head:
            info["last"] = r.commit_info(head)
        try:
            _, info["status"] = self.status_cached(r)
        except RepoError:
            info["status"] = None
        # 上次恢复被断电/强杀打断了？界面上要能看见，别让人对着半新半旧的工作区发懵
        try:
            info["pending_restore"] = r.pending_restore()
        except (RepoError, OSError):
            info["pending_restore"] = None
        pn = os.path.join(r.meta, "PULL_NEEDED")
        if os.path.isfile(pn):
            try:
                with open(pn, "r", encoding="utf-8") as f:
                    info["pull_needed"] = json.load(f)
            except (OSError, ValueError):
                info["pull_needed"] = {"head": head}
        # 用链接（#p=路径）打开的项目一般不在项目列表里：界面上得说清楚，
        # 并且给一个「加入列表」的出口，而不是让它看起来像「这个项目不存在」
        info["registered"] = any(
            os.path.normcase(os.path.abspath(p.get("path") or "")) == os.path.normcase(r.root)
            for p in self.registry.projects)
        # 对象个数只是给人看的：几十万个小文件走一遍目录不算便宜，
        # 而界面每次刷新都会调这个接口——按项目缓存一分钟，不重复数。
        ck = os.path.normcase(r.root)
        cached = self._objects_cache.get(ck)
        if cached and time.time() - cached[0] < 60:
            info["objects"] = cached[1]
        else:
            n = sum(len(fs) for _, _, fs in os.walk(os.path.join(r.meta, "objects")))
            self._objects_cache[ck] = (time.time(), n)
            if len(self._objects_cache) > 64:
                self._objects_cache.clear()      # 只是显示用的缓存，清掉重数即可
            info["objects"] = n
        if head:
            self.prewarm(r)
        return info


# --------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "ZhuiguangRecord/" + __version__
    # 慢速连接的超时：连上来却慢慢发请求行/头（Slowloris）会把线程一直占着。
    # 每条连接一个线程、线程数没有上限，几百个这样的人就能让正常用户连不上。
    timeout = 30
    # 这一条请求还没读的请求体字节数；None = 还没算过。
    # 一个 Handler 实例服务整条连接，所以每条请求开头都要清零。
    _unread = None

    def setup(self):
        super().setup()
        try:
            self.connection.settimeout(self.timeout)
        except OSError:
            pass

    def handle_one_request(self):
        self._unread = None
        super().handle_one_request()

    # ---- 请求体记账
    def _body_remaining(self) -> int:
        if self._unread is None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            self._unread = max(0, n)
        return self._unread

    def _drain_body(self):
        """回包之前，把客户端发了但我们没读的请求体吃掉。

        不吃会怎样（Windows 实测）：客户端还在往上发 body，我们就把 socket 关了，
        操作系统立刻回 RST，客户端读不到「404 / 令牌无效」这些答复，只看到
        「远程主机强迫关闭了一个现有的连接」——看起来像软件崩了，其实是我们在甩手。
        连接能复用时更糟：残留字节会被当成下一条请求行，后面所有请求全乱。"""
        left = self._body_remaining()
        if left <= 0:
            return
        if left > DRAIN_CAP:
            self.close_connection = True
            self._unread = 0
            return
        try:
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            self.close_connection = True
            left = 0
        self._unread = max(0, left)

    # ---- 输出 helpers
    # 每个回包都带的安全头：不给浏览器猜类型、不许被别的页面套框架、不外传来源地址
    SEC_HEADERS = (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    )
    # HTML 的策略：脚本/样式只认自己这份（内联给它，importmap 必须是内联的），
    # 禁止外链脚本和 fetch 到别处 —— 就算页面里混进一段坏内容，也偷不走数据送出去
    CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
           "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
           "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
           "base-uri 'self'; frame-ancestors 'none'; form-action 'none'")
    # 图形文档（SVG/XML）：只允许内联样式，脚本、外链、表单全关
    CSP_SVG = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
               "sandbox")

    def _sec_headers(self, code, ctype, extra):
        if "X-Content-Type-Options" not in extra:
            self.send_header("X-Content-Type-Options", "nosniff")
        if "X-Frame-Options" not in extra:
            self.send_header("X-Frame-Options", "DENY")
        if "Referrer-Policy" not in extra:
            self.send_header("Referrer-Policy", "no-referrer")
        # 别的站点不能把我们当资源嵌进去（配合 /api 只能从本页面发）
        if "Cross-Origin-Resource-Policy" not in extra:
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        # 别家页面打开的窗口不能反过来操纵我们这一页（window.opener）
        if "Cross-Origin-Opener-Policy" not in extra:
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        # 这软件不需要定位/摄像头/麦克风/USB/支付，一律关掉，被注入也调不起来
        if "Permissions-Policy" not in extra:
            self.send_header("Permissions-Policy",
                             "geolocation=(), camera=(), microphone=(), "
                             "payment=(), usb=(), serial=(), midi=()")
        # Flash/Acrobat 的 crossdomain.xml 一概不认
        if "X-Permitted-Cross-Domain-Policies" not in extra:
            self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        if ctype.startswith("text/html") and "Content-Security-Policy" not in extra:
            self.send_header("Content-Security-Policy", self.CSP)
        elif ("svg" in ctype or "xml" in ctype) and "Content-Security-Policy" not in extra:
            # SVG 是能带 <script> 的文档：在浏览器里直接打开时，它和本页面同源，
            # 一段脚本就能读走 /api/projects（里面有项目路径）。当 <img> 用不会执行，
            # 但用户完全可以右键「在新标签打开」。所以给 SVG 发一份「什么都不许」的 CSP。
            self.send_header("Content-Security-Policy", self.CSP_SVG)

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self._drain_body()
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        self._sec_headers(code, ctype, extra)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_out(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def err(self, msg, code=400):
        # 别的机器看到的错误里不带本机路径；本机（自己和自己的浏览器）照旧给全
        text = str(msg) if self.is_local() else scrub_paths(msg)
        self.json_out({"ok": False, "error": text}, code)

    def body_json(self):
        n = self._body_remaining()
        if n <= 0:
            return {}
        if n > MAX_JSON_BODY:
            raise RepoError("请求体过大：JSON 接口最多 %.0f MB" % (MAX_JSON_BODY / 1048576.0))
        raw = self.rfile.read(n)
        # 先记账再解析：解析失败时这些字节已经吃掉了，再去 drain 就会等一批永远不来的数据
        self._unread = max(0, n - len(raw))
        data = json.loads(raw.decode("utf-8"))
        # null/数组/字符串都是合法 JSON，但不是我们要的对象——早点挡掉，别到后面炸 500
        if not isinstance(data, dict):
            raise RepoError("请求体必须是 JSON 对象")
        return data

    def q(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def q1(self, name, default=""):
        return (self.q().get(name) or [default])[0]

    def qint(self, name, default, lo=None, hi=None) -> int:
        """整数查询参数。

        以前是 `int(self.q1("limit") or 200)` —— 传 `?limit=abc` 直接 ValueError，
        被 do_GET 的兜底当成「内部错误 500」，日志里还留一段 traceback。
        参数不对是**调用方的问题**，就该回 400 并说清楚，顺便别让 `limit=0`、
        负数这类值跑到下游去（`r.log(-5)` 在 Python 里等于取倒数 5 条）。
        """
        raw = (self.q1(name, "") or "").strip()
        if not raw:
            return default
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise RepoError("参数 %s 必须是整数：%s" % (name, raw[:20])) from None
        if lo is not None and v < lo:
            v = lo
        if hi is not None and v > hi:
            v = hi
        return v

    def _day_start_ms(self) -> int:
        """「今天」从哪一刻算起：优先用浏览器报来的本地零点（today_from）。

        浏览器和服务端可能不在同一台机器、甚至不同时区。按服务端零点数，
        另一端 00:00–08:00 记的版本会被算进「昨天」，抬头和卡片就对不上。
        只接受和本机时间相差两天以内的值——查询串是外部输入，不跟着它跑到
        几十年前或几十年后去数历史。
        """
        now = int(time.time() * 1000)
        try:
            got = int((self.q1("today_from") or "").strip())
        except (TypeError, ValueError):
            got = 0
        if got and abs(now - got) <= 2 * 86400000:
            # 掐到整秒，避免同一个零点在两次请求里给出不同的值
            return got - got % 1000
        return int(time.mktime(tuple(time.localtime()[:3]) + (0, 0, 0, 0, 0, -1)) * 1000)

    def token_ok(self):
        if not self.app.token:
            return True
        got = self.headers.get("X-ZGR-Token") or self.q1("token") or ""
        return hmac.compare_digest(got.encode("utf-8"), self.app.token.encode("utf-8"))

    def origin_ok(self):
        """挡跨站请求：浏览器带 Origin 时必须与本站 Host 一致"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            o = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        return bool(o.netloc) and o.netloc.lower() == host

    def api_guard(self, route: str, method="GET") -> bool:
        """配了令牌：/api/* 全部要令牌。
        没配令牌：本机随便用；跨机只放开「同步协议 + 按对象号取缩略图」，
        不能让局域网里任何人借这个口子翻全盘、改别人项目。"""
        if not route.startswith("/api/"):
            return True
        if route == "/api/ping":
            return True
        # 自动配对的第一步：只报项目名/记录人，不返回任何路径和文件。
        # 这一步必须免令牌——否则配了口令的机器根本没法被发现，只能靠把口令
        # 广播给整个网段（那才是真正危险的）。
        if method == "GET" and route == "/api/sync/discover":
            return True
        if not self.token_ok():
            note_auth_fail(self.client_address[0], route)
            return False
        if self.app.token or self.is_local():
            return True
        if route.startswith("/api/sync/"):
            return True
        if method == "GET" and route == "/api/peer/list":
            return True          # 只报名字/分支/大小，不含任何本地路径
        if method == "GET" and route in ("/api/preview", "/api/mesh/peer"):
            return bool(self.q1("oid")) and not self.q1("file")
        return False

    # ---- 静态
    def serve_static(self, rel: str):
        rel = urllib.parse.unquote((rel or "").lstrip("/")) or "index.html"
        rel = rel.replace("\\", "/")
        parts = [x for x in rel.split("/") if x]
        if (not parts or len(parts) != len(rel.split("/"))
                or any(x in (".", "..") or ":" in x for x in parts)):
            return self.err("非法路径", 403)
        root = os.path.realpath(WEB_DIR)
        try:
            p = os.path.realpath(os.path.join(root, *parts))
            if os.path.commonpath([root, p]) != root:
                return self.err("非法路径", 403)
        except ValueError:
            return self.err("非法路径", 403)
        if not os.path.isfile(p):
            return self.err("未找到 %s" % rel, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            # 预览引擎用到的资源：WASM 必须是 application/wasm 才能流式编译
            ".wasm": "application/wasm",
            ".ttf": "font/ttf",
            ".otf": "font/otf",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".json": "application/json; charset=utf-8",
            ".map": "application/json; charset=utf-8",
        }.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
        # 静态资源：ETag + 每次回源验证。
        # 以前是 no-store —— 每次打开界面都要重下 app.js（153 KB）和整套 vendor
        # （含 7.6 MB 的 wasm），局域网也白等；改成 no-cache + ETag 后命中就是
        # 一个 304 空包，而升级后文件 mtime 变了 ETag 必变，绝不会吃到旧界面。
        try:
            st = os.stat(p)
        except OSError:
            return self.err("未找到 %s" % rel, 404)
        etag = '"%x-%x"' % (st.st_mtime_ns, st.st_size)
        wants_gz = (st.st_size > 1024 and self._gzip_ok(ctype)
                    and "gzip" in (self.headers.get("Accept-Encoding") or "").lower())
        # 压缩表示和原样表示是两个不同的实体，ETag 不能共用（否则 304 会张冠李戴）
        etag_out = etag[:-1] + '-gz"' if wants_gz else etag
        if (self.headers.get("If-None-Match") or "").strip() in (etag, etag_out):
            self._drain_body()
            self.send_response(304)
            self.send_header("ETag", etag_out)
            self.send_header("Cache-Control", "no-cache")
            if wants_gz:
                self.send_header("Vary", "Accept-Encoding")
            self._sec_headers(304, ctype, {})
            self.end_headers()
            return None
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError:
            # 刚好在这两步之间被删/被换成目录（升级部署时会发生）：
            # 404 比 500 诚实，也别让用户以为服务坏了
            return self.err("未找到 %s" % rel, 404)
        extra = {"ETag": etag, "Cache-Control": "no-cache"}
        if wants_gz:
            packed = _static_gzip(p, st, body)
            if packed is not None:
                body = packed
                extra["Content-Encoding"] = "gzip"
                extra["Vary"] = "Accept-Encoding"
        self._send(200, body, ctype, extra)

    @staticmethod
    def _gzip_ok(ctype: str) -> bool:
        return (ctype.startswith("text/") or "javascript" in ctype or "json" in ctype
                or "svg" in ctype or "xml" in ctype)

    # ---- 路由
    def _gate(self, route) -> bool:
        """两个方法共用的第一道关：Host 白名单 + 猜令牌限流。
        返回 False 表示已经回过包了。"""
        if not host_allowed(self.headers.get("Host") or ""):
            self.err("不认识这个 Host（防 DNS rebinding）：%s。"
                     "如果确实要用别的域名访问，给服务设环境变量 "
                     "ZGR_ALLOW_HOSTS=<域名>" % (self.headers.get("Host") or ""), 403)
            return False
        if route.startswith("/api/") and auth_blocked(self.client_address[0]):
            self.err("令牌错误次数过多，请稍后再试", 429)
            return False
        return True

    def do_GET(self):
        try:
            self.app.registry.reload()
            route = urllib.parse.urlsplit(self.path).path
            if not self._gate(route):
                return None
            if route.startswith("/api/") and not self.api_guard(route, "GET"):
                return self.err("令牌无效或不允许跨机访问", 401)
            if route.startswith("/api/sync/"):
                return self.sync_get(route)
            if route.startswith("/api/"):
                return self.api_get(route)
            if route == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self.serve_static(route)
        except (RepoError, SyncError) as e:
            self.err(e)
        except Exception as e:
            log(traceback.format_exc())
            self.err("内部错误：%s" % e, 500)

    def do_POST(self):
        try:
            self.app.registry.reload()
            route = urllib.parse.urlsplit(self.path).path
            if not self._gate(route):
                return None
            # 分块传输我们不解码，硬读会把块头当内容。直接拒绝并关连接，别留半截字节
            te = (self.headers.get("Transfer-Encoding") or "").strip().lower()
            if te and te != "identity":
                self.close_connection = True
                return self.err("不支持分块传输，请带上 Content-Length", 411)
            if not self.origin_ok():
                return self.err("跨站请求已被拒绝", 403)
            if route.startswith("/api/") and not self.api_guard(route, "POST"):
                return self.err("令牌无效或不允许跨机访问", 401)
            # 大对象走原始字节流，其余 POST 必须是 JSON —— 挡掉网页表单式 CSRF
            if (route.startswith("/api/") and not route.startswith("/api/sync/object/")
                    and int(self.headers.get("Content-Length") or 0) > 0
                    and "application/json" not in (self.headers.get("Content-Type") or "")):
                return self.err("请求必须是 application/json", 415)
            if route.startswith("/api/sync/"):
                return self.sync_post(route)
            if route.startswith("/api/"):
                return self.api_post(route)
            self.err("未知接口", 404)
        except (RepoError, SyncError) as e:
            self.err(e)
        except ValueError as e:
            self.err("请求格式错误：%s" % e)
        except OSError as e:
            # 盘符不存在、没权限、名字非法：这是请求本身的问题，不是服务器坏了
            self.err("文件系统操作失败：%s" % e)
        except Exception as e:
            log(traceback.format_exc())
            self.err("内部错误：%s" % e, 500)

    # 只认 GET/POST。别的方法也要先吃掉请求体再回 405，否则一样会被 RST 打断
    def _method_not_allowed(self):
        self.close_connection = True
        self.err("只支持 GET 和 POST", 405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    # ------------------------------------------------------------------ 同步协议
    def sync_get(self, route):
        # 发现接口免令牌：它只回项目名和记录人（局域网里本来就是公开信息）。
        # 要求令牌的话，配了口令的机器只能靠「把口令广播给整个网段」被发现。
        if route != "/api/sync/discover" and not self.token_ok():
            return self.err("令牌无效", 401)
        if route == "/api/sync/discover":
            # 自动配对只暴露名字/作者，不返回任何本地路径
            projects = []
            for p in self.app.registry.projects:
                r = Repo(p["path"])
                if not r.exists:
                    continue
                try:
                    author = r.config.get("author") or ""
                except RepoError:
                    author = ""
                projects.append({"name": p.get("name") or r.name, "author": author})
            return self.json_out({
                "ok": True, "app": APP_NAME, "version": __version__,
                "host": self.app.host, "port": self.server.server_port,
                "projects": projects,
                "urls": local_urls(self.server.server_port),
            })
        project = self.q1("project") or None
        repo = self.app.sync_repo(project, allow_path=self.is_local())
        if route == "/api/sync/refs":
            branches = {b: repo.head_oid(b) for b in repo.branches()}
            out = {
                "ok": True, "app": APP_NAME, "version": __version__,
                "name": repo.name, "branches": branches,
                "host": self.app.host,
                "default": repo.branch,
                "urls": local_urls(self.server.server_port),
            }
            # 只有自动配对要判断历史是否同源时才走这一步（LiveSync 每 8 秒轮询不会带这个参数）
            if self.q1("roots"):
                out["roots"] = self.app._roots_of(repo, repo.head_oid())
            return self.json_out(out)
        if route.startswith("/api/sync/object/"):
            oid = route.rsplit("/", 1)[-1]
            if not valid_oid(oid) or not repo.has(oid):
                return self.err("对象不存在", 404)
            p = repo.object_path(oid)
            size = os.path.getsize(p)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return None
        self.err("未知同步接口", 404)

    def sync_post(self, route):
        if not self.token_ok():
            return self.err("令牌无效", 401)
        if route.startswith("/api/sync/object/"):
            return self.sync_put_object(route)
        body = self.body_json()
        repo = self.app.sync_repo(body.get("project") or self.q1("project") or None,
                                  allow_path=self.is_local())
        try:
            branch = safe_branch(body.get("branch") or repo.branch)
        except RepoError as e:
            return self.err(str(e), 400)
        if route == "/api/sync/manifest":
            have = body.get("have")
            head = repo.head_oid(branch)
            missing = sorted(reachable(repo, head) - reachable(repo, have))
            return self.json_out({"ok": True, "head": head, "branch": branch,
                                  "objects": missing})
        if route == "/api/sync/objects":
            stored = skipped = 0
            for oid, b64 in (body.get("objects") or {}).items():
                if repo.put_raw_object(oid, base64.b64decode(b64)):
                    stored += 1
                else:
                    skipped += 1
            return self.json_out({"ok": True, "stored": stored, "skipped": skipped})
        if route == "/api/sync/refs":
            old, new = body.get("old"), body.get("new")
            # 比对旧值 → 写指针 → 落地，全程在同一把锁里：两个对端同时推同一个分支时，
            # 晚到的那个必须看到前一个的结果并失败，而不是两个都 200、后写的覆盖先写的
            with self.app.lock:
                return self._sync_refs(repo, branch, old, new)
        return self.err("未知同步接口", 404)

    def _sync_refs(self, repo, branch, old, new):
        cur = repo.head_oid(branch)
        if (cur or None) != (old or None):
            return self.err("远端分支已变化，请重新拉取（当前 %s）" % (cur or "空")[:12], 409)
        if not new or not repo.has(new):
            return self.err("提交不存在：缺少对象 %s" % (new or "")[:12], 409)
        try:
            verify_complete(repo, new)      # 对象图不完整绝不移动分支
            repo.files_of(new)              # 顺带校验路径（.zgr、..、盘符一律拒绝）
        except (SyncError, RepoError) as e:
            return self.err("对象不完整或路径不合法，已拒绝：%s" % e, 409)
        # 允许快进，也允许「已把对方合并进来」的推送
        if old and not contains(repo, new, old):
            return self.err("新版本没有包含对方历史（既不是快进也不是合并），已拒绝", 409)
        # 收到的这条分支**不是我正在用的那条**（一人一条线）：只更新指针。
        # 绝不能顺手 set_branch + restore —— 那等于把别人的线切成我的线，
        # 我的工作区会被对方的图纸覆盖，自己的版本看着像丢了。
        if branch != repo.branch:
            with self.app.lock:
                repo.set_ref(branch, new)
            log("同步：%s 分支 %s → %s（仅更新指针，不影响当前分支 %s）"
                % (repo.name, branch, new[:12], repo.branch))
            try:
                info = live_mod.describe_commit(repo, new, old)
                live_mod.append_event(
                    repo, "peer_push", head=new, branch=branch,
                    author=info.get("author"),
                    message="对方在自己的线上记了新版本：%s"
                            % (info.get("message") or ""),
                    counts=info.get("counts"), files=info.get("files"),
                    total=info.get("total"))
            except Exception:                                  # noqa: BLE001
                log("写入接收事件失败：%s" % traceback.format_exc())
            return self.json_out({"ok": True, "branch": branch, "head": new,
                                  "applied": "ref_only"})
        # 收下新提交前先判断工作区是否干净，决定能否顺手更新文件
        with self.app.lock:
            clean = False
            try:
                clean = repo.status()["clean"]
            except RepoError:
                pass
            applied = "history_only"
            if bool(repo.config.get("auto_checkout", True)) and clean:
                # 先写标记再落地：restore 失败时不会留下「头已前进、工作区没更新、也没提示」
                _atomic_json(os.path.join(repo.meta, "PULL_NEEDED"),
                             {"head": new, "time": int(time.time() * 1000),
                              "reason": "正在更新工作区"})
                try:
                    repo.restore(new, backup=True)
                    self._clear_pull_flag(repo)
                    repo.set_branch(branch, new)
                    applied = "worktree_updated"
                except RepoError as e:
                    log("同步落地失败：%s（%s），已保留待更新标记" % (repo.name, e))
                    return self.err("工作区更新失败：%s（历史已更新，重试后可继续）" % e, 500)
            else:
                repo.set_branch(branch, new)
                _atomic_json(os.path.join(repo.meta, "PULL_NEEDED"),
                             {"head": new, "time": int(time.time() * 1000),
                              "reason": ("工作区有未记录的改动" if not clean
                                         else "已关闭自动更新")})
                applied = "pull_needed"
        log("同步：%s 分支 %s → %s（%s）" % (
            repo.name, branch, new[:12],
            {"worktree_updated": "工作区已更新", "pull_needed": "待更新工作区",
             "history_only": "仅更新历史"}[applied]))
        try:
            info = live_mod.describe_commit(repo, new, old)
            live_mod.append_event(
                repo, "peer_pull", head=new,
                author=info.get("author"),
                message=info.get("message") or "对方保存了一个新版本",
                counts=info.get("counts"), files=info.get("files"),
                total=info.get("total"), applied=applied)
        except Exception:
            log("写入接收事件失败：%s" % traceback.format_exc())
        return self.json_out({"ok": True, "branch": branch, "head": new,
                              "applied": applied})

    def sync_put_object(self, route):
        """接收大对象：请求体就是压缩后的对象本身，流式落盘，不进内存"""
        oid = route.rsplit("/", 1)[-1]
        if not valid_oid(oid):
            return self.err("对象号不合法", 400)
        n = self._body_remaining()
        if n <= 0:
            return self.err("缺少内容", 400)
        if n > MAX_OBJECT_BODY:
            # 先看大小再找仓库：上限检查不该依赖项目是否存在
            self.close_connection = True
            return self.err("对象太大（上限 %.0f GB）" % (MAX_OBJECT_BODY / (1 << 30)), 413)
        repo = self.app.sync_repo(self.q1("project") or None,
                                  allow_path=self.is_local())
        tmpdir = os.path.join(repo.meta, "tmp")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, "up-%s.part" % oid)
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    self._unread = left
            if left:
                return self.err("数据不完整（还差 %d 字节）" % left, 400)
            stored = repo.put_raw_file(oid, tmp)
        except RepoError as e:
            return self.err(str(e), 400)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return self.json_out({"ok": True, "stored": 1 if stored else 0,
                              "skipped": 0 if stored else 1})

    # ------------------------------------------------------------------ 业务接口 GET
    def api_get(self, route):
        if route == "/api/ping":
            body = {"ok": True, "app": APP_NAME, "version": __version__,
                    "host": self.app.host,
                    "time": int(time.time() * 1000),
                    "urls": local_urls(self.server.server_port),
                    "token_required": bool(self.app.token)}
            # 缓存命中率只报给本机：这是排「打开项目为什么慢」用的，
            # 没必要让局域网里别的人知道这台机器最近读了多少对象。
            if self.is_local():
                from .store import obj_cache_stats
                body["cache"] = obj_cache_stats()
            return self.json_out(body)
        if route == "/api/peer/list":
            # 对方机器问「你这儿有哪些项目」——只给名字/分支/大小/最后一条记录，
            # 一个本地路径都不给（局域网里谁都能问，路径属于隐私）
            projects = []
            for p in self.app.registry.projects:
                r = Repo(p["path"])
                if not r.exists:
                    continue
                item = {"name": p.get("name") or r.name, "branch": None,
                        "branches": [], "head": None, "files": 0, "bytes": 0,
                        "time": None, "author": "", "message": ""}
                try:
                    item["branch"] = r.branch
                    item["branches"] = sorted(r.branches())
                    item["author"] = r.config.get("author") or ""
                    head = r.head_oid()
                    item["head"] = head
                    if head:
                        c = r.commit_info(head)
                        st = c.get("stats") or {}
                        item.update({
                            "files": st.get("files") or 0,
                            "bytes": st.get("bytes") or 0,
                            "time": c.get("time"),
                            "author": c.get("author") or item["author"],
                            "message": (c.get("message") or "").split("\n")[0][:100],
                        })
                except RepoError:
                    continue
                projects.append(item)
            return self.json_out({
                "ok": True, "app": APP_NAME, "version": __version__,
                "host": self.app.host, "port": self.server.server_port,
                "token_required": bool(self.app.token),
                "projects": projects})
        if route == "/api/peer/projects":
            try:
                return self.json_out(self.app.peer_projects(
                    url=(self.q1("url") or "").strip(),
                    token=self.q1("token") or ""))
            except RepoError as e:
                return self.err(str(e), 400)
        if route == "/api/export/step/status":
            job = self.app.steps.get(self.q1("id") or "")
            if not job:
                return self.err("没有这个导出任务（可能已经结束并被清理）", 404)
            return self.json_out({"ok": True, "job": job.snapshot()})
        if route == "/api/bom/status":
            job = self.app.boms.get(self.q1("id") or "")
            if not job:
                return self.err("没有这个 BOM 任务（可能已经结束并被清理）", 404)
            prev = self.qint("preview", 300, lo=0, hi=5000)
            return self.json_out({"ok": True, "job": job.snapshot(preview=prev)})
        if route == "/api/bom/file":
            job = self.app.boms.get(self.q1("id") or "")
            if not job:
                return self.err("没有这个 BOM 任务", 404)
            kind = (self.q1("kind") or "xlsx").lower()
            path = (job.result or {}).get(kind)
            if not path or not os.path.isfile(path):
                return self.err("还没有生成这个文件", 404)
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet" if kind == "xlsx"
                             else "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s"
                             % urllib.parse.quote(os.path.basename(path)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return None
        if route == "/api/peer/clone/status":
            job = self.app.clones.get(self.q1("id") or "")
            if not job:
                return self.err("没有这个下载任务（可能已经结束并被清理）", 404)
            return self.json_out({"ok": True, "job": job.snapshot()})
        if route == "/api/pick/ready":
            # 这台机器（运行服务的那台）能不能弹系统对话框——前端据此决定要不要提示
            ok, why = nativepick.desktop_ready()
            sid = None
            try:
                sid = nativepick.session_id()
            except Exception:                                 # noqa: BLE001
                pass
            return self.json_out({"ok": True, "ready": ok, "reason": why,
                                  "session": sid, "os": sys.platform})
        if route == "/api/status":
            r = self.app.repo(self.q1("path"))
            head, st = self.app.status_cached(r)
            return self.json_out({"ok": True, "head": head, **st})
        if route == "/api/doctor":
            # 和 `zgr doctor` 同一份检查：没记录的东西、待落地、中断的恢复、
            # 最新版本完不完整、回收站/缓存、磁盘、同步、最近记录
            from . import doctor as doctor_mod
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, "report": doctor_mod.report(r)})
        if route == "/api/fsck":
            # 体检只读：查对象缺没缺、坏没坏。deep=1 才逐个重算校验和
            r = self.app.repo(self.q1("path"))
            deep = self.q1("deep") in ("1", "true", "yes")
            rep = r.fsck(deep=deep)
            return self.json_out({"ok": True, "report": rep})
        if route == "/api/size":
            # 项目体积账本：图纸 / 历史 / 回收站 / 缓存 / 打包 / BOM 快照各占多少
            r = self.app.repo(self.q1("path"))
            cfg = r.config
            return self.json_out({
                "ok": True, "report": r.size_report(),
                "cache": {"auto": cfg.get("cache_auto", True) is not False,
                          "preview_max_mb": int(cfg.get("cache_max_mb", 2048) or 0),
                          "mesh_max_mb": int(cfg.get("mesh_max_mb", 2048) or 0)}})
        if route == "/api/search":
            # 跨所有版本按文件名找零件：删掉的老零件也能搜到（标「已删除」）
            r = self.app.repo(self.q1("path"))
            q = self.q1("q")
            res = r.search_history(q, limit=self.qint("limit", 50, lo=1, hi=200))
            return self.json_out({"ok": True, **res})
        if route == "/api/gc":
            # 回收没人引用的对象：GET 只报数（一个都不删），真删走 POST
            r = self.app.repo(self.q1("path"))
            keep = self.q1("keep_days")
            keep = int(keep) if str(keep).isdigit() else 7
            return self.json_out({"ok": True, "plan": r.gc(dry=True, keep_days=keep)})
        if route == "/api/recovery":
            # 上次恢复是不是做到一半被打断了
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, "pending": r.pending_restore()})
        if route == "/api/projects":
            return self.json_out({"ok": True, "projects": self.app.project_list(),
                                  "urls": local_urls(self.server.server_port),
                                  "token_required": bool(self.app.token)})
        if route == "/api/project":
            return self.json_out({"ok": True, **self.app.project_detail(self.q1("path"))})
        if route == "/api/log":
            r = self.app.repo(self.q1("path"))
            limit = self.qint("limit", 200, lo=1, hi=100000)
            heads = {}
            status = {}
            has_more = False
            today = None
            if self.q1("graph"):
                # 分支树：所有分支可达的提交 + 完整 parents，界面才画得出分叉
                commits, heads, has_more = r.log_graph_ex(limit)
                status = branch_status(r, heads)
                # 「今天改了什么」要的是今天的**全部**版本，不是这一屏里的。
                # 今天零点由浏览器给：局域网里另一台机器打开界面时，服务端和
                # 浏览器可能不是同一个时区——按服务端的零点数，00:00–08:00 记的
                # 版本会被算到「昨天」，抬头和卡片就对不上。
                try:
                    since = self._day_start_ms()
                    n, cut = self.app.day_count(r, since)
                    today = {"since": since, "count": n, "truncated": cut}
                except Exception:
                    today = None
            else:
                commits = r.log(limit)
            if self.q1("changes"):
                self._attach_change_counts(r, commits)
            return self.json_out({"ok": True, "commits": commits, "heads": heads,
                                  "branch_status": status, "has_more": has_more,
                                  "today": today,
                                  "branch": r.branch, "head": r.head_oid()})
        if route == "/api/find":
            # 在**全部历史**里按说明/版本号找，而不是只在界面已经加载的那一屏里找。
            # 时间线一次只取 400 个版本（老项目几千个），界面上搜不到时用户会以为
            # 「这个版本不存在」，其实只是还没加载——这是两件事，必须分开说。
            r = self.app.repo(self.q1("path"))
            # 关键词长度封顶：几 MB 的 q 只是白比对一遍历史
            q = (self.q1("q") or "").strip().lower()[:200]
            limit = self.qint("limit", 100, lo=1, hi=500)
            rows, limited = self.app.commit_index(
                r, self.qint("scan", 20000, lo=1, hi=MAX_INDEX_ROWS))
            hits = []
            for cid, msg, when, author, files in rows:
                # 说明、版本号、记录人一起找：一人一条线的项目里，「小冯改的
                # 那几版在哪」是最常见的问法，而说明里往往没有名字。
                # 同时把「靠哪一项对上的」带回去——结果里看不到关键词时，
                # 用户才知道不是搜错了。
                how = []
                if q and cid.startswith(q):
                    how.append("版本号")
                if q and q in msg.lower():
                    how.append("说明")
                if q and q in (author or "").lower():
                    how.append("记录人")
                if how:
                    # 索引里存了前 500 个字用来比，回给界面的只留 200 个字
                    hits.append({"id": cid, "message": msg[:200], "time": when,
                                 "author": author, "files": files,
                                 "how": "+".join(how)})
                    if len(hits) >= limit:
                        break
            return self.json_out({
                "ok": True, "hits": hits, "scanned": len(rows),
                "scan_limited": limited,
                "total_versions": len(rows),
            })
        if route == "/api/commit":
            r = self.app.repo(self.q1("path"))
            c, files = r.files_of(self.q1("id"))
            marks = r.marks().get(c["id"], {})
            parent = (c.get("parents") or [None])[0]
            changed = None
            try:
                if parent and r.has(parent):
                    changed = r.diff(parent, c["id"])
                elif not parent:
                    changed = r.diff("", c["id"])   # 首个版本：全部文件都算新增
            except RepoError:
                changed = None
            return self.json_out({"ok": True, "commit": c, "files": files,
                                  "mark": marks, "parent": parent, "changes": changed})
        if route == "/api/filelog":
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, **self.app.filelog(
                r, self.q1("file"), with_work=self.q1("work") != "0")})
        if route == "/api/diff":
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, **r.diff(self.q1("a"), self.q1("b"))})
        if route == "/api/difftext":
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, **r.commit_diff_text(
                self.q1("a") or None, self.q1("b"), self.q1("file"))})
        if route == "/api/events":
            r = self.app.repo(self.q1("path"))
            since = self.qint("since", 0, lo=0)
            d = live_mod.read_events(r, since)
            return self.json_out({"ok": True, **d,
                                  "live": self.app.live_status(r),
                                  "head": r.head_oid()})
        if route == "/api/cache":
            # 缓存现状与策略（界面拿它来告诉用户「会自动留在多少以内」）
            r = self.app.repo(self.q1("path"))
            cfg = r.config
            pv = os.path.join(r.meta, "previews")
            ms = os.path.join(r.meta, "meshes")
            return self.json_out({
                "ok": True,
                "previews": {"bytes": cache_mod.total_bytes(pv),
                             "files": len(cache_mod._walk(pv))},
                "meshes": {"bytes": cache_mod.total_bytes(ms),
                           "files": len(cache_mod._walk(ms))},
                "auto": cfg.get("cache_auto", True) is not False,
                "preview_max_mb": int(cfg.get("cache_max_mb", 2048) or 0),
                "mesh_max_mb": int(cfg.get("mesh_max_mb", 2048) or 0)})
        if route == "/api/backups":
            r = self.app.repo(self.q1("path"))
            cfg = r.config
            return self.json_out({
                "ok": True,
                "backups": live_mod.list_backups(r),
                "auto_checkout": bool(cfg.get("auto_checkout", True)),
                # 回收站会自动清理：光靠人点「清理」按钮，盘满那天谁都想不到
                "auto_prune": bool(cfg.get("backup_auto", True)),
                "max_mb": int(cfg.get("backup_max_mb", 2048) or 0),
                "min_keep": int(cfg.get("backup_min_keep", 10) or 10),
                "total_bytes": r.backup_total_bytes()})
        if route == "/api/backup/files":
            # 一批备份里有什么：先看清再决定整批复原还是只挑一个
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, **live_mod.backup_files(
                r, self.q1("name"), limit=self.qint("limit", 500, lo=1, hi=5000))})
        if route == "/api/live":
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True, **self.app.live_status(r)})
        if route == "/api/remotes":
            r = self.app.repo(self.q1("path"))
            return self.json_out({"ok": True,
                                  "remotes": remotes_mod.list_remotes(r)})
        if route == "/api/pack/get":
            r = self.app.repo(self.q1("path"))
            if not self.is_local() and not self.token_ok():
                return self.err("下载打包文件需要令牌", 403)
            p = live_mod.pack_file(r, self.q1("name"))
            size = os.path.getsize(p)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s"
                             % urllib.parse.quote(os.path.basename(p)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return None
        if route == "/api/browse":
            return self.browse(self.q1("path"))
        if route == "/api/workfile":
            # 预览工作区里的当前文件（含未提交改动），路径安全由 project_file 把关
            r = self.app.repo(self.q1("path"))
            target = Handler.project_file(r, self.q1("file"))
            ext = os.path.splitext(target)[1].lower()
            ctype = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
                ".svg": "image/svg+xml", ".dxf": "application/dxf; charset=utf-8",
                ".md": "text/markdown; charset=utf-8", ".txt": "text/plain; charset=utf-8",
            }.get(ext, "application/octet-stream")
            try:
                fsize = os.path.getsize(target)
            except OSError as e:
                return self.err("读取文件失败：%s" % e, 500)
            # 以前整个文件读进内存再发。工作区里放一个 3 GB 的装配体，
            # 一次预览就能把服务撑爆 —— 而且这个接口本机就能调。
            if fsize > MAX_WORKFILE:
                return self.err("这个文件太大（%.0f MB），网页里不预览；"
                                "直接用 SolidWorks/CAD 打开它"
                                % (fsize / 1048576.0), 413)
            try:
                with open(target, "rb") as f:
                    body = f.read(MAX_WORKFILE + 1)
            except OSError as e:
                return self.err("读取文件失败：%s" % e, 500)
            if len(body) > MAX_WORKFILE:
                return self.err("这个文件太大，网页里不预览", 413)
            return self._send(200, body, ctype)
        if route == "/api/dwg2dxf":
            # DWG 由服务端用 LibreDWG 转成 DXF，前端只认 DXF
            r = self.app.repo(self.q1("path"))
            oid = self.q1("oid") or ""
            name = self.q1("file") or ""
            try:
                if oid:
                    if not valid_oid(oid) or not r.has(oid):
                        return self.err("文件对象不存在", 404)
                    kind, data = r.get(oid)
                    name = name or oid
                else:
                    target = Handler.project_file(r, name)
                    with open(target, "rb") as f:
                        data = f.read()
            except RepoError as e:
                return self.err(str(e), 404 if "不在工作区" in str(e) else 400)
            try:
                dxf, gz = dxfconvert.convert(name, data, os.path.join(data_dir(), "converted"))
            except RepoError as e:
                return self.err(str(e), 500)
            extra = {"Cache-Control": "private, max-age=300", "Vary": "Accept-Encoding"}
            if "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
                extra["Content-Encoding"] = "gzip"
                body = gz
            else:
                body = dxf
            return self._send(200, body, "application/dxf; charset=utf-8", extra)
        if route == "/api/font":
            # 图纸里的中文需要字体；只放行白名单里的 TTF，避免变成任意文件读取
            name = os.path.basename(self.q1("name") or "")
            if name not in ("simhei.ttf", "deng.ttf", "simsunb.ttf", "segoeui.ttf", "arial.ttf"):
                return self.err("不支持的字体", 403)
            p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
            if not os.path.isfile(p):
                return self.err("字体不存在", 404)
            with open(p, "rb") as f:
                body = f.read()
            return self._send(200, body, "font/ttf", {"Cache-Control": "public, max-age=86400"})
        if route == "/api/file":
            r = self.app.repo(self.q1("path"))
            oid = self.q1("oid")
            if not valid_oid(oid) or not r.has(oid):
                return self.err("文件对象不存在", 404)
            try:
                # 磁盘上的对象是 zlib(头部+内容)，这里只把内容本身发出去
                size = r.payload_size(oid)
            except RepoError as e:
                return self.err(str(e), 500)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                for chunk in r.iter_payload(oid):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return None
        if route == "/api/mesh":
            return self.mesh_out()
        if route == "/api/mesh/info":
            return self.mesh_info()
        if route == "/api/mesh/peer":
            return self.mesh_peer_out()
        if route == "/api/preview":
            return self.preview_out()
        if route == "/api/preview/info":
            r = self.app.repo(self.q1("path"))
            exts = (".sldprt", ".sldasm", ".slddrw", ".dwg", ".step", ".stp", ".igs")
            return self.json_out({
                "ok": True,
                "peer": r.config.get("preview_peer") or "",
                "capable": {e: preview.shell_capable(e) for e in exts},
            })
        if route == "/api/preview/missing":
            return self.preview_missing()
        self.err("未知接口", 404)

    def mesh_out(self):
        """3D 网格：按内容对象号取；没缓存就现让 SolidWorks 生成（大件可能几十秒）"""
        rel = self.q1("file", "")
        oid = self.q1("oid", "")
        if not valid_oid(oid):
            return self.err("缺少或非法的对象号", 400)
        try:
            r = self.app.repo(self.q1("path", ""))
        except RepoError as e:
            return self.err(str(e), 404)
        if not r.has(oid):
            return self.err("这个项目里没有该对象", 404)
        # 没缓存就要现场驱动 SolidWorks：跨机匿名请求按来源限速（和缩略图同一本账）
        if (not mesh_mod.is_cached(r, oid) and not self.is_local() and not self.app.token
                and not render_allowed(self.client_address[0])):
            return self.err("请求太频繁（这台机器的预览生成有上限），请稍后再试", 429)
        cfg = r.config
        peer = "" if self.q1("nopeer") else (cfg.get("preview_peer") or "")
        if peer:
            try:
                check_remote_url(peer, resolve=False)   # 配置可能被手改，回源前再挡一次
            except SyncError:
                peer = ""
        # 已经生成过网格就直接发缓存：没装 SolidWorks 的机器（或 SolidWorks 正开着）
        # 也能看历史上传过的模型，没必要因为「现在生成不了」把缓存也挡掉
        if (not peer and not mesh_mod.solidworks_available()
                and not mesh_mod.is_cached(r, oid)):
            return self.err("这台机器没有 SolidWorks。先在「双机同步」里填上对方地址"
                            "（有 SolidWorks 的那台），3D 网格就会从对端取。", 409)
        try:
            meta = mesh_mod.ensure_mesh(
                r, rel, oid, self.q1("commit") or None,
                force=truthy(self.q1("force")),
                peer=peer, peer_token=cfg.get("preview_peer_token") or "")
        except mesh_mod.MeshError as e:
            return self.err(str(e), 409)
        except Exception as e:  # noqa: BLE001 - 生成链路任何意外都要给出可读信息
            traceback.print_exc()
            return self.err("生成 3D 预览出错：%s" % e, 500)
        p = mesh_mod.cache_file(r, oid)
        if not os.path.exists(p):
            return self.err("网格文件没生成出来", 500)
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-ZGR-Tris", str((meta or {}).get("tris") or 0))
        self.send_header("X-ZGR-Objects", str((meta or {}).get("objects") or 0))
        self.send_header("X-ZGR-Source", str((meta or {}).get("source") or "")[:120])
        missing = (meta or {}).get("missing") or []
        self.send_header("X-ZGR-Missing",
                         urllib.parse.quote(",".join(missing), safe=""))
        if truthy(self.q1("force")):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def mesh_peer_out(self):
        """对端回源接口：只认对象号，不认任意路径；本机现场生成后回给对面。

        这个口子跨机可用（和缩略图一样），所以绝不能接受 file/path 参数，
        也不能让对面借它翻项目列表。
        """
        oid = self.q1("oid", "")
        if not valid_oid(oid):
            return self.err("缺少或非法的对象号", 400)
        r = self.app.repo_with(oid)
        if not r:
            return self.err("这个对象不在本机任何项目里", 404)
        try:
            p, meta = mesh_mod.peer_mesh(r, oid, self.q1("commit") or None)
        except mesh_mod.MeshError as e:
            return self.err(str(e), 409)
        except Exception as e:  # noqa: BLE001
            log(traceback.format_exc())
            return self.err("对端生成 3D 网格出错：%s" % e, 500)
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-ZGR-Tris", str((meta or {}).get("tris") or 0))
        self.send_header("X-ZGR-Objects", str((meta or {}).get("objects") or 0))
        missing = (meta or {}).get("missing") or []
        self.send_header("X-ZGR-Missing",
                         urllib.parse.quote(",".join(missing), safe=""))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return None

    def mesh_info(self):
        """只查缓存状态，不触发生成"""
        try:
            r = self.app.repo(self.q1("path", ""))
        except RepoError as e:
            return self.err(str(e), 404)
        oid = self.q1("oid", "")
        cached = mesh_mod.is_cached(r, oid)
        meta = mesh_mod.read_meta(r, oid) if cached else {}
        return self.json_out({
            "ok": True, "oid": oid, "cached": cached,
            "solidworks": mesh_mod.solidworks_available(),
            "peer": r.config.get("preview_peer") or "",
            "meta": meta,
        })

    def preview_missing(self):
        """这个版本里还没有缩略图的模型，交给浏览器端去渲染。

        服务端解不了的格式（STEP/STL…）就靠这条清单补齐，
        前端拿到后用内置的 occt / three 出图，再 POST /api/preview/put 回存。
        """
        r = self.app.repo(self.q1("path"))
        svc = thumbs_service()
        size = svc.clamp(self.q1("size", "128"))
        try:
            head = r.head_oid()
            _, files = r.files_of(head)
        except (RepoError, OSError) as e:
            return self.err("读不到项目版本：%s" % e, 404)
        have = svc.cached_set(r, size)          # None = 列不出来，退回逐个 stat
        need = []
        for p, v in files.items():
            if not preview.is_model(p):
                continue
            oid = v.get("oid") or ""
            if not valid_oid(oid) or int(v.get("size") or 0) > (120 << 20):
                continue
            if have is not None:
                if oid in have:
                    continue
            elif svc.cached(r, oid, size):
                continue
            need.append({"path": p, "oid": oid, "size": int(v.get("size") or 0),
                         "name": p.rsplit("/", 1)[-1]})
        return self.json_out({"ok": True, "head": head, "total": len(need),
                              "need": need[:2000]})

    def _render_slot(self):
        """非本机、无令牌的请求：拿一个「同时生成」的名额；拿不到返回 False。

        这一条和上面的每分钟预算不是一回事：预算管「一分钟能要几次」，
        名额管「现在有几个在真的画」。只限次数的话，一百个请求一起排队，
        每个都等着 SolidWorks，线程和 CPU 照样被占死。
        """
        if self.is_local() or self.app.token:
            return True
        return _REMOTE_RENDER.acquire(blocking=False)

    def _release_render_slot(self):
        if self.is_local() or self.app.token:
            return
        try:
            _REMOTE_RENDER.release()
        except ValueError:
            pass

    def preview_out(self):
        """零件缩略图：按 oid（历史版本）或 file（工作区当前文件）出 PNG"""
        size = self.q1("size", "512")
        oid = self.q1("oid", "")
        name = self.q1("name", "")
        rel = self.q1("file", "")
        path = self.q1("path", "")
        repo = self.app.repo(path) if path else None
        svc = thumbs_service()
        if not self.is_local() and not self.app.token and not render_allowed(
                self.client_address[0]):
            return self.err("请求太频繁（这台机器的预览生成有上限），请稍后再试", 429)
        if rel:
            if not repo:
                return self.err("缺少项目路径")
            # 走统一的相对路径校验：`..`、盘符（C:/...）、UNC、符号链接越界一律拒绝。
            # 以前只查 `..`，于是 `file=C:/Windows/…` 或 `file=D:/别的目录/照片.jpg`
            # 会被当成项目内的文件读出去 —— 一个按名字取缩略图的接口变成了读全盘。
            try:
                ap = self.project_file(repo, rel)
            except RepoError as e:
                msg = str(e)
                return self.err(msg, 404 if "不在工作区" in msg else 403)
            if not self._render_slot():
                return self.err("预览正在生成，稍等几秒再打开", 429)
            try:
                blob = svc.from_file(repo, ap, size)
            finally:
                self._release_render_slot()
        else:
            if not valid_oid(oid):
                return self.err("缺少对象号", 400)
            if not repo or not repo.has(oid):
                repo = self.app.repo_with(oid) or repo
            if not repo:
                return self.err("找不到对象", 404)
            cfg = repo.config
            peer = "" if self.q1("nopeer") else (cfg.get("preview_peer") or "")
            if peer:
                try:
                    check_remote_url(peer, resolve=False)   # 配置可能被手改，回源前再挡一次
                except SyncError:
                    peer = ""
            if not self._render_slot():
                return self.err("预览正在生成，稍等几秒再打开", 429)
            try:
                blob = svc.get(repo, oid, name, size, peer=peer,
                               peer_token=cfg.get("preview_peer_token") or "",
                               force=truthy(self.q1("force")),
                               # 网页要一张图，最多陪它等这么久；排队排不上就先回去，
                               # 别让几十个请求把线程全挂住（以前能挂到十几分钟）
                               budget=THUMB_FOREGROUND_WAIT)
            finally:
                self._release_render_slot()
        if not blob:
            if oid and repo and svc.empty(repo, oid):
                return self.err("这个模型里没有可显示的实体（空装配或纯曲面），"
                                "不是图丢了，是确实没东西可画", 404)
            return self.err("没有可用的缩略图", 404)
        # 按对象号取图内容永远不变，可以永久缓存；
        # 按工作区文件名取图会随文件改动而变，绝不能用 immutable（否则改完还是旧图）。
        cache = ("no-cache, max-age=0, must-revalidate" if rel
                 else "public, max-age=31536000, immutable")
        # 缓存里存的是「文件自带的预览流」，SolidWorks 多半放的是 JPEG，
        # Office/DWG 可能是 BMP/WMF。以前一律回 image/png，浏览器照着 PNG 解
        # 一张 JPEG，运气好能猜出来，运气不好就是一片空白 —— 用户看到的是「没图」。
        # 按真实魔数回类型，认不出的（比如 WMF）仍按 PNG 回，行为不变。
        ctype = preview.sniff(blob) or "image/png"
        self._send(200, blob, ctype, {"Cache-Control": cache,
                                      "Content-Disposition": "inline"})

    def browse(self, path):
        path = path or os.path.expanduser("~")
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return self.err("目录不存在：%s" % path)
        dirs = []
        try:
            for n in sorted(os.listdir(path), key=lambda x: x.lower()):
                ap = os.path.join(path, n)
                if os.path.isdir(ap) and not n.startswith("."):
                    dirs.append({"name": n, "path": ap,
                                 "repo": os.path.isfile(os.path.join(ap, META_DIR, "config.json"))})
        except PermissionError:
            return self.err("没有权限读取：%s" % path)
        parent = os.path.dirname(path)
        drives = []
        if os.name == "nt":
            import string
            drives = ["%s:\\" % c for c in string.ascii_uppercase if os.path.isdir("%s:\\" % c)]
        return self.json_out({"ok": True, "path": path,
                              "parent": parent if parent != path else None,
                              "dirs": dirs[:500], "drives": drives})

    # ------------------------------------------------------------------ 业务接口 POST
    # 会动文件系统的接口：非本机访问必须带令牌
    DESTRUCTIVE = {
        "/api/init", "/api/add", "/api/remove", "/api/restore", "/api/restore_file",
        "/api/export",
        "/api/checkout", "/api/ignore", "/api/branch",
        "/api/branch/merge", "/api/branch/delete", "/api/peer/clone",
        "/api/export/step", "/api/bom/export",
        "/api/openfolder", "/api/openfile", "/api/pick",
        "/api/merge/apply", "/api/undo", "/api/preview/clear", "/api/preview/peer",
        "/api/pack", "/api/reveal", "/api/backup/restore", "/api/backup/restore_file",
        "/api/live",
        "/api/author", "/api/auto", "/api/merge/from", "/api/autopair",
        # 打标记是整份 marks.json 的读-改-写：两个窗口同时打会丢一个标记，
        # 而 GC 正是靠标记保护「客户确认版」的，丢了标记就可能被当孤儿清掉
        "/api/mark", "/api/watch", "/api/snapshot",
        "/api/remote/save", "/api/remote/remove", "/api/remote/sync",
        "/api/recovery", "/api/backup/prune", "/api/repair", "/api/gc",
        "/api/cache/prune",
        # 手动同步会 restore 工作区：不加锁时，文件监视器正把「半新半旧」的文件
        # 扫进一个自动提交里，或者 restore 盖掉刚记的那一版——这类错很难事后发现
        "/api/sync",
    }

    # 这些接口不占全局写锁：它们要等人在系统对话框里点「确定」，可能开着几分钟。
    # 以前 /api/pick 在 DESTRUCTIVE 里，于是用户一打开选文件夹的框，
    # 整个服务的写操作全都堵住 —— 后台自动记录、实时同步、另一台机器推过来的版本
    # 一起停摆，而界面上看着只是「有点慢」。
    NO_LOCK = {"/api/pick"}

    # 这几类操作只在「运行服务的这台机器的本机上」允许：它们要么驱动这台机器的
    # 桌面（系统对话框、资源管理器、默认程序打开文件），要么改这台机器的全局配置
    # （新建项目登记、远端存储凭据）。
    # 令牌原本是「让另一台机器也能管理这套服务」的，等于整机管理权 ——
    # 一旦把令牌给了别人（或者它从聊天记录里泄出去），对方就能让这台机器
    # 弹框、打开任意已知路径的文件、把远端存储的密码改掉。真的需要远程做这些，
    # 显式设 ZGR_ALLOW_REMOTE_GUI=1，别默认开着。
    LOCAL_ONLY = {"/api/pick", "/api/openfolder", "/api/openfile", "/api/reveal",
                  "/api/init", "/api/remote/save", "/api/remote/remove",
                  "/api/backup/restore"}
    ALLOW_REMOTE_GUI = (os.environ.get("ZGR_ALLOW_REMOTE_GUI") or "").strip().lower() in (
        "1", "true", "yes", "on")

    def is_local(self) -> bool:
        """请求是不是「这台机器上的人」发的。

        只认 127.0.0.1 不够：有人把界面地址存成 `192.168.1.20:8795` 的书签，
        从那台机器自己的浏览器打开时，连过来的源地址就是**它自己的网卡地址**，
        不是回环地址。那种情况也属于「本机」，否则在他自己桌面上点「选文件夹」
        会被判成远程操作而拒绝。判断依据是「这个源地址是不是本机自己的地址」。
        """
        ip = (self.client_address or ("",))[0]
        if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        return ip.strip("[]").lower() in allowed_hosts()

    def api_post(self, route):
        body = self.body_json()
        path = body.get("path") or ""
        if route in self.DESTRUCTIVE and route not in self.NO_LOCK:
            # 会改 refs/工作区/配置的接口串行执行，避免 restore 与自动记录交错出混合快照
            with self.app.lock:
                return self._api_post(route, body, path)
        return self._api_post(route, body, path)

    def _api_post(self, route, body, path):
        if (route in self.LOCAL_ONLY and not self.is_local()
                and not self.ALLOW_REMOTE_GUI):
            return self.err("这个操作只能在这台机器的本机上做（它要动这台机器的桌面或全局设置）；"
                            "确实需要远程操作，请在服务那边设环境变量 ZGR_ALLOW_REMOTE_GUI=1", 403)
        if route in self.DESTRUCTIVE and not self.is_local() and not self.token_ok():
            return self.err("该操作只允许本机发起；如需在另一台机器上操作，请给服务加 --token 并提交令牌", 403)
        if route == "/api/init":
            target = os.path.abspath(body.get("target") or path)
            r = Repo(target).init(body.get("name"))
            entry = self.app.registry.add(r.root, body.get("name") or r.name)
            if body.get("watch"):
                self.app.start_watch(r, int(body.get("interval") or 30))
            log("新建项目：%s" % r.root)
            # 新项目不用等用户填地址：后台立刻找对端
            threading.Thread(target=self.app.auto_pair_repo, args=(r,),
                             kwargs={"force_scan": True}, daemon=True).start()
            return self.json_out({"ok": True, "project": entry,
                                  **self.app.project_detail(r.root)})
        if route == "/api/demo":
            # 第一次用、列表还是空的时候最容易卡住：给一个能立刻点开看的示例项目。
            # 只建在临时目录里，永不覆盖用户自己的目录。
            from .demo import make_demo
            r = make_demo()
            entry = self.app.registry.add(r.root, r.name)
            log("示例项目：%s" % r.root)
            return self.json_out({"ok": True, "project": entry,
                                  **self.app.project_detail(r.root)})
        if route == "/api/add":
            target = os.path.abspath(body.get("target") or path)
            r = Repo(target)
            if not r.exists:
                return self.err("该目录还不是追光几何项目，请先新建")
            entry = self.app.registry.add(r.root, body.get("name"))
            threading.Thread(target=self.app.auto_pair_repo, args=(r,),
                             kwargs={"force_scan": True}, daemon=True).start()
            return self.json_out({"ok": True, "project": entry})
        if route == "/api/autopair":
            r = self.app.repo(path) if path else None
            if r:
                peer = self.app.auto_pair_repo(r, force_scan=True)
                return self.json_out({"ok": True, "peer": peer,
                                      **self.app.live_status(r)})
            found = self.app.auto_pair_all(force_scan=True)
            return self.json_out({"ok": True, "paired": found})
        if route == "/api/remove":
            # 实时同步线程也要停：以前只停了自动记录和文件通知，同步线程还在后台
            # 每 8 秒拉一次、往这个「已经不在列表里」的项目写配置和对象
            self.app.stop_live(path)
            self.app.stop_watch(path, persist=True)
            self.app.fs_watch.stop(path)          # 不再盯这个目录，句柄要还回去
            self.app.registry.remove(path)
            self.app.forget_project(path)         # 按目录做的缓存也一起丢掉
            return self.json_out({"ok": True})
        if route == "/api/snapshot":
            r = self.app.repo(path)
            if os.path.isfile(os.path.join(r.meta, "PULL_NEEDED")) and not body.get("force"):
                return self.err("对方有新版本还没更新到工作区；请先处理「待更新」再记录，"
                                "否则会把旧内容记成一个回退版本")
            with self.app.lock:
                oid = r.commit(body.get("message") or "", body.get("author"))
            self._clear_pull_flag(r)
            self.app.invalidate_status(r)     # 刚记完，别让界面还拿着旧状态
            st = r.status()
            if oid:
                info = live_mod.describe_commit(r, oid, body.get("parent") or None)
                live_mod.append_event(r, "commit", head=oid,
                                      author=info.get("author"),
                                      message=info.get("message") or "记录了一个新版本",
                                      counts=info.get("counts"),
                                      files=info.get("files"),
                                      total=info.get("total"))
            return self.json_out({"ok": True, "commit": oid, "clean": st["clean"],
                                  "status": st,
                                  "skipped": getattr(r, "last_skipped", [])})
        if route == "/api/checkout":
            r = self.app.repo(path)
            head = r.head_oid()
            if not head:
                return self.err("还没有任何版本")
            st = r.status()
            if not st["clean"] and not body.get("force"):
                return self.err("工作区有未记录改动；先记录一次，或选择强制覆盖")
            res = r.restore(head, backup=True)
            self._clear_pull_flag(r)
            # 工作区刚被整个换掉：不把状态缓存作废，界面接下来这一两秒
            # 拿到的还是「换之前」的 head 和未保存清单
            self.app.invalidate_status(r)
            live_mod.append_event(r, "checkout", head=head,
                                  message="工作区已更新到最新版本",
                                  total=len(res.get("removed") or []),
                                  files=[{"path": p, "st": "D"}
                                         for p in (res.get("removed") or [])][:30])
            return self.json_out({"ok": True, **res})
        if route == "/api/restore":
            r = self.app.repo(path)
            if body.get("dry"):
                # 只算不做：确认框里要把「会覆盖哪些、会删掉哪些」摆出来
                return self.json_out({"ok": True,
                                      "plan": r.restore_plan(body.get("commit"),
                                                             body.get("target"))})
            if body.get("backup", True):
                # 整项目恢复前先把回收站按上限瘦一次：这次恢复本身又要写一份备份，
                # 盘快满的时候先腾地方，比事后报「磁盘空间不足、已回滚」好得多
                try:
                    r.auto_prune_backups()
                except (RepoError, OSError):
                    pass
            res = r.restore(body.get("commit"), body.get("target"),
                            mirror=bool(body.get("mirror", True)),
                            backup=bool(body.get("backup", True)))
            self.app.invalidate_status(r)
            live_mod.append_event(r, "restore", head=res.get("commit"),
                                  message="把文件恢复到历史版本 %s"
                                          % (res.get("commit") or "")[:10],
                                  total=len(res.get("removed") or []),
                                  files=[{"path": p, "st": "D"}
                                         for p in (res.get("removed") or [])][:30])
            return self.json_out({"ok": True, **res})
        if route == "/api/restore_file":
            r = self.app.repo(path)
            res = Handler.restore_file(r, body.get("file") or "", body.get("oid") or "",
                                       backup=bool(body.get("backup", True)))
            self.app.invalidate_status(r)
            return self.json_out({"ok": True, **res})
        if route == "/api/export":
            r = self.app.repo(path)
            target = body.get("target")
            if not target:
                return self.err("请给出导出目录")
            if not truthy(body.get("yes")):
                # 导出不备份：目标目录里的同名文件会被直接盖掉。
                # 不传 yes 就先算清楚「会覆盖几个、新建几个」，让调用方自己确认
                # （网页会把这些数字摆进确认框；直接 curl 的脚本也不会一点就没了）
                return self.json_out({"ok": True, "dry": True,
                                      "plan": r.restore_plan(body.get("commit"),
                                                             target, mirror=False)})
            res = r.restore(body.get("commit"), target, mirror=False, backup=False)
            return self.json_out({"ok": True, **res})
        if route == "/api/export/step":
            # 选中的 SolidWorks 文件批量转 STEP（后台任务，前端轮询进度）
            r = self.app.repo(path)
            dest = (body.get("dest") or "").strip()
            if not dest:
                return self.err("请先选择 STEP 放到哪个文件夹", 400)
            try:
                job = self.app.start_step(r.root, body.get("files") or [], dest)
            except RepoError as e:
                return self.err(str(e), 400)
            except swstep.StepError as e:
                return self.err(str(e), 400)
            log("导出 STEP：%s（%d 个文件 → %s）" % (r.name, job.total, job.dest))
            return self.json_out({"ok": True, "job": job.snapshot()})
        if route == "/api/bom/export":
            # 从一个装配体出物料清单：后台跑（要开 SolidWorks 几分钟），前端轮询
            r = self.app.repo(path)
            try:
                job = self.app.start_bom(
                    r.root, body.get("file") or "", dest=(body.get("dest") or "").strip(),
                    include_children=bool(body.get("include_children", True)),
                    merge_by=str(body.get("merge_by") or "code"))
            except RepoError as e:
                return self.err(str(e), 400)
            except bom_mod.BomError as e:
                return self.err(str(e), 400)
            log("生成 BOM：%s → %s" % (r.name, job.dest))
            return self.json_out({"ok": True, "job": job.snapshot()})
        if route == "/api/filetimes":
            # 文件清单要按「修改日期 / 创建日期」排序时，现取工作区里这些文件的时间
            r = self.app.repo(path)
            files = body.get("files") or []
            out = {}
            for rel in files[:20000]:
                rel = str(rel or "").replace("\\", "/").lstrip("/")
                if not rel or ".." in rel.split("/") or ":" in rel:
                    continue
                p = os.path.join(r.root, *rel.split("/"))
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[rel] = {"m": int(st.st_mtime * 1000), "c": int(st.st_ctime * 1000),
                            "s": int(st.st_size)}
            return self.json_out({"ok": True, "times": out, "root": r.root})
        if route == "/api/mark":
            r = self.app.repo(path)
            return self.json_out({"ok": True, **r.set_mark(
                body.get("commit"), body.get("mark"), body.get("tags"))})
        if route == "/api/ignore":
            r = self.app.repo(path)
            pats = [str(x).strip() for x in (body.get("ignore") or []) if str(x).strip()]
            # .zgr 必须永远忽略，否则镜像恢复会把仓库元数据当普通文件删掉
            for must in (META_DIR, ".git"):
                if must not in pats:
                    pats.insert(0, must)
            pats = pats or list(DEFAULT_IGNORE)
            # 忽略规则是整份列表：两个窗口同时保存「各自以为的完整列表」时，
            # 后写的会把前一份整个盖掉（其中包括别人刚加的排除项）。
            cfg = r.update_config(lambda c: c.update({"ignore": pats}))
            return self.json_out({"ok": True, "ignore": cfg.get("ignore") or pats})
        if route == "/api/watch":
            r = self.app.repo(path)
            if body.get("on"):
                self.app.start_watch(r, int(body.get("interval") or 30))
                return self.json_out({"ok": True, "watching": True})
            self.app.stop_watch(r.root)
            return self.json_out({"ok": True, "watching": False})
        if route == "/api/branch":
            r = self.app.repo(path)
            name = safe_branch(body.get("branch"))
            target = body.get("commit") or ""
            exists = name in r.branches()
            # 切到已经存在的分支时**绝不能**改动它的指针。
            # 以前这里无条件 set_branch(name, 当前HEAD)，等于把目标分支挪到当前提交上，
            # 那条分支原来的提交就从分支上掉下来了（历史没丢，但分支位置被毁）。
            if exists and not target:
                head = r.head_oid(name) or ""
                cur = r.head_oid() or ""
                if head and head != cur:
                    # 切到另一条分支时，文件也要跟着换。
                    # 以前只写 HEAD 不动工作区：界面看着已经切过去了，可磁盘上还是原来
                    # 那条分支的内容；这时候自动记录/「立即记录」一触发，就在对方的分支上
                    # 记了一版「把这些文件改回去」——对方再同步就会把工作区覆盖掉。
                    if not truthy(body.get("force")):
                        try:
                            st = r.status()
                        except RepoError:
                            st = {"clean": False, "counts": {}}
                        plan = r.restore_plan(head)
                        return self.json_out({
                            "ok": True, "dry": True, "branch": name, "head": head,
                            "current": cur, "clean": bool(st.get("clean")),
                            "dirty": sum((st.get("counts") or {}).values()),
                            "plan": {k: plan.get(k) for k in
                                     ("overwrite", "create", "remove", "same")}})
                    with self.app.lock:
                        res = r.restore(head, backup=True)
                        r._write_text("HEAD", name)
                    self._clear_pull_flag(r)
                    self.app.invalidate_status(r)
                    log("切分支：%s → %s（工作区已更新，写 %d 个、删 %d 个）"
                        % (r.name, name, res.get("written") or 0,
                           len(res.get("removed") or [])))
                    return self.json_out({"ok": True, "branch": name, "head": head,
                                          "created": False, "switched": True,
                                          "written": res.get("written") or 0,
                                          "removed": res.get("removed") or []})
                r._write_text("HEAD", name)
                return self.json_out({"ok": True, "branch": name,
                                      "head": head, "created": False})
            if not target:
                target = r.head_oid()
            if target:
                try:
                    r.commit_info(target)          # 分支只能指向提交，不能是 blob/tree
                except RepoError:
                    return self.err("目标不是提交对象：%s" % target[:12])
            r._write_text("HEAD", name)
            if target:
                r.set_branch(name, target)
            else:
                r._write_text(os.path.join("refs", "heads", name), "")
            return self.json_out({"ok": True, "branch": name, "head": target,
                                  "created": not exists})
        if route == "/api/branch/merge":
            # 把一条分支并入当前分支：生成双父提交，分支本身先留着（合并完再删也不迟）
            r = self.app.repo(path)
            name = safe_branch(body.get("branch"))
            if name not in r.branches():
                return self.err("没有这条分支：%s" % name, 404)
            if name == r.branch:
                return self.err("这就是当前所在的分支")
            theirs, ours = r.head_oid(name), r.head_oid()
            if not theirs:
                return self.err("这条分支还没有任何版本")
            if not ours:
                return self.err("当前分支还没有任何版本")
            if theirs == ours or theirs in r.commit_reachable(ours):
                return self.err("分支 %s 已经在当前分支里了，没有需要合并的内容" % name, 409)
            incoming = sorted(r.commit_reachable(theirs) - r.commit_reachable(ours))
            strategy = "smart" if str(body.get("strategy") or "smart").lower() != "manual" else "manual"
            # 同 /api/merge/from：先核对再合并，核对之后本机又记了新版本就得重新核对
            expect = (body.get("expect") or "").strip()
            if body.get("apply") and expect and expect != ours:
                return self.err("核对之后本机又记录了新版本（%s）。请重新点一次「并入」再确认。"
                                % ours[:8], 409)
            # 被并入的那条分支头同样要核对：分支名是不动的，但指针会动
            # （对方随时可能推上来一版）。只查本机这一边，等于核对完的东西
            # 可能被换掉——用户按「会并进 N 个版本/没有冲突」点的确认就落空了。
            expect_theirs = (body.get("expect_theirs") or "").strip()
            if body.get("apply") and expect_theirs and expect_theirs != theirs:
                return self.err("核对之后分支「%s」又前进了（%s）。请重新点一次「并入」再确认。"
                                % (name, theirs[:8]), 409)
            # 只做计划时把「谁动过它」也查出来：合并确认框里要写清是哪个人的哪个零件
            p = merge_mod.plan(r, theirs, smart=(strategy == "smart"),
                               detail=not body.get("apply"))
            if not body.get("apply"):
                return self.json_out({"ok": True, **p, "branch": name,
                                      "versions": len(incoming), "strategy": strategy})
            with self.app.lock:
                res = merge_mod.apply(
                    r, p, choices=body.get("choices") or {},
                    message=body.get("message") or "合并分支 %s（%d 个版本）" % (name, len(incoming)),
                    checkout=bool(body.get("checkout", True)),
                    backup=bool(body.get("backup", True)),
                    strategy=strategy,
                    author=r.config.get("author") or None)
            self.app.invalidate_status(r)      # 合并会把工作区换成合并结果
            log("合并分支：%s ← %s（%d 个版本，%d 个文件，%s）"
                % (r.name, name, len(incoming), res["files"], strategy))
            live_mod.append_event(r, "merge", head=res["commit"], peer=name,
                                  message="把分支 %s 合并进 %s" % (name, r.branch),
                                  total=res["files"],
                                  files=[{"path": d["path"], "st": d.get("pick") or ""}
                                         for d in res.get("decisions", [])][:30])
            return self.json_out({"ok": True, **res, "branch": name,
                                  "versions": len(incoming), "strategy": strategy})
        if route == "/api/branch/delete":
            # 和 git branch -d 一样：只删指针，不删任何提交。
            # 没合并过的分支要先确认（force），避免把只有它才有的版本「藏起来」。
            r = self.app.repo(path)
            name = safe_branch(body.get("branch"))
            if name == r.branch:
                return self.err("不能删除当前所在的分支，先切到别的分支再删")
            if name not in r.branches():
                return self.err("没有这条分支：%s" % name, 404)
            target = r.head_oid(name) or ""
            heads = {b: r.head_oid(b) for b in r.branches() if b != name}
            merged_into = None
            if target:
                for other, oh in heads.items():
                    if not oh:
                        continue
                    if oh == target or target in r.commit_reachable(oh):
                        merged_into = other
                        break
            if target and not merged_into and not body.get("force"):
                return self.err("分支 %s 还有没合并进其它分支的版本，先合并，或者确认后再删"
                                % name, 409)
            r.delete_branch(name)
            log("删除分支：%s（并入 %s）" % (name, merged_into or "无"))
            live_mod.append_event(r, "branch_del", branch=name,
                                  message="删除分支 %s%s"
                                          % (name, "（已并入 %s）" % merged_into if merged_into else ""))
            return self.json_out({"ok": True, "branch": name, "merged_into": merged_into,
                                  "head": target})
        if route == "/api/remote/save":
            r = self.app.repo(path)
            # 注意：body["path"] 是本地项目目录，远端自己的目录走 local_path
            conf = {k: (body.get(k) or "") for k in
                    ("name", "kind", "url", "user", "bucket", "endpoint",
                     "region", "prefix", "project")}
            conf["path"] = (body.get("local_path") or "").strip()
            if body.get("path_style") is False:
                conf["path_style"] = False
            secret = {k: body.get(k) for k in
                      ("password", "access_key", "secret_key", "token")
                      if body.get(k)}
            if not conf["name"].strip():
                return self.err("远端要有个名字")
            remotes_mod.save_remote(r, conf, secret=secret or None)
            return self.json_out({"ok": True, "remotes": remotes_mod.list_remotes(r)})
        if route == "/api/remote/remove":
            r = self.app.repo(path)
            remotes_mod.remove_remote(r, (body.get("name") or "").strip())
            return self.json_out({"ok": True, "remotes": remotes_mod.list_remotes(r)})
        if route == "/api/remote/test":
            r = self.app.repo(path)
            name = (body.get("name") or "").strip()
            return self.json_out({"ok": True,
                                  **remotes_mod.test_remote(r, name)})
        if route == "/api/remote/sync":
            r = self.app.repo(path)
            name = (body.get("name") or "").strip()
            res = remotes_mod.sync_remote(
                r, name, push=bool(body.get("push", True)),
                pull=bool(body.get("pull", True)),
                checkout=bool(body.get("checkout")),
                force=bool(body.get("force")), branch=body.get("branch") or None,
                log=lambda m: log("  " + str(m)))
            live_mod.append_event(
                r, "sync", head=res.get("head"), peer="远端 %s" % name,
                message="远端同步：%s" % "；".join(res.get("messages") or []),
                total=len(res.get("messages") or []))
            return self.json_out({"ok": True, **res})
        if route == "/api/sync":
            r = self.app.repo(path)
            url = (body.get("url") or "").strip()
            share = (body.get("share") or "").strip()
            # 只填共享目录、不填地址是正常用法（「从共享文件夹拉取」那个按钮）：
            # 以前无条件校验 url，空串直接被判成「地址不合法」，共享同步整条不可用。
            if url:
                url = check_remote_url(url)
            if share:
                share = check_share_path(share)
            if not url and not share:
                return self.err("请填写对方地址，或者选一个共享目录")
            self._remember_peer(r, url, body.get("token"))
            res = do_sync(
                r, url, push=bool(body.get("push", True)),
                pull=bool(body.get("pull", True)), token=body.get("token") or None,
                branch=body.get("branch") or None, checkout=bool(body.get("checkout")),
                force=bool(body.get("force")), share=share,
                project=body.get("remote_project") or "",
                log=lambda m: log("  " + str(m)))
            live_mod.append_event(
                r, "sync", head=res.get("head"), peer=url or body.get("share") or "",
                message="手动同步：%s" % "；".join(res.get("messages") or []),
                total=len(res.get("messages") or []))
            return self.json_out({"ok": True, **res})
        if route == "/api/merge/plan":
            r = self.app.repo(path)
            info = self._merge_side(r, body)
            p = merge_mod.plan(r, info["theirs"], smart=bool(body.get("smart")),
                               detail=True)
            if info.get("local_commit"):
                p["theirs_log"] = [{
                    "id": info["theirs"],
                    "message": (r.commit_info(info["theirs"]).get("message") or ""),
                    "time": r.commit_info(info["theirs"]).get("time"),
                    "author": r.commit_info(info["theirs"]).get("author"),
                }]
            else:
                p["theirs_log"] = merge_mod.remote_log(r, info["theirs"], p.get("base"))
            p.update({"branch": info["branch"], "remote_name": info["name"],
                      "theirs_head": info["theirs"]})
            return self.json_out({"ok": True, **p})
        if route == "/api/merge/apply":
            r = self.app.repo(path)
            info = self._merge_side(r, body)
            strategy = "smart" if str(body.get("strategy") or "").lower() == "smart" else "manual"
            p = merge_mod.plan(r, info["theirs"], smart=(strategy == "smart"))
            with self.app.lock:
                res = merge_mod.apply(
                    r, p, choices=body.get("choices") or {},
                    message=body.get("message") or "",
                    checkout=bool(body.get("checkout", True)),
                    backup=bool(body.get("backup", True)),
                    strategy=strategy,
                    author=r.config.get("author") or None)
            self.app.invalidate_status(r)      # 合并会把工作区换成合并结果
            log("合并：%s ← %s（%d 个文件，%s）" % (
                r.name, info["name"], res["files"], strategy))
            live_mod.append_event(r, "merge", head=res["commit"],
                                  peer=info["name"],
                                  message="合并了 %s 的改动" % info["name"],
                                  total=res["files"],
                                  files=[{"path": d["path"], "st": d.get("pick") or ""}
                                         for d in res.get("decisions", [])][:30])
            return self.json_out({"ok": True, **res, "remote_name": info["name"],
                                  "strategy": strategy})
        if route == "/api/merge/from":
            # 把任意一个历史节点当作「对方版本」并进来：可以不切分支就把老版本的内容
            # 合并回当前版本（双父提交，历史全部保留，随时能撤销）
            r = self.app.repo(path)
            target = r.resolve(body.get("commit") or "")
            head = r.head_oid()
            if not head:
                return self.err("本机还没有任何版本")
            if target == head:
                return self.err("这就是当前版本，无需合并")
            strategy = "smart" if str(body.get("strategy") or "").lower() != "manual" else "manual"
            # 界面是「先核对、再合并」两步。这中间本机可能又记了一版（后台自动记录、
            # 对方推过来、另一个窗口记录），那核对时看到的结果就不是真正要合的东西了。
            # 界面把核对时的 ours 带回来，对不上就让它重新核对。
            expect = (body.get("expect") or "").strip()
            if body.get("apply") and expect and expect != head:
                return self.err("核对之后本机又记录了新版本（%s）。"
                                "为了让你看到的就是真正会合的东西，请重新点一次「与这一版合并」。"
                                % head[:8], 409)
            p = merge_mod.plan(r, target, smart=(strategy == "smart"),
                               detail=not body.get("apply"))
            if not body.get("apply"):
                return self.json_out({"ok": True, **p, "remote_name": "历史版本 %s" % target[:10]})
            with self.app.lock:
                res = merge_mod.apply(
                    r, p, choices=body.get("choices") or {},
                    message=body.get("message") or "合并历史版本 %s" % target[:10],
                    checkout=bool(body.get("checkout", True)),
                    backup=True, strategy=strategy,
                    author=r.config.get("author") or None)
            live_mod.append_event(r, "merge", head=res["commit"],
                                  message="把历史版本 %s 合并进来" % target[:10],
                                  total=res["files"],
                                  files=[{"path": d["path"], "st": d.get("pick") or ""}
                                         for d in res.get("decisions", [])][:30])
            return self.json_out({"ok": True, **res, "remote_name": "历史版本 %s" % target[:10],
                                  "strategy": strategy})
        if route == "/api/undo":
            r = self.app.repo(path)
            res = merge_mod.undo(r, body.get("commit"),
                                 checkout=bool(body.get("checkout", True)),
                                 backup=bool(body.get("backup", True)),
                                 author=r.config.get("author") or None,
                                 message=body.get("message") or "")
            self.app.invalidate_status(r)      # 工作区被换过，别再拿旧状态
            log("回溯：%s → %s" % (r.name, res["commit"][:12]))
            live_mod.append_event(r, "undo", head=res["commit"],
                                  target=res.get("target"),
                                  message="回溯到历史版本 %s（生成新版本）"
                                          % (res.get("target") or "")[:10])
            return self.json_out({"ok": True, **res})
        if route == "/api/preview/put":
            # 浏览器端渲染好的缩略图回存：POST 要求 application/json（CSRF 防护），
            # 所以 PNG 走 base64 而不是裸字节流
            r = self.app.repo(path)
            oid = (body.get("oid") or "").strip()
            raw = body.get("png") or ""
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            try:
                data = base64.b64decode(raw, validate=False)
            except Exception:
                return self.err("图片数据不是合法的 base64", 400)
            if not thumbs_service().put(r, oid, data, body.get("size") or 512):
                return self.err("缩略图存不进去（对象号或图片格式不对）", 400)
            return self.json_out({"ok": True, "oid": oid,
                                  "size": thumbs_service().clamp(body.get("size") or 512)})
        if route == "/api/mesh/put":
            # 无头浏览器（web/meshgen.html）用 occt/three 解析 STEP/IGES/STL 之后回存网格。
            # 走 JSON + base64：POST 要求 application/json 是现成的 CSRF 防护。
            r = self.app.repo(path)
            oid = (body.get("oid") or "").strip()
            if not valid_oid(oid) or not r.has(oid):
                return self.err("对象号不对", 400)
            raw = body.get("zgm") or ""
            try:
                data = base64.b64decode(raw, validate=False)
            except Exception:
                return self.err("网格数据不是合法的 base64", 400)
            try:
                meta = mesh_mod.put_zgm(r, oid, data,
                                        source="headless:" + (body.get("ua") or "")[:40],
                                        rel=(body.get("file") or "")[:200])
            except mesh_mod.MeshError as e:
                return self.err(str(e), 400)
            thumbs_service().forget(r, oid)
            return self.json_out({"ok": True, "oid": oid, "verts": meta["verts"],
                                  "tris": meta["tris"]})
        if route == "/api/preview/putfile":
            # 「未保存」网格里的工作区文件没有对象号，单独按 (路径, mtime, 大小) 回存
            r = self.app.repo(path)
            # 只查 `..` 是不够的：`C:/Windows/win.ini` 这种带盘符的路径，os.path.join
            # 会把前面的项目目录整个丢掉，于是这个接口变成「任意路径在不在 + 精确 mtime」
            # 的预言机（还能在预览缓存里生成垃圾）。必须走统一的项目内路径校验。
            try:
                ap = self.project_file(r, body.get("file") or "")
            except RepoError as e:
                msg = str(e)
                return self.err(msg, 404 if "不在工作区" in msg else 403)
            rel = os.path.relpath(ap, os.path.realpath(r.root)).replace(os.sep, "/")
            raw = body.get("png") or ""
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            try:
                data = base64.b64decode(raw, validate=False)
            except Exception:
                return self.err("图片数据不是合法的 base64", 400)
            v = body.get("v")
            ok = thumbs_service().put_file(r, ap, data, body.get("size") or 512,
                                           expect_mtime=v if v not in (None, "") else None)
            if not ok:
                return self.err("缩略图存不进去（文件已变或图片格式不对）", 400)
            return self.json_out({"ok": True, "file": rel})
        if route == "/api/preview/peer":
            r = self.app.repo(path)
            peer = check_remote_url((body.get("url") or "").strip())
            tok = body.get("token") or ""

            def _set(cfg):
                cfg["preview_peer"] = peer
                if tok:
                    cfg["preview_peer_token"] = tok
            cfg = r.update_config(_set)
            return self.json_out({"ok": True, "peer": cfg.get("preview_peer") or peer})
        if route == "/api/preview/clear":
            import shutil as _sh
            r = self.app.repo(path)
            d = os.path.join(r.meta, "previews")
            if os.path.isdir(d):
                _sh.rmtree(d, ignore_errors=True)
            return self.json_out({"ok": True})
        if route == "/api/openfolder":
            return self._open_folder(body, path)
        if route == "/api/openfile":
            return self._open_file(body, path)
        if route == "/api/reveal":
            return self._open_folder(body, path, select=True)
        if route == "/api/peer/clone":
            # 把对方机器上的一个项目整个拉过来（后台任务，前端轮询进度）
            url = check_remote_url((body.get("url") or "").strip())
            project = (body.get("project") or "").strip()
            dest = (body.get("dest") or "").strip()
            if not project:
                return self.err("没说要拉哪个项目", 400)
            if not dest:
                return self.err("请先选择保存到哪个文件夹", 400)
            try:
                dest = peerjoin.check_dest(dest)
            except peerjoin.JoinError as e:
                return self.err(str(e), 400)
            job = self.app.start_clone(url, project, dest, token=body.get("token") or "")
            log("开始克隆：%s（%s ← %s）" % (project, dest, url))
            return self.json_out({"ok": True, "job": job.snapshot()})
        if route == "/api/pick":
            # 用 Windows 自带的对话框选文件夹/文件（不是自己画的目录树）：
            # 桌面、下载、U 盘、网络位置、快速访问、新建文件夹这些系统里都有
            ok, why = nativepick.desktop_ready()
            if not ok:
                return self.err(why, 409)
            kind = str(body.get("kind") or "folder").lower()
            if kind not in ("folder", "files"):
                return self.err("kind 只能是 folder 或 files", 400)
            try:
                paths = nativepick.pick(
                    kind=kind,
                    initial=(body.get("initial") or "").strip(),
                    multiple=bool(body.get("multiple")),
                    title=(body.get("title") or "").strip())
            except nativepick.PickBusy as e:
                return self.err(str(e), 429)
            except nativepick.PickUnavailable as e:
                return self.err(str(e), 409)
            except Exception as e:                            # noqa: BLE001
                log("选择对话框失败：%s" % e)
                return self.err("系统对话框打不开：%s" % e, 500)
            return self.json_out({"ok": True, "paths": paths, "canceled": not paths,
                                  "kind": kind})
        if route == "/api/pack":
            r = self.app.repo(path)
            res = live_mod.pack(r, commit=body.get("commit") or None,
                                worktree=bool(body.get("worktree")),
                                prefix=(body.get("prefix") or "").strip())
            live_mod.append_event(r, "pack", message="打包了 %d 个文件（%.1f MB）"
                                  % (res["files"], res["size"] / 1048576.0))
            res["download"] = "/api/pack/get?" + urllib.parse.urlencode(
                {"path": r.root, "name": res["name"]})
            log("打包：%s → %s（%d 个文件，%.1f MB）" % (
                r.name, res["name"], res["files"], res["size"] / 1048576.0))
            return self.json_out({"ok": True, **res})
        if route == "/api/backup/restore":
            r = self.app.repo(path)
            res = live_mod.restore_backup(r, body.get("name") or "")
            live_mod.append_event(r, "trash_restore", message="从回收站复原 %d 个文件"
                                  % res["count"],
                                  total=res["count"],
                                  files=[{"path": p, "st": "R"}
                                         for p in res["written"]][:30])
            log("回收站复原：%s ← %s（%d 个文件）"
                % (r.name, res["name"], res["count"]))
            return self.json_out({"ok": True, **res})
        if route == "/api/backup/restore_file":
            # 只从一批备份里挑一个文件放回：整批复原会把当时被覆盖的所有文件
            # 一起盖回去，而用户往往只丢了那一个零件
            r = self.app.repo(path)
            res = live_mod.restore_backup_file(
                r, body.get("name") or "", body.get("file") or "",
                backup=bool(body.get("backup", True)))
            self.app.invalidate_status(r)
            live_mod.append_event(r, "trash_restore", head=None,
                                  message="从回收站复原单个文件：%s" % res["file"],
                                  total=1, files=[{"path": res["file"], "st": "R"}])
            log("回收站复原单文件：%s ← %s / %s"
                % (r.name, res["name"], res["file"]))
            return self.json_out({"ok": True, **res})
        if route == "/api/repair":
            # 从对方补缺的/坏的对象。只写对象库，不动分支头、不动工作区
            from .sync import repair as do_repair
            r = self.app.repo(path)
            url = (body.get("url") or "").strip()
            share = (body.get("share") or "").strip()
            if not url and not share:
                live = (r.config.get("live") or {})
                url = (live.get("url") or "").strip()
            if not url and not share:
                return self.err("还没配对方地址：先在「双机同步」里连上另一台电脑", 400)
            if url:
                # 请求里的地址由调用方给，服务端会主动去连——必须和别的同步接口
                # 一样先过地址校验，否则这里就是一个「让服务器去访问任意内网地址」
                # 的口子（还能把对方响应片段回显在错误里）
                url = check_remote_url(url)
            if share:
                # 共享目录同理：不能由请求随手指定一个可写目录就把仓库搬过去
                share = check_share_path(share)
            res = do_repair(r, url, token=body.get("token") or None,
                            share=share, project=body.get("project") or "",
                            branch=body.get("branch") or None,
                            deep=not bool(body.get("quick")), log=log)
            log("对象修复：%s 补回 %d 个，移除坏对象 %d 个，结果 %s"
                % (r.name, res["fetched"], res["removed_bad"],
                   "通过" if res["ok"] else "仍有问题"))
            return self.json_out({"ok": True, **res})
        if route == "/api/cache/prune":
            # 把预览图/网格删到不超过上限（默认按项目配置）；dry 只报不删
            r = self.app.repo(path)
            cfg = r.config
            dry = bool(body.get("dry"))
            pmb = body.get("preview_mb")
            mmb = body.get("mesh_mb")
            res = cache_mod.prune_repo(
                r, dry=dry,
                preview_mb=int(pmb) if pmb not in (None, "") else None,
                mesh_mb=int(mmb) if mmb not in (None, "") else None)
            pv = res.get("previews") or {}
            ms = res.get("meshes") or {}
            # 「还剩多少、上限多少、还超不超」一起回给界面：用户点完要能一眼看出
            # 「是删过了」还是「删不动（都是刚生成的）」还是「本来就没超」
            def cap_mb(v, key, default=2048):
                if v not in (None, ""):
                    try:
                        return max(0, int(v))
                    except (TypeError, ValueError):
                        return default
                try:
                    return max(0, int(cfg.get(key, default) or 0))
                except (TypeError, ValueError):
                    return default
            pv_cap = cap_mb(pmb, "cache_max_mb") * 1048576
            ms_cap = cap_mb(mmb, "mesh_max_mb") * 1048576
            if not dry and (pv.get("removed") or ms.get("removed")):
                log("清理缓存：%s 预览图 %d 个（%.1f MB）、网格 %d 个（%.1f MB）"
                    % (r.name, len(pv.get("removed") or []), (pv.get("bytes") or 0) / 1e6,
                       len(ms.get("removed") or []), (ms.get("bytes") or 0) / 1e6))
            return self.json_out({
                "ok": True, "dry": dry,
                "previews": {"removed": len(pv.get("removed") or []),
                             "bytes": pv.get("bytes") or 0,
                             "total": pv.get("total") or 0,
                             "cap": pv_cap,
                             "over": bool(pv_cap) and (pv.get("total") or 0) > pv_cap},
                "meshes": {"removed": len(ms.get("removed") or []),
                           "bytes": ms.get("bytes") or 0,
                           "total": ms.get("total") or 0,
                           "cap": ms_cap,
                           "over": bool(ms_cap) and (ms.get("total") or 0) > ms_cap},
                "auto": cfg.get("cache_auto", True) is not False})
        if route == "/api/backup/prune":
            r = self.app.repo(path)
            keep = int(body.get("keep") or 20)
            max_mb = body.get("max_mb")
            # 同 /api/gc：不写 yes 只演练，绝不默认删备份
            dry = not truthy(body.get("yes"))
            res = r.prune_backups(
                keep=keep, dry=dry,
                max_bytes=(int(max_mb) * 1024 * 1024) if max_mb else None,
                min_keep=int(body.get("min_keep") or 10))
            if not dry and res["removed"]:
                log("清理旧备份：保留 %d 批，删掉 %d 批（释放 %.1f MB）"
                    % (res["kept"], len(res["removed"]), res["bytes"] / 1e6))
            return self.json_out({"ok": True, **res})
        if route == "/api/gc":
            r = self.app.repo(path)
            # 界面永远不会传 0；接口上强制留一天以上，免得有人在同步途中把它清空
            keep = max(1, int(body.get("keep_days") if body.get("keep_days") is not None else 7))
            # 删除是不可逆的：不写 yes 一律按演练处理。
            # （以前空 body 的 POST 就真删，脚本或误点没有第二次机会）
            res = r.gc(dry=not truthy(body.get("yes")), keep_days=keep)
            # 对象被清掉了，它导出的网格就是死文件：顺手删掉（几十 MB 一个）
            try:
                oids = cache_mod.mesh_oids(r)
                gone = cache_mod.orphans(r, {o for o in oids if r.has(o)})
                freed = 0
                for fp in gone:
                    if body.get("dry"):
                        continue
                    try:
                        freed += os.path.getsize(fp)
                        os.remove(fp)
                    except OSError:
                        pass
                res["mesh_files"] = len(gone)
                res["mesh_bytes"] = freed
            except (RepoError, OSError):
                pass
            log("回收孤儿对象：%s %d 个，释放 %.1f MB（体检通过才动手）"
                % (r.name, res["removed"], res["bytes"] / 1e6))
            return self.json_out({"ok": True, "plan": res})
        if route == "/api/recovery":
            # 收尾上次被打断的恢复：again=再恢复一遍，backup=把备份拷回去
            r = self.app.repo(path)
            mode = body.get("mode") or "again"
            if body.get("dismiss"):
                r.clear_pending_restore()
                return self.json_out({"ok": True, "dismissed": True})
            res = r.finish_pending_restore(mode)
            live_mod.append_event(r, "recover", message="收尾上次中断的恢复（%s）" % mode)
            log("收尾中断的恢复：%s %s" % (r.name, mode))
            return self.json_out({"ok": True, **res})
        if route == "/api/live":
            r = self.app.repo(path)
            if not body.get("enabled"):
                was = self.app.stop_live(r.root, manual=True)
                return self.json_out({"ok": True, **self.app.live_status(r),
                                      "stopped": was})
            url = (body.get("url") or "").strip()
            if not url:
                return self.err("请填写对方地址")
            before = self.app.live_status(r)
            self.app.start_live(
                r, url, token=body.get("token") or "",
                interval=int(body.get("interval") or 15),
                auto_pull=bool(body.get("auto_pull", True)),
                auto_push=bool(body.get("auto_push", True)),
                auto_merge=bool(body.get("auto_merge", True)),
                checkout=bool(body.get("checkout", True)),
                auto_watch=bool(body.get("auto_watch", False)),
                auto=body.get("auto", True),
                mode=body.get("mode") or live_mod.MODE_SPLIT)
            log("实时同步：%s ← %s（每 %s 秒，%s）"
                % (r.name, url, body.get("interval") or 15,
                   "一人一条线" if str(body.get("mode")).lower() == live_mod.MODE_SPLIT
                   else "共用一条线"))
            return self.json_out({"ok": True, **self.app.live_status(r),
                                  "before": before})
        if route == "/api/author":
            r = self.app.repo(path)
            author = (body.get("author") or "").strip()[:60]
            name = author or os.environ.get("USERNAME", "user")
            cfg = r.update_config(lambda c: c.update({"author": name}))
            return self.json_out({"ok": True, "author": cfg.get("author") or name})
        if route == "/api/auto":
            r = self.app.repo(path)
            if body.get("auto_checkout") is not None:
                val = bool(body.get("auto_checkout"))
                cfg = r.update_config(lambda c: c.update({"auto_checkout": val}))
            else:
                cfg = r.config
            return self.json_out({"ok": True,
                                  "auto_checkout": bool(cfg.get("auto_checkout", True))})
        self.err("未知接口", 404)

    # 「打开」只放行**已知的文档/图纸**，不再用黑名单。
    # 黑名单永远漏：`.scf` 只要一行 `Command=calc.exe` 就会被 ShellExecute 直接执行，
    # `.py`/`.pyw` 在装了 Python 的机器上同理；漏一个就等于给了对方
    # 「推一个文件进项目 + 用户点一下 = 执行任意代码」。白名单漏了只会打不开，
    # 不会执行——两种错法差得很远。
    OPENABLE_EXT = {
        ".sldprt", ".sldasm", ".slddrw", ".dwg", ".dxf", ".step", ".stp",
        ".iges", ".igs", ".ipt", ".iam", ".prt", ".catpart", ".3mf", ".stl",
        ".obj", ".pdf", ".txt", ".md", ".csv", ".log", ".json", ".xml",
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip",
    }

    @staticmethod
    def project_file(repo: Repo, rel: str) -> str:
        """把项目内相对路径解析成绝对路径，并保证它没有越界、确实存在"""
        rel = (rel or "").strip().replace("\\", "/").lstrip("/")
        parts = rel.split("/")
        if not rel or any(p in ("", ".", "..") or ":" in p for p in parts):
            raise RepoError("缺少文件路径")
        root = os.path.realpath(repo.root)
        target = os.path.realpath(os.path.join(root, *parts))
        try:
            if os.path.commonpath([root, target]) != root:
                raise RepoError("文件路径越界")
        except ValueError:
            raise RepoError("文件路径越界") from None
        if not os.path.isfile(target):
            raise RepoError("文件不在工作区里（可能已被删除或还没同步）")
        return target

    def _open_file(self, body, path):
        """用本机默认程序打开工作区里的文件（SolidWorks、CAD、记事本…）"""
        try:
            r = self.app.repo(body.get("path") or path)
            target = Handler.project_file(r, body.get("file") or "")
        except RepoError as e:
            msg = str(e)
            return self.err(msg, 404 if "不在工作区" in msg else 400)
        # Windows 会吃掉结尾的点和空格（`x.exe.` 就是 `x.exe`），先把名字规整好再判
        name = os.path.basename(target).rstrip(" .")
        ext = os.path.splitext(name)[1].lower()
        if ext not in Handler.OPENABLE_EXT:
            return self.err("这类文件不直接打开（防止同步进来一个可执行文件就运行），"
                            "请在资源管理器里查看", 403)
        try:
            if os.name == "nt":
                os.startfile(target)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])  # noqa: S603,S607
        except OSError as e:
            return self.err("打开失败：%s" % e)
        return self.json_out({"ok": True, "target": target})

    def _open_folder(self, body, path, select=False):
        """打开项目目录；select=True 时在资源管理器里选中某个文件"""
        rel = (body.get("file") or "").strip().replace("\\", "/").lstrip("/")
        target = os.path.abspath(body.get("target") or path or "")
        if rel:
            try:
                r = self.app.repo(body.get("path") or path)
            except RepoError as e:
                return self.err(str(e), 404)
            if ".." in rel.split("/"):
                return self.err("非法路径", 403)
            target = os.path.join(r.root, rel.replace("/", os.sep))
            if not os.path.exists(target):
                return self.err("文件不在工作区里（可能已被删除或还没拉取）：%s" % rel)
            if os.name == "nt":
                import subprocess
                subprocess.Popen(["explorer", "/select," + target])  # noqa: S603,S607
            else:
                os.startfile(os.path.dirname(target))  # noqa: S606
            return self.json_out({"ok": True, "target": target, "selected": True})
        if not os.path.isdir(target):
            return self.err("目录不存在")
        if os.name == "nt":
            os.startfile(target)  # noqa: S606
        return self.json_out({"ok": True, "target": target})

    # ---- 时间线：每个版本改了多少个文件
    def _attach_change_counts(self, repo: Repo, commits):
        """每条提交「改了几个文件」：只比树、不展开整棵树（见 tree_diff_counts）

        结果按（项目, 提交号）缓存：提交对象不可变，同一个版本和它父版本的差别
        永远是同一个数。三千个零件的项目、一屏 400 版，每次刷新都重算就是半秒；
        缓存之后只有新记的那几版要算。
        """
        root = getattr(repo, "root", "")
        # app 为空时（单测直接调这个方法）就老老实实算，不要缓存
        app = getattr(self, "app", None)
        trees = {c["id"]: c.get("tree") for c in commits}
        for c in commits:
            if not c.get("tree"):
                continue                      # 提交对象残缺：宁可不显示改动数，也别报错
            key = (root, c["id"])
            hit = app.changes_cache_get(key) if app is not None else None
            if hit is not None:
                c["changes"] = dict(hit)
                continue
            parent = (c.get("parents") or [None])[0]
            if parent and parent not in trees:
                try:
                    trees[parent] = repo.commit_info(parent).get("tree")
                except RepoError:
                    continue
            try:
                added, modified, deleted = repo.tree_diff_counts(
                    trees.get(parent) if parent else None, c.get("tree"))
            except RepoError:
                continue
            info = {"added": added, "modified": modified, "deleted": deleted,
                    "total": added + modified + deleted}
            c["changes"] = dict(info)
            if app is not None:
                app.changes_cache_put(key, info)

    # ---- 单个文件（零件）的历史
    @staticmethod
    def _tree_entry(repo: Repo, tree_oid: str, file: str):
        """只沿目录往下找目标文件（实现在 Repo.tree_entry，合并那边也要用）"""
        return repo.tree_entry(tree_oid, file)

    @staticmethod
    def file_history(repo: Repo, file: str, limit=600, with_work=True):
        file = (file or "").replace("\\", "/").lstrip("/")
        if not file or any(p in ("", ".", "..") or ":" in p for p in file.split("/")):
            raise RepoError("缺少文件路径")
        entries = []
        # 多取一条专门用来判断有没有被截断：正好 limit 条时不能误报
        commits = repo.log(limit + 1)
        truncated = len(commits) > limit
        if truncated:
            commits = commits[:limit]
        for idx, c in enumerate(commits):
            tree = c.get("tree")
            try:
                cur = Handler._tree_entry(repo, tree, file) if tree else None
            except (RepoError, OSError):
                cur = None
            # log() 沿第一父提交往下走，所以「父提交」就是列表里的下一个
            old = None
            if idx + 1 < len(commits):
                try:
                    old = Handler._tree_entry(repo, commits[idx + 1]["tree"], file)
                except (RepoError, OSError):
                    old = None
            else:
                parent = (c.get("parents") or [None])[0]
                if parent:
                    try:
                        old = Handler._tree_entry(repo, repo.tree_of(parent), file)
                    except (RepoError, OSError):
                        old = None
            cur_oid = (cur or {}).get("oid")
            old_oid = (old or {}).get("oid")
            if cur_oid == old_oid:
                continue
            st = "A" if not old_oid else ("D" if not cur_oid else "M")
            entries.append({
                "commit": c["id"], "time": c.get("time"), "message": c.get("message"),
                "author": c.get("author"), "st": st,
                "oid": cur_oid or "", "old_oid": old_oid or "",
                "size": int((cur or {}).get("size") or 0),
                "old_size": int((old or {}).get("size") or 0),
            })
        ap = os.path.join(repo.root, file.replace("/", os.sep))
        exists = os.path.isfile(ap)
        work = {"exists": exists, "changed": False,
                "size": os.path.getsize(ap) if exists else 0}
        if with_work and exists:
            st = repo.status()
            work["changed"] = file in st["modified"] or file in st["added"]
        return {"file": file, "entries": entries, "work": work,
                "truncated": truncated, "limit": limit}

    @staticmethod
    def restore_file(repo: Repo, file: str, oid: str, backup=True):
        """只把工作区里的这一个文件写回指定版本，不生成新版本、不动其它文件"""
        file = (file or "").replace("\\", "/").lstrip("/")
        if not file or any(p in ("", ".", "..") or ":" in p for p in file.split("/")):
            raise RepoError("缺少文件路径")
        if not valid_oid(oid):
            raise RepoError("版本对象号不合法")
        if not repo.has(oid):
            raise RepoError("这一版的文件内容不在本机，先同步后再恢复")
        root = os.path.realpath(repo.root)
        dst = os.path.realpath(os.path.join(root, file.replace("/", os.sep)))
        try:
            if os.path.commonpath([root, dst]) != root:
                raise RepoError("文件路径越界")
        except ValueError:
            raise RepoError("文件路径越界") from None

        # 上面那段是安全校验，必须用没加前缀的路径算；
        # 真正碰磁盘的这几步再换成长路径前缀版本，长路径才写得动（也能备份到）。
        dstp = fs_path(dst)
        backup_path = ""
        if backup and os.path.isfile(dstp):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bdir = repo._p("backup", "单文件-" + stamp)
            n = 0
            while os.path.exists(bdir):       # 同一秒内恢复两次也不互相覆盖
                n += 1
                bdir = repo._p("backup", "单文件-%s-%d" % (stamp, n))
            backup_path = os.path.join(bdir, file.replace("/", os.sep))
            bdst = fs_path(backup_path)
            try:
                os.makedirs(os.path.dirname(bdst), exist_ok=True)
                shutil.copy2(dstp, bdst)
            except OSError as e:
                raise RepoError("备份现有文件失败，未做改动：%s" % e) from None
        try:
            os.makedirs(os.path.dirname(dstp), exist_ok=True)
            repo.blob_to_file(oid, dstp)
        except RepoError:
            raise
        except OSError as e:
            raise RepoError("写入失败（文件可能正被 SolidWorks 等程序占用）：%s" % e) from None
        try:
            size = os.path.getsize(dstp)
        except OSError:
            size = 0
        return {"file": file, "size": size,
                "backup": os.path.relpath(backup_path, root).replace("\\", "/")
                          if backup_path else ""}

    # ---- 合并辅助
    def _remember_peer(self, repo: Repo, url: str, token=None):
        """把对端 HTTP 地址记成缩略图回源地址（没装的机器靠它看图）

        读-改-写要在锁里：并发请求（实时同步、自动配对、手动同步）同时改配置时，
        后写的会把别人刚改的那几项整份盖掉。
        """
        if not url or not url.startswith("http"):
            return
        try:
            # 注意是 app 的锁：_remember_peer 是 Handler 的方法，Handler 自己没有锁
            with self.app.lock:                   # 同一个 App 里的配置写串行
                def _set(cfg):
                    if cfg.get("preview_peer") == url and (
                            not token or cfg.get("preview_peer_token") == token):
                        return             # 没变化：update_config 不会写盘
                    cfg["preview_peer"] = url
                    if token:
                        cfg["preview_peer_token"] = token
                repo.update_config(_set)
        except (OSError, RepoError):
            pass

    def _prepare_remote(self, repo: Repo, body: dict):
        """连上对方，把缺的对象先取回来，返回对方 HEAD"""
        url = (body.get("url") or "").strip()
        share = (body.get("share") or "").strip()
        token = body.get("token") or None
        if url:
            check_remote_url(url)
        if share:
            # 共享目录也是「服务端会去读写的路径」，和 /api/sync 同一道校验
            share = check_share_path(share)
        be = make_backend(url=url, share=share,
                          project=body.get("remote_project") or repo.name, token=token)
        refs = be.refs()
        self._remember_peer(repo, url, token)
        branches = refs.get("branches") or {}
        branch = body.get("branch") or repo.branch
        if branches and branch not in branches:
            branch = refs.get("default") or next(iter(branches))
        rhead = branches.get(branch)
        if not rhead:
            raise SyncError("对方分支 %s 还没有任何版本" % branch)
        need = [o for o in be.manifest(branch, repo.head_oid(branch)) if not repo.has(o)]
        if need:
            log("合并前先取回 %d 个对象" % len(need))
            be.fetch(repo, need, log=lambda m: log("  " + str(m)))
        verify_complete(repo, rhead)
        return {"theirs": rhead, "branch": branch,
                "name": refs.get("name") or url or share}

    def _merge_side(self, repo: Repo, body: dict):
        """合并对象可以是远端，也可以任意一个历史提交（body.from_commit）"""
        target = (body.get("from_commit") or "").strip()
        if target:
            oid = repo.resolve(target)
            return {"theirs": oid, "branch": repo.branch,
                    "name": "历史版本 %s" % oid[:10], "local_commit": True}
        return self._prepare_remote(repo, body)

    # ---- 杂项
    @staticmethod
    def _clear_pull_flag(repo: Repo):
        try:
            os.remove(os.path.join(repo.meta, "PULL_NEEDED"))
        except OSError:
            pass

    # ---- 静默默认日志
    def log_message(self, fmt, *args):
        return


_MUTEX_HANDLE = None


def _single_instance_mutex():
    """占住一个命名互斥量，让安装包/卸载器知道「服务正在跑」。

    返回值故意存成全局：进程一退出句柄就释放，不需要手动清理。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = k32.CreateMutexW(None, False, "ZhuiguangGeometryRunning")
    except Exception:
        _MUTEX_HANDLE = None
    return _MUTEX_HANDLE


def serve(port=8795, host="0.0.0.0", token=None, project=None, registry=None,
          open_browser=False):
    _single_instance_mutex()
    reg = registry or Registry()
    app = App(reg, token=token, default_project=project)
    handler = type("BoundHandler", (Handler,), {"app": app})

    class Server(ThreadingHTTPServer):
        # Windows 上 SO_REUSEADDR 会允许两个进程绑同一端口，必须关掉才能发现占用
        allow_reuse_address = os.name != "nt"
        # socketserver 默认只让 5 个连接排队等 accept。浏览器打开项目会一口气
        # 发几十个缩略图请求，队列一满就只能等 Windows 的 SYN 重传（实测建连 p95 0.5 秒，
        # 请求本身只要 2 毫秒）。放宽到 128，抖动就没了。
        request_queue_size = 128

    try:
        httpd = Server((host, port), handler)
    except OSError as e:
        log("端口 %d 启动失败：%s" % (port, e))
        log("如果追光几何已经在运行，直接访问 http://127.0.0.1:%d 即可。" % port)
        if open_browser:
            try:
                import webbrowser
                webbrowser.open("http://127.0.0.1:%d" % port)
            except Exception:
                pass
        return
    httpd.daemon_threads = True
    app.port = port
    app.resume_watches()
    app.resume_lives()
    app.start_background()
    urls = local_urls(port)
    log("%s 服务已启动（pid %d）" % (APP_NAME, os.getpid()))
    for u in urls:
        log("  本机/局域网地址：%s" % u)
    if token:
        log("  已启用访问令牌（同步与接口都需要 X-ZGR-Token）")
    else:
        loopback = host in ("127.0.0.1", "localhost", "::1")
        log("  未启用令牌：局域网里这台机器以外的任何人都能读写这些项目")
        if not loopback:
            log("  ⚠ 现在监听 %s，整个局域网都能连上。" % host)
            log("     只想自己用：zgr serve --host 127.0.0.1")
            log("     要和另一台电脑同步：加 --token 一个口令，两台填同一个")
    log("  网页界面：%s" % urls[0])
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(urls[0])
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("正在退出…")
    finally:
        app.stop_background()
        for w in list(app.watchers.values()):
            w.stop()
        for w in list(app.lives.values()):
            w.stop()
        httpd.server_close()
