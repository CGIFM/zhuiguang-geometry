"""追光几何 · 内网双机同步

两种后端，协议语义完全一致（对象按 oid 传输、只允许快进）：

1. HTTP（对方机器开着服务）
   GET  /api/sync/refs                      → {name, branches:{...}, default}
   POST /api/sync/manifest {have, branch}   → {head, objects:[oid...]}
   GET  /api/sync/object/<oid>              → 对象的压缩字节（流式）
   POST /api/sync/objects {objects:{}}      → 批量写入（小对象）
   POST /api/sync/object/<oid>              → 单个对象原始字节（大对象）
   POST /api/sync/refs {branch, old, new}   → 仅允许快进

2. 共享文件夹（UNC / 网盘同步目录，对方不用开机跑服务）
   <共享目录>/<项目名>/.zgr/objects/...     与本地仓库同构的「裸仓库」
   <共享目录>/<项目名>/.zgr/refs/heads/...  分支指向
   直接按文件复制对象，天然不需要 base64、不进内存。

安全模型：局域网内默认信任；配 token 后所有 /api/sync/* 都要带 X-ZGR-Token。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request

from .store import Repo, RepoError

# 单个对象超过这个大小就走原始字节流（避免 base64 撑爆内存/请求体上限）
RAW_THRESHOLD = 2 << 20
# 一次 JSON 批量请求的原始字节上限（base64 后约 1.33 倍）
BATCH_BYTES = 8 << 20
CHUNK = 1 << 20


class SyncError(Exception):
    pass


# --------------------------------------------------------------------- 工具
def reachable(repo: Repo, oid: str | None, limit: int = 500000) -> set:
    """从某提交可达的全部对象（提交/树/文件）"""
    if not oid or not repo.has(oid):
        return set()
    seen, stack = set(), [oid]
    while stack and len(seen) < limit:
        cur = stack.pop()
        if not cur or cur in seen or not repo.has(cur):
            continue
        seen.add(cur)
        kind, payload = repo.get(cur)
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
                    stack.append(it["h"])
    return seen


def contains(repo: Repo, newer: str | None, older: str | None) -> bool:
    """older 是否已在 newer 的历史里（沿全部父链，合并提交也算）：

    只有这样才能既支持快进、又支持「合并后推送」。
    """
    if not newer or not older:
        return False
    if newer == older:
        return True
    return older in reachable(repo, newer)


def verify_complete(repo: Repo, oid: str | None):
    """确认某个提交的对象图（提交/树/全部文件）在本机完整，缺一个就报错"""
    if not oid:
        return True
    if not repo.has(oid):
        raise SyncError("缺少提交对象 %s" % oid[:12])
    missing = []

    def walk(tree_oid):
        kind, payload = repo.get(tree_oid)
        if kind != "tree":
            raise SyncError("树对象损坏：%s" % tree_oid[:12])
        for it in json.loads(payload):
            if it["t"] == "d":
                walk(it["h"])
            elif not repo.has(it["h"]):
                missing.append(it["n"])

    c = repo.commit_info(oid)
    walk(c["tree"])
    if missing:
        raise SyncError("对象不完整，缺少 %d 个文件（如 %s）" % (len(missing), missing[0]))
    for p in c.get("parents") or []:
        if not repo.has(p):
            raise SyncError("缺少父提交 %s" % p[:12])
    return True


def _mark_pull_needed(repo: Repo, head: str, reason: str):
    """留下「历史到这一版了、工作区还没跟上」的记号（界面会据此提示去更新）"""
    try:
        os.makedirs(repo.meta, exist_ok=True)
        tmp = os.path.join(repo.meta, "PULL_NEEDED.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"head": head, "time": int(time.time() * 1000), "reason": reason},
                      f, ensure_ascii=False)
        os.replace(tmp, os.path.join(repo.meta, "PULL_NEEDED"))
    except OSError:
        pass


def _tmpdir(repo: Repo) -> str:
    d = os.path.join(repo.meta, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup_tmp(repo: Repo, older_than=3600):
    d = os.path.join(repo.meta, "tmp")
    if not os.path.isdir(d):
        return
    now = time.time()
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) > older_than:
                os.remove(p)
        except OSError:
            pass


# --------------------------------------------------------------------- HTTP 后端
def _req(base: str, method: str, path: str, body=None, token=None, timeout=60,
         raw_body: bytes | None = None):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if raw_body is not None:
        data = raw_body
        headers["Content-Type"] = "application/octet-stream"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-ZGR-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            if "json" in ctype:
                return json.loads(raw.decode("utf-8"))
            return raw
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error") or msg
        except ValueError:
            pass
        raise SyncError("远端返回 %s：%s" % (e.code, msg)) from None
    except urllib.error.URLError as e:
        raise SyncError("连不上 %s：%s" % (base, e.reason)) from None


class HttpBackend:
    kind = "http"

    def __init__(self, base: str, token=None, project: str = ""):
        self.base = (base or "").rstrip("/")
        self.token = token
        self.project = project or ""
        self.name = ""

    def _q(self) -> str:
        if not self.project:
            return ""
        from urllib.parse import quote
        return "?project=" + quote(self.project)

    def refs(self):
        r = _req(self.base, "GET", "/api/sync/refs" + self._q(), token=self.token, timeout=15)
        self.name = r.get("name") or self.base
        return r

    def manifest(self, branch, have):
        body = {"have": have, "branch": branch}
        if self.project:
            body["project"] = self.project
        m = _req(self.base, "POST", "/api/sync/manifest", body,
                 token=self.token, timeout=120)
        return m.get("objects") or []

    def fetch(self, repo: Repo, oids, log=print, progress=None) -> int:
        """流式下载：先落临时文件，边下边校验，最后原子改名

        progress(i, total)：每处理一个对象回调一次（整仓克隆要显示进度条）
        """
        got = 0
        tmpdir = _tmpdir(repo)
        total = len(oids)
        for i, oid in enumerate(oids):
            if repo.has(oid):
                if progress:
                    progress(i + 1, total)
                continue
            dst = os.path.join(tmpdir, oid + ".part")
            url = self.base + "/api/sync/object/" + oid + self._q()
            headers = {"X-ZGR-Token": self.token} if self.token else {}
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=600) as r, open(dst, "wb") as f:
                    shutil.copyfileobj(r, f, CHUNK)
            except urllib.error.HTTPError as e:
                raise SyncError("下载对象 %s 失败：HTTP %s" % (oid[:12], e.code)) from None
            except urllib.error.URLError as e:
                raise SyncError("下载对象 %s 失败：%s" % (oid[:12], e.reason)) from None
            try:
                if repo.put_raw_file(oid, dst):
                    got += 1
            finally:
                try:
                    os.remove(dst)
                except OSError:
                    pass
            if progress:
                progress(i + 1, total)
            elif (i + 1) % 200 == 0:
                log("  已接收 %d/%d" % (i + 1, len(oids)))
        return got

    def push(self, repo: Repo, oids, log=print) -> int:
        """小对象走 JSON 批量，大对象走原始字节流"""
        sent = 0
        small, big = [], []
        for oid in oids:
            size = os.path.getsize(repo.object_path(oid))
            (big if size > RAW_THRESHOLD else small).append(oid)
        batch, bsize = [], 0
        for oid in small + [None]:
            sz = os.path.getsize(repo.object_path(oid)) if oid else 0
            if oid is not None and batch and bsize + sz > BATCH_BYTES:
                sent += self._post_json(repo, batch)
                batch, bsize = [], 0
            if oid is not None:
                batch.append(oid)
                bsize += sz
        if batch:
            sent += self._post_json(repo, batch)
        for n, oid in enumerate(big, 1):
            with open(repo.object_path(oid), "rb") as f:
                raw = f.read()
            r = _req(self.base, "POST", "/api/sync/object/" + oid + self._q(), raw_body=raw,
                     token=self.token, timeout=1800)
            sent += int((r or {}).get("stored", 0)) + int((r or {}).get("skipped", 0))
            log("  已发送大对象 %d/%d（%.1f MB）" % (n, len(big), len(raw) / 1048576))
        return sent

    def _post_json(self, repo, batch):
        payload = {oid: base64.b64encode(repo.object_bytes(oid)).decode("ascii")
                   for oid in batch}
        body = {"objects": payload}
        if self.project:
            body["project"] = self.project
        r = _req(self.base, "POST", "/api/sync/objects", body,
                 token=self.token, timeout=600)
        return int(r.get("stored", 0)) + int(r.get("skipped", 0))

    def set_ref(self, branch, old, new):
        body = {"branch": branch, "old": old, "new": new}
        if self.project:
            body["project"] = self.project
        return _req(self.base, "POST", "/api/sync/refs", body,
                    token=self.token, timeout=60)


# --------------------------------------------------------------------- 共享文件夹后端
class ShareBackend:
    """把共享目录里的一个裸仓库当远端；对象就是文件复制，稳定又快"""

    kind = "share"

    def __init__(self, root: str, project: str):
        self.root = root
        self.project = project
        self.path = os.path.join(root, project)
        self.repo = Repo(self.path)
        self.name = "共享目录 %s" % project

    # ---- 初始化（第一次用时自动建）
    def ensure(self):
        if self.repo.exists:
            return
        try:
            self.repo.init(self.project)
        except RepoError as e:
            raise SyncError("共享目录不可用：%s（%s）" % (self.root, e)) from None

    def refs(self):
        self.ensure()
        return {"ok": True, "name": self.name,
                "branches": {b: self.repo.head_oid(b) for b in self.repo.branches()},
                "default": self.repo.branch}

    def manifest(self, branch, have):
        head = self.repo.head_oid(branch)
        have_set = reachable(self.repo, have)
        return sorted(reachable(self.repo, head) - have_set)

    def fetch(self, repo: Repo, oids, log=print, progress=None) -> int:
        got = 0
        tmpdir = _tmpdir(repo)
        for i, oid in enumerate(oids):
            if progress:
                try:
                    progress(i, len(oids))
                except Exception:      # 界面进度条坏了不能拖垮同步
                    pass
            if repo.has(oid):
                continue
            src = self.repo.object_path(oid)
            if not os.path.isfile(src):
                raise SyncError("共享目录缺少对象 %s" % oid[:12])
            dst = os.path.join(tmpdir, oid + ".part")
            shutil.copyfile(src, dst)
            if repo.put_raw_file(oid, dst):
                got += 1
            try:
                os.remove(dst)
            except OSError:
                pass
            if (i + 1) % 200 == 0:
                log("  已接收 %d/%d" % (i + 1, len(oids)))
        return got

    def push(self, repo: Repo, oids, log=print) -> int:
        got = 0
        self.ensure()
        for i, oid in enumerate(oids, 1):
            dest = self.repo.object_path(oid)
            if os.path.isfile(dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp.%d" % os.getpid()
            shutil.copy(repo.object_path(oid), tmp)
            os.replace(tmp, dest)
            got += 1
            if i % 500 == 0:
                log("  已写入共享目录 %d/%d" % (i, len(oids)))
        return got

    def set_ref(self, branch, old, new):
        cur = self.repo.head_oid(branch)
        if (cur or None) != (old or None):
            raise SyncError("共享目录的分支已变化（当前 %s），请重新同步" % (cur or "空")[:12])
        if new and not self.repo.has(new):
            raise SyncError("共享目录缺少提交对象 %s" % new[:12])
        try:
            verify_complete(self.repo, new)
        except SyncError as e:
            raise SyncError("共享目录里的对象不完整：%s" % e) from None
        self.repo.set_branch(branch, new)
        return {"ok": True, "branch": branch, "head": new, "applied": "history_only"}


def make_backend(url: str = "", share: str = "", project: str = "", token=None):
    if share:
        root = os.path.expandvars(os.path.expanduser(share))
        if not os.path.isdir(root):
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as e:
                raise SyncError("共享目录打不开：%s（%s）" % (root, e)) from None
        be = ShareBackend(root, project)
        be.name = "共享目录 " + project
        return be
    if not url:
        raise SyncError("请给出对方地址或共享目录")
    return HttpBackend(url, token=token, project=project)


# --------------------------------------------------------------------- 主流程
def sync(repo: Repo, base: str = "", push: bool = True, pull: bool = True,
         token=None, branch: str | None = None, checkout: bool = False,
         force: bool = False, log=print, share: str = "", project: str = "",
         backend=None):
    """与远端同步；返回结果摘要

    backend 给定时直接用（WebDAV/S3 等由 remotes.py 造好的后端），
    不再按 base/share 自己挑。
    """
    if not repo.exists:
        raise SyncError("这里不是追光几何仓库：%s" % repo.root)
    be = backend or make_backend(url=base, share=share,
                                 project=project or repo.name, token=token)
    _cleanup_tmp(repo)
    branch = branch or repo.branch
    refs = be.refs()
    remote_name = refs.get("name") or "远端"
    branches = refs.get("branches") or {}
    if branches:
        branch = _pick_branch(refs, branch)
    else:
        branch = branch or "main"
    rhead = branches.get(branch)
    lhead = repo.head_oid(branch)
    log("远端「%s」分支 %s：%s" % (remote_name, branch, (rhead or "空")[:12]))
    log("本机分支 %s：%s" % (branch, (lhead or "空")[:12]))

    result = {"branch": branch, "local": lhead, "remote": rhead,
              "pulled": 0, "pushed": 0, "head": lhead, "checked_out": False,
              "backend": be.kind, "messages": []}

    # ---------------- 拉取
    if pull and rhead and rhead != lhead:
        if lhead and contains(repo, lhead, rhead):
            log("远端是旧版本，无需拉取")
            result["messages"].append("远端无新提交")
        else:
            need = [o for o in be.manifest(branch, lhead) if not repo.has(o)]
            log("需要下载 %d 个对象" % len(need))
            result["pulled"] = be.fetch(repo, need, log=log)
            if lhead is None or contains(repo, rhead, lhead):
                verify_complete(repo, rhead)      # 缺对象绝不移动分支
                clean_before = True
                if checkout:
                    try:
                        clean_before = repo.status()["clean"]
                    except RepoError:
                        clean_before = True
                repo.set_branch(branch, rhead)
                lhead = rhead
                result["head"] = rhead
                result["messages"].append("已快进到远端最新")
                log("已快进到 %s" % rhead[:12])
                if checkout:
                    if not clean_before and not force:
                        _mark_pull_needed(repo, rhead, "工作区有未记录的改动，未覆盖")
                        result["messages"].append("工作区有未记录改动，未覆盖")
                        log("工作区有未记录改动，跳过落地（加 --force 强制）")
                    else:
                        try:
                            repo.restore(rhead, backup=True)
                        except RepoError as e:
                            # 分支已经前进了，工作区还是旧的——必须留下「待更新」记号，
                            # 否则下一次自动记录会把旧文件记成新版本，再被推回对方
                            _mark_pull_needed(repo, rhead, "工作区更新失败：%s" % e)
                            raise SyncError(
                                "分支已更新到 %s，但工作区更新失败（多半图纸正被占着）：%s。"
                                "关掉占用它的程序后重新同步即可，本机没有丢东西。"
                                % (rhead[:12], e))
                        result["checked_out"] = True
                        result["messages"].append("已把工作区更新为该版本")
                        log("工作区已更新为该版本")
            else:
                raise SyncError(
                    "两边都有新提交，历史已分叉。用「合并」把对方改动并进来：\n"
                    "  1) 网页「双机同步」→ 查看对方改动 → 按文件选择性合并；\n"
                    "  2) 命令行 zgr merge --url <对方地址>；\n"
                    "  3) 只想要对方版本：zgr sync --pull --checkout --force")

    # ---------------- 推送
    if push and lhead and lhead != rhead:
        if rhead and not repo.has(rhead):
            need = [o for o in be.manifest(branch, lhead) if not repo.has(o)]
            be.fetch(repo, need, log=log)
            rhead = rhead or None
        if rhead and not contains(repo, lhead, rhead):
            raise SyncError("远端已前进，请先拉取或合并后再推送")
        have_remote = reachable(repo, rhead) if rhead and repo.has(rhead) else set()
        need = [o for o in reachable(repo, lhead) if o not in have_remote]
        log("需要上传 %d 个对象" % len(need))
        if need:
            result["pushed"] = be.push(repo, sorted(need), log=log)
        r = be.set_ref(branch, rhead, lhead)
        result["remote"] = r.get("head")
        result["messages"].append("已推送 %d 个对象" % len(need))
        log("已推送到远端 %s" % lhead[:12])
    elif push:
        result["messages"].append("两边已一致，无需推送")

    if not result["messages"]:
        result["messages"].append("两边已一致")
    return result


def repair(repo: Repo, base: str = "", token=None, project: str = "", branch=None,
           log=print, backend=None, deep: bool = True) -> dict:
    """把另一台机器当备份：本机缺的、坏的对象，按对象号从远端补回来。

    和 sync(pull=True) 的区别：pull 只比分支头，头一样就什么都不做；
    这里逐个核对对象，专门对付「文件在、对象丢了/坏了」这种头看不出来的损伤。
    只下载、只删本机的坏对象，绝不改分支头、绝不碰工作区里的图纸。
    """
    be = backend or make_backend(url=base, project=project or repo.name, token=token)
    _cleanup_tmp(repo)
    branch = branch or repo.branch
    refs = be.refs()
    rb = refs.get("branches") or {}
    if branch not in rb:
        branch = _pick_branch(refs, branch) if rb else branch
    rhead = rb.get(branch)
    if not rhead:
        raise SyncError("远端「%s」没有分支 %s 的提交，没法当备份源"
                        % (refs.get("name") or "远端", branch))

    rep = repo.fsck(deep=deep)
    bad = [b["oid"] for b in rep["bad"]]
    removed_bad = 0
    for oid in bad:
        # 不直接删：挪到旁边留个案底（.bad），万一以后要查是怎么坏的
        src = repo.object_path(oid)
        try:
            os.replace(src, src + ".bad")
            removed_bad += 1
        except OSError:
            pass
    log("本机：缺 %d 个对象，坏 %d 个（坏的已移除，准备重下）"
        % (len(rep["missing"]), removed_bad))

    remote_objs = be.manifest(branch, [])
    need = [o for o in remote_objs if not repo.has(o)]
    log("远端有 %d 个对象，本机要补 %d 个" % (len(remote_objs), len(need)))
    problems = []
    got = 0
    if need:
        try:
            got = be.fetch(repo, need, log=log)
        except SyncError as e:
            # 对方那份备份也不全：能补多少补多少，剩下的老实报出来
            problems.append(str(e))

    local_head = repo.head_oid(branch)
    if local_head:
        try:
            verify_complete(repo, local_head)
        except SyncError as e:
            problems.append(str(e))
    after = repo.fsck(deep=deep)
    if after["missing"] or after["bad"]:
        problems.append("补完之后仍然缺 %d 个、坏 %d 个——远端这份备份也不全，"
                        "请用另一台机器或更早的备份再修一次"
                        % (len(after["missing"]), len(after["bad"])))
    return {"ok": not problems, "branch": branch, "fetched": got,
            "removed_bad": removed_bad, "missing_before": len(rep["missing"]),
            "bad_before": len(bad), "problems": problems}


def _pick_branch(refs: dict, branch: str):
    branches = refs.get("branches") or {}
    if branch in branches:
        return branch
    if len(branches) == 1:
        return next(iter(branches))
    raise SyncError("远端没有分支 %s（现有：%s）" % (branch, "、".join(sorted(branches)) or "无"))


def _ip_rank(ip: str) -> int:
    """局域网地址排序：10.x 优先，其次 192.168，再 172.16-31，最后其他"""
    if ip.startswith("10."):
        return 0
    if ip.startswith("192.168."):
        return 1
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            if 16 <= second <= 31:
                return 2
        except ValueError:
            pass
    return 3


def _usable(ip: str) -> bool:
    if ip.startswith("127.") or ip.startswith("169.254."):
        return False
    if ip.startswith("198.18.") or ip.startswith("198.19."):   # Clash TUN 等虚拟网段
        return False
    if ip == "0.0.0.0":
        return False
    return True


def local_urls(port: int):
    """给出本机可用于同步的地址（过滤虚拟网卡，按内网可用性排序）"""
    found = set()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _usable(ip):
                found.add(ip)
    except OSError:
        pass
    for probe in (("10.255.255.255", 1), ("192.168.255.255", 1), ("8.8.8.8", 53)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect(probe)
            ip = s.getsockname()[0]
            s.close()
            if _usable(ip):
                found.add(ip)
        except OSError:
            pass
    urls = ["http://127.0.0.1:%d" % port]
    urls += ["http://%s:%d" % (ip, port) for ip in sorted(found, key=lambda x: (_ip_rank(x), x))]
    return urls
