# -*- coding: utf-8 -*-
"""把对方机器上的项目「搬一份到本机」——从零克隆，之后接着同步。

场景：174 新建/添加了一个项目，167 左侧也想看到它，点一下就拉过来一起做。
做法和 git clone 一个道理，顺序一步都不能少：

    问对方的 refs → 每条约分支缺哪些对象 → 一个一个要回来（流式、校验）
    → 本机把分支指针指过去 → 把工作区铺出来 → 登记成项目 → （可选）开实时同步

注意几个坑：
  · 目标目录必须空着。铺工作区会写入上千个文件，混进用户原有文件里没法收拾。
  · 不能建在别的项目里面（否则 .zgr 互相套娃，备份/恢复都会出错）。
  · 对象没下全绝不移动分支指针——半截仓库比没有更危险。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import sync as sync_mod
from .store import META_DIR, Repo, RepoError


class JoinError(Exception):
    pass


# --------------------------------------------------------------------- 对方项目清单
def _get_json(url: str, token: str = "", timeout: float = 6.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("X-ZGR-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4 << 20).decode("utf-8"))


def list_projects(url: str, token: str = "", timeout: float = 6.0) -> dict:
    """问对方有哪些项目（只有名字、版本数、大小、最后记录人和时间，不含路径）"""
    base = (url or "").rstrip("/")
    if not base:
        raise JoinError("没填对方地址")
    try:
        d = _get_json(base + "/api/peer/list", token, timeout)
    except urllib.error.HTTPError as e:
        raise JoinError("对方拒绝了（HTTP %s）" % e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise JoinError("连不上 %s：%s" % (base, getattr(e, "reason", e))) from None
    except ValueError:
        raise JoinError("对方返回的不是追光几何的数据") from None
    if not isinstance(d, dict) or not d.get("ok"):
        raise JoinError("对方返回异常")
    d["url"] = base
    return d


# --------------------------------------------------------------------- 目标目录体检
def check_dest(dest: str) -> str:
    dest = (dest or "").strip()
    if not dest:                       # abspath("") 会变成当前目录，得先挡掉空串
        raise JoinError("请选择一个本地文件夹")
    dest = os.path.abspath(dest)
    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise JoinError("这个路径是个文件，不是文件夹：%s" % dest)
        if os.path.isdir(os.path.join(dest, META_DIR)):
            raise JoinError("这个文件夹本身已经是一个追光几何项目了：%s" % dest)
        try:
            items = [n for n in os.listdir(dest) if not n.startswith(".")]
        except OSError as e:
            raise JoinError("读不了这个文件夹：%s" % e) from None
        if items:
            raise JoinError("这个文件夹不是空的（有 %d 个文件/文件夹）。"
                            "请选一个空文件夹或新建一个，避免把对方的文件混进你已有的资料里"
                            % len(items))
    # 不能套在别的项目里面
    p = dest
    while True:
        if os.path.isdir(os.path.join(p, META_DIR)):
            raise JoinError("不能在已有项目「%s」里面再建一个项目" % p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return dest


# --------------------------------------------------------------------- 克隆主体
def clone(url: str, project: str, dest: str, token: str = "",
          progress=None) -> dict:
    """把对方的 project 克隆到本机 dest，返回 {path, name, head, branch, objects, bytes}

    progress(phase=..., done=..., total=..., detail=...) 会被频繁调用，
    调用方负责节流（这里只管报，不管打印）。
    """
    url = (url or "").rstrip("/")
    if not url:
        raise JoinError("没填对方地址")
    project = (project or "").strip()
    if not project:
        raise JoinError("没说要拉哪个项目")
    dest = check_dest(dest)

    def say(phase, done=0, total=0, detail=""):
        if progress:
            progress(phase=phase, done=done, total=total, detail=detail)

    say("连接对方", detail=url)
    backend = sync_mod.HttpBackend(url, token=token, project=project)
    try:
        refs = backend.refs()
    except sync_mod.SyncError as e:
        raise JoinError(str(e)) from None
    heads = {k: v for k, v in (refs.get("branches") or {}).items() if v}
    if not heads:
        raise JoinError("对方的这个项目还没有任何版本")
    branch = refs.get("default") or (sorted(heads)[0] if heads else "main")
    if branch not in heads:
        branch = sorted(heads)[0]
    peer_name = refs.get("name") or project

    # 先把目录建好（空目录才允许进来，check_dest 已经验过）
    if not os.path.isdir(dest):
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            raise JoinError("建目录失败：%s" % e) from None
    repo = Repo(dest)
    fresh = not repo.exists
    if fresh:
        repo.init(peer_name)
    else:
        raise JoinError("目标目录里已经有 .zgr 了，不能重复克隆")

    # 对象：一条分支一条分支地问「你有哪些我没有的」
    total_objs = 0
    got_objs = 0
    try:
        for name in sorted(heads):
            say("准备对象清单", detail=name)
            oids = backend.manifest(name, [])
            total_objs += len(oids)
            say("下载 %s" % name, got_objs, total_objs, "%d 个对象" % len(oids))

            def tick(i, n, _base=got_objs):
                say("下载 %s" % name, _base + i, total_objs, "%d/%d" % (i, n))

            got_objs += backend.fetch(repo, oids, progress=tick)
            say("下载 %s" % name, got_objs, total_objs)

        # 对象齐了才动指针：任何一条分支缺对象都不挪
        say("校验", got_objs, total_objs)
        for name, head in heads.items():
            try:
                sync_mod.verify_complete(repo, head)
            except sync_mod.SyncError as e:
                raise JoinError("对象不完整，已中止（分支 %s）：%s" % (name, e)) from None

        for name, head in heads.items():
            if name == branch:
                continue
            repo.set_branch(name, head)
        repo.set_branch(branch, heads[branch])
        repo._write_text("HEAD", branch)

        say("铺开文件", got_objs, total_objs, "%d 个文件" % (
            (repo.commit_info(heads[branch]).get("stats") or {}).get("files") or 0))
        repo.restore(heads[branch], backup=False)
        try:                                  # 克隆下来的工作区就是这一版，别留「待更新」标记
            os.remove(os.path.join(repo.meta, "PULL_NEEDED"))
        except OSError:
            pass
    except JoinError:
        raise
    except (sync_mod.SyncError, RepoError) as e:
        raise JoinError(str(e)) from None
    except (urllib.error.URLError, OSError) as e:
        raise JoinError("下载中断：%s" % getattr(e, "reason", e)) from None

    stats = repo.commit_info(heads[branch]).get("stats") or {}
    say("完成", got_objs, total_objs)
    return {"path": dest, "name": repo.name, "branch": branch, "head": heads[branch],
            "branches": sorted(heads), "objects": got_objs, "files": stats.get("files") or 0,
            "bytes": stats.get("bytes") or 0, "peer": url,
            "peer_name": peer_name}


def clone_url_for(host: str, port: int = 8795) -> str:
    return "http://%s:%d" % (host, port)


def quote_project(name: str) -> str:
    return urllib.parse.quote(name or "")
