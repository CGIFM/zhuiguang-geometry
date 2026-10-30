"""追光几何 · 实时同步 / 动态事件 / 打包 / 回收站

这里的每个功能都只依赖标准库，原则和仓库其它部分一致：

* **实时同步**：后台线程定时问对方「现在到哪一版了」。对方前进就自动下载对象、
  工作区干净时直接落地；两边都改过就按智能三路合并自动并一次。
  合并永远不删文件，冲突块默认保留本机，并留一条动态让用户看得见。
* **动态事件**：`.zgr/events.jsonl` 追加式记录。网页轮询拿到「谁在什么时候
  改了哪些文件」，不需要用户点任何按钮。
* **打包**：把某个版本或当前文件夹压成 zip，给别人直接发。
* **回收站**：沿用 `.zgr/backup/<时间戳>`。删除/覆盖/还原前的文件都在里面，
  可以按批次整体复原，复原本身又会先备份当前内容，来回都能走。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import zipfile

from . import merge as merge_mod
from .store import (META_DIR, Repo, RepoError, fs_path, ignored, is_link_like,
                    norm, safe_branch, safe_rel)
from .sync import (SyncError, contains, make_backend, reachable,
                   verify_complete)

#: 两人共用一条线：谁记都记在同一分支上，分叉了由程序自动合并（老行为）
MODE_SHARED = "shared"
#: 一人一条线：各记各的分支，互相只看不动，合不合并由人决定
MODE_SPLIT = "split"

EVENT_FILE = "events.jsonl"
EVENT_SEQ = "events.seq"
EVENT_MAX_BYTES = 4 << 20
PACK_KEEP = 40
PACK_NAME = re.compile(r"\A[A-Za-z0-9._\-一-鿿 ()]+\.zip\Z")

_EVENT_LOCK = threading.Lock()


# --------------------------------------------------------------------- 动态事件
def _read_seq(repo: Repo) -> int:
    try:
        with open(os.path.join(repo.meta, EVENT_SEQ), "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def append_event(repo: Repo, kind: str, **fields):
    """追加一条动态。seq 单调递增，网页按 seq 增量拉取。"""
    if not repo.exists:
        return None
    with _EVENT_LOCK:
        seq = _read_seq(repo) + 1
        ev = {"seq": seq, "t": int(time.time() * 1000), "kind": kind}
        ev.update({k: v for k, v in fields.items() if v is not None})
        try:
            cfg_author = ""
            try:
                cfg_author = repo.config.get("author") or ""
            except RepoError:
                pass
            ev.setdefault("author", cfg_author)
            p = os.path.join(repo.meta, EVENT_FILE)
            if os.path.isfile(p) and os.path.getsize(p) > EVENT_MAX_BYTES:
                os.replace(p, p + ".1")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            tmp = os.path.join(repo.meta, EVENT_SEQ + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(str(seq))
            os.replace(tmp, os.path.join(repo.meta, EVENT_SEQ))
        except OSError:
            pass
        return ev


def read_events(repo: Repo, since: int = 0, limit: int = 200):
    """返回 since 之后的动态（最多 limit 条）和当前最大 seq"""
    since = max(0, int(since or 0))
    rows = []
    p = os.path.join(repo.meta, EVENT_FILE)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if int(ev.get("seq") or 0) > since:
                    rows.append(ev)
    except OSError:
        pass
    return {"events": rows[-limit:], "seq": max([_read_seq(repo)] + [r.get("seq", 0) for r in rows])}


def change_summary(repo: Repo, old: str | None, new: str | None, limit: int = 30):
    """两个版本之间改了什么：给实时动态用的一句话"""
    out = {"counts": {"added": 0, "modified": 0, "deleted": 0}, "files": []}
    if not new or not repo.has(new):
        return out
    try:
        d = repo.diff(old or "", new)
    except RepoError:
        return out
    out["counts"] = d["counts"]
    for x in d["added"]:
        out["files"].append({"path": x["path"], "st": "A"})
    for x in d["modified"]:
        out["files"].append({"path": x["path"], "st": "M"})
    for x in d["deleted"]:
        out["files"].append({"path": x["path"], "st": "D"})
    out["files"] = out["files"][:limit]
    out["total"] = (d["counts"]["added"] + d["counts"]["modified"]
                    + d["counts"]["deleted"])
    return out


def describe_commit(repo: Repo, oid: str, old: str | None = None):
    try:
        c = repo.commit_info(oid)
    except RepoError:
        return {}
    s = change_summary(repo, old, oid)
    return {"commit": oid, "author": c.get("author") or "",
            "message": c.get("message") or "", "time": c.get("time"),
            "counts": s["counts"], "files": s["files"], "total": s.get("total", 0)}


# --------------------------------------------------------------------- 打包
def _safe_name(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (name or "项目")).strip(" .")
    return s[:60] or "项目"


def _prune_packs(d: str):
    try:
        items = [(os.path.getmtime(os.path.join(d, n)), n)
                 for n in os.listdir(d) if n.lower().endswith(".zip")]
    except OSError:
        return
    for _, n in sorted(items, reverse=True)[PACK_KEEP:]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def pack(repo: Repo, commit: str | None = None, worktree: bool = False,
         prefix: str = ""):
    """把一个版本（或当前文件夹）打成 zip，返回下载信息"""
    commit = repo.resolve(commit) if commit else (None if worktree else repo.head_oid())
    if not commit and not worktree:
        raise RepoError("还没有任何版本可以打包")
    d = os.path.join(repo.meta, "packs")
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "工作区" if worktree else (commit or "")[:8]
    fn = "%s-%s-%s.zip" % (_safe_name(repo.name), tag, stamp)
    out = os.path.join(d, fn)
    root = _safe_name(prefix or repo.name) + "-" + tag
    files = 0
    total = 0

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if worktree:
            pats = repo.ignore_patterns
            walk_root = fs_path(repo.root)
            for dirpath, dirnames, filenames in os.walk(walk_root, topdown=True):
                rel_dir = norm(os.path.relpath(dirpath, walk_root))
                if rel_dir == ".":
                    rel_dir = ""
                dirnames[:] = [x for x in dirnames
                               if not (not rel_dir and x == META_DIR)
                               and not is_link_like(os.path.join(dirpath, x))]
                for f in sorted(filenames):
                    rel = (rel_dir + "/" + f).lstrip("/")
                    if ignored(rel, pats) or is_link_like(os.path.join(dirpath, f)):
                        continue
                    ap = os.path.join(dirpath, f)
                    try:
                        z.write(ap, root + "/" + rel)
                        files += 1
                        total += os.path.getsize(ap)
                    except OSError:
                        continue
            info = {"commit": "", "message": "当前文件夹（未记录内容也在内）",
                    "time": int(time.time() * 1000)}
        else:
            c, fl = repo.files_of(commit)
            tmpdir = os.path.join(repo.meta, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            for rel, item in sorted(fl.items()):
                src = repo.object_path(item["oid"])
                if not os.path.isfile(src):
                    raise RepoError("缺少文件对象：%s" % rel)
                tmp = os.path.join(tmpdir, "pack-%s.blob" % item["oid"][:16])
                try:
                    repo.blob_to_file(item["oid"], tmp)
                    z.write(tmp, root + "/" + rel)
                    files += 1
                    total += int(item.get("size") or 0)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            info = {"commit": commit, "message": c.get("message") or "",
                    "time": c.get("time")}

        note = ("追光几何打包\r\n"
                "项目：%s\r\n"
                "版本：%s\r\n"
                "时间：%s\r\n"
                "文件：%d 个，原始大小 %.1f MB\r\n"
                "说明：%s\r\n"
                % (repo.name, info.get("commit") or "（当前文件夹）",
                   time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime((info.get("time") or 0) / 1000 or None)),
                   files, total / 1048576.0, info.get("message") or ""))
        z.writestr(root + "/追光几何-打包说明.txt", note.encode("utf-8"))

    _prune_packs(d)
    return {"file": out, "name": fn, "size": os.path.getsize(out), "files": files,
            "commit": info.get("commit") or "", "message": info.get("message") or ""}


def pack_file(repo: Repo, name: str):
    """下载用：只允许取 .zgr/packs 里的 zip，名字不允许带路径"""
    if not PACK_NAME.match(name or ""):
        raise RepoError("文件名不合法")
    p = os.path.join(repo.meta, "packs", name)
    if not os.path.isfile(p):
        raise RepoError("打包文件不存在，请重新打包")
    return p


# --------------------------------------------------------------------- 回收站
def _backup_time(name: str):
    m = re.match(r"(\d{8})-(\d{6})", name or "")
    if not m:
        return None
    try:
        return int(time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))) * 1000
    except ValueError:
        return None


def list_backups(repo: Repo, limit: int = 200):
    d = os.path.join(repo.meta, "backup")
    out = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for name in names[:limit]:
        p = fs_path(os.path.join(d, name))
        if not os.path.isdir(p):
            continue
        files, size = [], 0
        for dp, _dn, fns in os.walk(p):
            for f in fns:
                ap = os.path.join(dp, f)
                rel = norm(os.path.relpath(ap, p))
                files.append(rel)
                try:
                    size += os.path.getsize(ap)
                except OSError:
                    pass
        out.append({"name": name, "time": _backup_time(name),
                    "files": len(files), "bytes": size,
                    "sample": files[:12]})
    return out


def _backup_dir(repo: Repo, name: str) -> str:
    """一批备份的目录；名字只认批次名，别的一律拒绝（路径穿越）"""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise RepoError("备份批次不合法")
    src = os.path.join(repo.meta, "backup", name)
    if not os.path.isdir(fs_path(src)):
        raise RepoError("找不到这批备份：%s" % name)
    return src


def backup_files(repo: Repo, name: str, limit: int = 5000):
    """这一批备份里有哪些文件（相对路径 + 大小 + 修改时间）

    「整批复原」是一把双刃剑：只想找回误删的那一个零件，却把当时所有被覆盖的
    文件一起盖回去。先让人看清楚里面有什么，再挑。
    """
    src = fs_path(_backup_dir(repo, name))
    rows, total = [], 0
    for dp, _dn, fns in os.walk(src):
        for f in fns:
            ap = os.path.join(dp, f)
            rel = norm(os.path.relpath(ap, src))
            if rel.startswith(META_DIR + "/") or rel == META_DIR:
                continue
            total += 1
            if len(rows) >= limit:
                continue
            try:
                st = os.stat(ap)
            except OSError:
                continue
            rows.append({"path": rel, "size": int(st.st_size),
                         "time": int(st.st_mtime * 1000)})
    rows.sort(key=lambda x: x["path"])
    return {"name": name, "files": rows, "total": total,
            "truncated": total > len(rows)}


def restore_backup_file(repo: Repo, name: str, rel: str, backup=True) -> dict:
    """只把一批备份里的**一个**文件放回工作区。

    和整批复原一样，覆盖前先把当前这个文件存进一批新的备份（`单文件-时间`），
    所以「恢复错了」还能再恢复回来。
    """
    src = _backup_dir(repo, name)
    rel = safe_rel(rel)
    if not rel:
        raise RepoError("文件名不合法：%s" % (rel or "（空）"))
    ap = fs_path(os.path.join(src, rel.replace("/", os.sep)))
    if not os.path.isfile(ap):
        raise RepoError("这批备份里没有这个文件：%s" % rel)
    dst = fs_path(os.path.join(repo.root, rel.replace("/", os.sep)))
    saved = ""
    if backup and os.path.isfile(dst):
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-单文件"
        bdir = os.path.join(repo.meta, "backup", stamp)
        n = 0
        while os.path.exists(bdir):
            n += 1
            bdir = os.path.join(repo.meta, "backup", "%s-%d" % (stamp, n))
        bdst = fs_path(os.path.join(bdir, rel.replace("/", os.sep)))
        try:
            os.makedirs(os.path.dirname(bdst), exist_ok=True)
            shutil.copy2(dst, bdst)
            saved = os.path.relpath(bdir, os.path.join(repo.meta, "backup"))
        except OSError as e:
            raise RepoError("备份现有文件失败，未做改动：%s" % e) from None
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(ap, dst)
    except OSError as e:
        raise RepoError("写回失败（文件可能正被 SolidWorks 等程序占用）：%s" % e) from None
    try:
        size = os.path.getsize(dst)
    except OSError:
        size = 0
    return {"name": name, "file": rel, "size": size, "safety_backup": saved}


def restore_backup(repo: Repo, name: str):
    """把某一批备份整体放回项目目录。写回前先把当前内容备份一批，可再次反悔。"""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise RepoError("备份批次不合法")
    src = fs_path(os.path.join(repo.meta, "backup", name))
    if not os.path.isdir(src):
        raise RepoError("找不到这批备份：%s" % name)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-复原前"
    keep = os.path.join(repo.meta, "backup", stamp)
    n = 0
    while os.path.exists(keep):
        n += 1
        keep = os.path.join(repo.meta, "backup", "%s-%d" % (stamp, n))
    written = []
    for dp, _dn, fns in os.walk(src):
        for f in fns:
            ap = os.path.join(dp, f)
            rel = norm(os.path.relpath(ap, src))
            if rel.startswith(META_DIR + "/") or rel == META_DIR:
                continue
            dst = fs_path(os.path.join(repo.root, rel.replace("/", os.sep)))
            if os.path.exists(dst):
                bdst = fs_path(os.path.join(keep, rel.replace("/", os.sep)))
                os.makedirs(os.path.dirname(bdst), exist_ok=True)
                try:
                    shutil.copy2(dst, bdst)
                except OSError:
                    pass
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(ap, dst)
            written.append(rel)
    return {"name": name, "written": written, "count": len(written),
            "safety_backup": keep if os.path.isdir(keep) else None}


# --------------------------------------------------------------------- 一人一条线
def personal_branch_name(repo: Repo) -> str:
    """我这个人的线叫什么：优先用「记录人姓名」，没设就用系统用户名"""
    author = ""
    try:
        author = (repo.config.get("author") or "").strip()
    except RepoError:
        pass
    if not author:
        author = (os.environ.get("USERNAME") or "").strip()
    return safe_branch(author or "我的线")


def ensure_personal_branch(repo: Repo, author: str | None = None):
    """确保本机有一条「我的线」，当前不在它上面就从现在的版本分出来并切过去。

    为什么要有这一步：两个人原本都记在 `main` 上，开了「一人一条线」之后
    如果还留在 `main`，谁记一笔都往同一条线上摞，等于没分开。所以第一次
    开启时**从当前版本分一条属于自己的分支**（提交一个都不复制，只是多一个
    指向同一提交的指针），之后两个人各记各的。
    """
    name = safe_branch((author or "").strip() or personal_branch_name(repo))
    cur = repo.branch
    if name == cur:
        return {"branch": name, "created": False, "switched": False,
                "from": cur, "head": repo.head_oid()}
    head = repo.head_oid()
    if name in repo.branches():
        repo._write_text("HEAD", name)
        return {"branch": name, "created": False, "switched": True,
                "from": cur, "head": repo.head_oid()}
    repo._write_text("HEAD", name)
    if head:
        repo.set_branch(name, head)
    else:
        repo._write_text(os.path.join("refs", "heads", safe_branch(name)), "")
    return {"branch": name, "created": True, "switched": True,
            "from": cur, "head": head}


# --------------------------------------------------------------------- 实时同步
class LiveSync(threading.Thread):
    """定时对账 + 自动落地。一个项目一条线程，停止时干净退出。"""

    MIN_INTERVAL = 5
    MAX_INTERVAL = 3600

    def __init__(self, repo: Repo, url: str, token: str = "", interval: int = 15,
                 auto_pull: bool = True, auto_push: bool = True,
                 auto_merge: bool = True, checkout: bool = True,
                 mode: str = MODE_SHARED,
                 lock: threading.RLock | None = None, logger=None):
        super().__init__(daemon=True)
        self.repo = repo
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.interval = max(self.MIN_INTERVAL, min(self.MAX_INTERVAL, int(interval)))
        self.mode = MODE_SPLIT if str(mode).lower() == MODE_SPLIT else MODE_SHARED
        self.auto_pull = auto_pull
        self.auto_push = auto_push
        self.auto_merge = auto_merge if self.mode == MODE_SHARED else False
        self.checkout = checkout
        self.lock = lock or threading.RLock()
        self.stop_flag = threading.Event()
        self.logger = logger or (lambda m: None)
        # 上次因「同一处两边改成不同内容」而停下时的 (对方头, 本机头)：
        # 同一对头只提醒一次，避免每轮都刷事件
        self._conflict_key = None
        self.status = {
            "running": False, "url": self.url, "interval": self.interval,
            "mode": self.mode, "my_branch": repo.branch,
            "peer_branches": [],
            "auto_pull": auto_pull, "auto_push": auto_push,
            "auto_merge": self.auto_merge, "checkout": checkout,
            "last_check": None, "last_ok": None, "last_error": "",
            "state": "starting", "peer_head": None, "peer_name": "",
            "checks": 0, "pulls": 0, "pushes": 0, "merges": 0,
        }

    def run(self):
        self.status["running"] = True
        self.logger("实时同步已开启：%s（每 %d 秒对账一次）"
                    % (self.repo.name, self.interval))
        append_event(self.repo, "live_on", url=self.url, interval=self.interval,
                     message="实时同步已开启")
        while not self.stop_flag.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - 一条线程不能因为一次异常死掉
                self.status["last_error"] = "%s" % e
                self.status["state"] = "error"
                self.logger("实时同步出错：%s" % e)
            self.stop_flag.wait(self.interval)
        self.status["running"] = False
        self.status["state"] = "stopped"
        append_event(self.repo, "live_off", message="实时同步已关闭")

    def stop(self):
        self.stop_flag.set()

    def _be(self):
        return make_backend(url=self.url, project=self.repo.name,
                            token=self.token or None)

    def _flush_pull_flag(self):
        try:
            os.remove(os.path.join(self.repo.meta, "PULL_NEEDED"))
        except OSError:
            pass

    def _mark_pull_needed(self, head: str, reason: str):
        tmp = os.path.join(self.repo.meta, "PULL_NEEDED.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"head": head, "time": int(time.time() * 1000),
                           "reason": reason}, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(self.repo.meta, "PULL_NEEDED"))
        except OSError:
            pass

    def tick(self) -> dict:
        repo = self.repo
        if not repo.exists:
            raise SyncError("项目已经不存在")
        be = self._be()
        refs = be.refs()
        self.status["last_check"] = int(time.time() * 1000)
        self.status["peer_name"] = refs.get("name") or self.url
        self.status["checks"] += 1
        self.status["last_ok"] = int(time.time() * 1000)
        self.status["last_error"] = ""
        self.status["state"] = "idle"
        if self.mode == MODE_SPLIT:
            return self._tick_split(be, refs)
        branches = refs.get("branches") or {}
        branch = repo.branch
        if branches and branch not in branches:
            branch = refs.get("default") or next(iter(branches))
        rhead = branches.get(branch)
        lhead = repo.head_oid(branch)
        self.status["peer_head"] = rhead

        if not rhead and not lhead:
            return self.status
        if rhead == lhead:
            return self.status

        # 对方的新提交可能本机还没有：先拿下来再判断「快进 / 本机领先 / 分叉」。
        # 否则 contains() 因为缺对象而误判成分叉，凭空多造一个合并提交。
        if rhead and not repo_has(repo, rhead):
            self._fetch(be, branch, lhead, rhead)

        # ---- 对方落后 / 本机领先：只推不拉
        if lhead and (not rhead or contains(repo, lhead, rhead)):
            if self.auto_push:
                self._push(branch, rhead)
            else:
                self.status["state"] = "local_ahead"
            return self.status

        # ---- 对方领先（快进）
        if lhead is None or contains(repo, rhead, lhead):
            if not self.auto_pull:
                self.status["state"] = "remote_ahead"
                return self.status
            self._fast_forward(be, branch, lhead, rhead)
            return self.status

        # ---- 分叉：智能合并
        if not self.auto_merge:
            self.status["state"] = "diverged"
            append_event(repo, "peer_diverged", peer=self.status["peer_name"],
                         message="两边都有新版本，需要合并")
            return self.status
        self._merge(be, branch, lhead, rhead)
        return self.status

    def _fetch(self, be, branch, lhead, rhead):
        need = [o for o in be.manifest(branch, lhead) if not repo_has(self.repo, o)]
        if need:
            self.logger("实时同步：下载 %d 个对象" % len(need))
            be.fetch(self.repo, need, log=lambda m: None)
        verify_complete(self.repo, rhead)
        return len(need)

    # ---------------------------------------------------------- 一人一条线（split）
    def _tick_split(self, be, refs) -> dict:
        """各记各的分支：看得见对方的线，但不合并、不切过去。

        三件事，顺序不能反：
        1. **先拉别人的线**：对象下载到本机、分支指针跟着走，时间线上就多出
           对方那一列——这一步只动「那条分支的指针」，绝不碰当前分支和工作区。
        2. **再看自己的线**：领先就推；对方把我的线往前推过就快进；
           真分叉了只报告，**绝不自动合并**（要不要合、合哪几个版本由人点）。
        3. 状态里带上双方各自的线，界面据此显示「我在 A 线、他在 B 线」。
        """
        repo = self.repo
        branches = refs.get("branches") or {}
        mine = repo.branch
        self.status["my_branch"] = mine
        rhead = branches.get(mine)
        lhead = repo.head_oid(mine)
        self.status["peer_head"] = rhead
        peers = []

        # ---- 1) 别人的线：只跟着走，不合并
        for name in sorted(branches):
            head = branches.get(name)
            if name == mine or not head:
                continue
            lh = repo.head_oid(name)
            if lh == head:
                peers.append({"branch": name, "head": head, "state": "same"})
                continue
            try:
                if not repo_has(repo, head):
                    # 本机还没有这条线：拿对方任意一条我们都有的线当「已经有了」的基准，
                    # 只下差的那部分。否则第一次会把他整条线的对象全下一遍（GB 级）。
                    basis = lh or next(
                        (h for b, h in branches.items()
                         if b != name and h and repo_has(repo, h)), None)
                    self._fetch(be, name, basis, head)
                # contains(repo, newer, older)：older 在 newer 的历史里。
                # 这里要问的是「本机这条线的头是不是对方那条线的祖先」，参数别写反。
                ahead = (not lh) or contains(repo, head, lh)
            except (SyncError, RepoError) as e:
                self.status["last_error"] = "拉取分支 %s 失败：%s" % (name, e)
                peers.append({"branch": name, "head": head, "state": "error"})
                continue
            if not ahead:
                # 本机这条分支上有对方没有的版本（理论上不该发生：一人一条线）
                peers.append({"branch": name, "head": head, "state": "diverged"})
                self.logger("实时同步：分支 %s 两边分叉了，没有自动覆盖" % name)
                continue
            with self.lock:
                repo.set_ref(name, head)
            peers.append({"branch": name, "head": head, "state": "updated",
                          "old": lh})
            try:
                new = len(repo.commit_reachable(head) - repo.commit_reachable(lh))
            except RepoError:
                new = 1
            desc = describe_commit(repo, head, lh)
            if lh:
                append_event(repo, "peer_branch", peer=self.status["peer_name"],
                             branch=name, head=head, from_head=lh,
                             author=desc.get("author"),
                             message="%s 在自己的线上又记了 %d 个版本"
                                     % (desc.get("author") or "对方", new),
                             counts=desc.get("counts"), files=desc.get("files"),
                             total=desc.get("total"))
            else:
                append_event(repo, "peer_branch_new", peer=self.status["peer_name"],
                             branch=name, head=head, author=desc.get("author"),
                             message="%s 开了一条新线「%s」"
                                     % (desc.get("author") or "对方", name))
            self.logger("实时同步：对方的线「%s」前进到 %s（%d 个新版本）"
                        % (name, head[:10], new))
        self.status["peer_branches"] = peers

        # ---- 2) 自己的线
        if not rhead and not lhead:
            return self.status
        if rhead == lhead:
            return self.status
        if lhead and (not rhead or contains(repo, lhead, rhead)):
            if self.auto_push:
                # 对方手上已经有它自己那几条线的全部对象，推的时候拿它们当基准，
                # 只传真正新的对象（分开的线第一次推尤其明显，不然等于重传整个项目）
                self._push(mine, rhead,
                           remote_heads=[h for b, h in branches.items()
                                         if b != mine and h])
            else:
                self.status["state"] = "local_ahead"
            return self.status
        if lhead and not repo_has(repo, rhead):
            self._fetch(be, mine, lhead, rhead)
        if lhead is None or contains(repo, rhead, lhead):
            if not self.auto_pull:
                self.status["state"] = "remote_ahead"
                return self.status
            self._fast_forward(be, mine, lhead, rhead)
            return self.status
        # 有人往「我的线」上推了跟我分叉的东西：不合并，等我看
        self.status["state"] = "diverged"
        key = (rhead, lhead)
        if self._conflict_key != key:
            self._conflict_key = key
            append_event(repo, "peer_diverged", peer=self.status["peer_name"],
                         head=lhead, peer_head=rhead,
                         message="我的线「%s」上出现了两边不同的版本，没有自动合并"
                                 % mine)
        return self.status

    def _fast_forward(self, be, branch, lhead, rhead):
        repo = self.repo
        self._fetch(be, branch, lhead, rhead)
        desc = describe_commit(repo, rhead, lhead)
        clean = False
        try:
            clean = repo.status()["clean"]
        except RepoError:
            pass
        with self.lock:
            repo.set_branch(branch, rhead)
            self._conflict_key = None
            if self.checkout and (clean or lhead is None):
                try:
                    repo.restore(rhead, backup=True)
                    self._flush_pull_flag()
                    self.status["state"] = "pulled"
                    self.status["pulls"] += 1
                    self.logger("实时同步：已更新到对方版本 %s（%s）"
                                % (rhead[:10], desc.get("author") or ""))
                    append_event(repo, "peer_pull", peer=self.status["peer_name"],
                                 head=rhead, from_head=lhead,
                                 author=desc.get("author"),
                                 message=desc.get("message") or "对方保存了新版本",
                                 counts=desc.get("counts"),
                                 files=desc.get("files"), total=desc.get("total"))
                except RepoError as e:
                    self._mark_pull_needed(rhead, "工作区更新失败：%s" % e)
                    self.status["state"] = "pull_failed"
                    append_event(repo, "peer_pending", peer=self.status["peer_name"],
                                 head=rhead, message="对方有新版本，写入工作区失败：%s" % e)
            else:
                why = "工作区有未记录的改动" if not clean else "已关闭自动更新"
                self._mark_pull_needed(rhead, why)
                self.status["state"] = "pending"
                append_event(repo, "peer_pending", peer=self.status["peer_name"],
                             head=rhead, author=desc.get("author"),
                             message="对方有新版本（%s）" % why,
                             counts=desc.get("counts"), files=desc.get("files"),
                             total=desc.get("total"))

    def _merge(self, be, branch, lhead, rhead):
        repo = self.repo
        self._fetch(be, branch, lhead, rhead)
        clean = False
        try:
            clean = repo.status()["clean"]
        except RepoError:
            pass
        with self.lock:
            p = merge_mod.plan(repo, rhead, ours=lhead, smart=True)
            got_auto = set(p.get("auto") or [])
            unresolved = [c for c in (p.get("conflicts") or []) if c not in got_auto]
            if unresolved:
                # 同一处两边改成不同内容：自动合并帮不上忙。**不生成合并提交、不推送**，
                # 否则两边各留各的、互相推，会无限循环。留给用户用「合并」面板选一次。
                self.status["state"] = "conflict"
                self.status["last_error"] = ""
                key = (rhead, lhead)
                if self._conflict_key != key:
                    self._conflict_key = key
                    self.logger("实时同步：%d 个文件冲突，等待手动选择" % len(unresolved))
                    append_event(
                        repo, "peer_conflict", peer=self.status["peer_name"],
                        head=lhead, peer_head=rhead,
                        message="%d 个文件两边改在同一处，需要你选一下"
                                % len(unresolved),
                        files=[{"path": x, "st": "C"} for x in unresolved][:30],
                        total=len(unresolved))
                return
            self._conflict_key = None
            res = merge_mod.apply(repo, p, strategy="smart",
                                  checkout=self.checkout and clean,
                                  backup=True,
                                  message="实时同步：自动合并 %s 的改动"
                                          % (self.status["peer_name"] or "对方"))
        self.status["merges"] += 1
        self.status["state"] = "merged" if clean else "merged_pending"
        if clean and self.checkout:
            self._flush_pull_flag()
        else:
            self._mark_pull_needed(res["commit"], "工作区有未记录的改动")
        self.logger("实时同步：已自动合并对方改动（%d 个文件）" % res["files"])
        try:
            c = repo.commit_info(res["commit"])
            other = c.get("author") or ""
        except RepoError:
            other = ""
        append_event(repo, "peer_merge", peer=self.status["peer_name"],
                     head=res["commit"], from_head=lhead, peer_head=rhead,
                     author=other, message="自动合并了对方的改动",
                     files=[{"path": d["path"], "st": d.get("pick") or ""}
                            for d in res.get("decisions", [])][:30],
                     total=res["files"])
        if self.auto_push:
            self._push(branch, rhead, after_merge=True)

    def _push(self, branch, rhead, after_merge=False, remote_heads=None):
        repo = self.repo
        try:
            be = self._be()
            lhead = repo.head_oid(branch)
            if not lhead:
                return
            have_remote = set()
            for h in ([rhead] if rhead and repo.has(rhead) else []) + list(remote_heads or []):
                if h and repo.has(h):
                    have_remote |= reachable(repo, h)
            need = [o for o in reachable(repo, lhead) if o not in have_remote]
            if need:
                be.push(repo, sorted(need), log=lambda m: None)
            be.set_ref(branch, rhead, lhead)
            self._conflict_key = None
            self.status["pushes"] += 1
            self.status["state"] = "pushed" if not after_merge else "merged_pushed"
            self.logger("实时同步：已把本机版本 %s 推给对方" % lhead[:10])
            desc = describe_commit(repo, lhead)
            append_event(repo, "peer_push", head=lhead, peer=self.status["peer_name"],
                         author=desc.get("author"),
                         message="本机新版本已自动同步给对方",
                         counts=desc.get("counts"), files=desc.get("files"),
                         total=desc.get("total"))
        except (SyncError, RepoError) as e:
            self.status["last_error"] = "%s" % e
            self.status["state"] = "push_failed"
            self.logger("实时同步：推送失败：%s" % e)


def repo_has(repo: Repo, oid: str) -> bool:
    return bool(oid) and repo.has(oid)
