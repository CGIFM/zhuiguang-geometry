# -*- coding: utf-8 -*-
"""项目体检：把「这一天会踩的事」一次查完，每条都带一句「现在该做什么」。

和 fsck 的分工：
- fsck 查对象库（缺没缺、坏没坏），是**数据**有没有坏；
- doctor 查的是**人和流程**：有没有没记录的东西、对方推的版本是不是还压着没落地、
  上次恢复有没有断在一半、最新版本能不能完整打开、盘还剩多少、回收站和缓存是不是
  该清了、最近到底多久没记过一版。

只读。一个文件都不改（除了读）。
"""
from __future__ import annotations

import os
import shutil
import time

from .store import META_DIR, Repo, RepoError, fs_path, obj_cache_stats


def _has_any_file(root: str) -> bool:
    """工作区里到底有没有「本该被记录」的文件（只看有没有，找到第一个就停）"""
    stack = [fs_path(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name != META_DIR:
                                stack.append(fs_path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            return True
                    except OSError:
                        continue
        except OSError:
            continue
    return False


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GB" % n


def _fmt_time(ms):
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000.0))


def report(repo: Repo) -> dict:
    """{bad:[{msg,todo}], warn:[...], good:[...], head, branch, name, root}"""
    bad, warn, good = [], [], []

    def B(msg, todo=""):
        bad.append({"msg": msg, "todo": todo})

    def W(msg, todo=""):
        warn.append({"msg": msg, "todo": todo})

    def G(msg):
        good.append({"msg": msg})

    try:
        st = repo.status()
    except (RepoError, OSError) as e:
        st = None
        B("读不出工作区状态：%s" % e,
          "先关掉正在写这些文件的程序（SolidWorks）再看一次")

    head = (st or {}).get("head") or ""
    branch = (st or {}).get("branch") or repo.branch

    if st is not None:
        n = sum((st.get("counts") or {}).values())
        if n:
            c = st["counts"]
            B("有 %d 个文件还没记录（+%d ~%d -%d）"
              % (n, c["added"], c["modified"], c["deleted"]),
              '确认这些改动是要的就记一版：zgr snapshot "%s"'
              % time.strftime("%m-%d %H:%M"))
        else:
            G("工作区干净，和最新版本一致")

    if os.path.isfile(os.path.join(repo.meta, "PULL_NEEDED")):
        B("对方有新版本还没更新到工作区",
          "先处理它再记录（否则会把旧内容记成新版本）：网页里点「更新工作区」，"
          "或 zgr sync pull --url http://对方:8795")

    try:
        pend = repo.pending_restore()
    except (RepoError, OSError):
        pend = None
    if pend:
        tag = (pend.get("commit") or "?")[:10] if isinstance(pend, dict) else "?"
        B("上次恢复（%s）被中断了，工作区可能半新半旧" % tag,
          "收尾它（文件不会丢，先备份）：zgr recover %s" % repo.root)

    if head:
        try:
            from .sync import verify_complete
            verify_complete(repo, head)
            G("最新版本的文件内容都在本机（能完整打开）")
        except Exception as e:                            # noqa: BLE001
            B("最新版本缺文件内容：%s" % e,
              "从另一台机器补齐：zgr repair %s --url http://对方:8795" % repo.root)
        try:
            t = repo.commit_info(head).get("time") or 0
        except (RepoError, OSError):
            t = 0
        if t:
            days = (time.time() * 1000 - t) / 86400000.0
            if days >= 3:
                W("最近一次记录是 %s（%.0f 天前）" % (_fmt_time(t), days),
                  "自动记录没开的话，打开网页服务或 zgr watch %s" % repo.root)

    cfg = repo.config
    try:
        bk = repo.backup_total_bytes()
        cap = int(cfg.get("backup_max_mb", 2048) or 0) * 1024 * 1024
        if cap and bk > cap:
            W("回收站 %s，超过上限 %s" % (_fmt_size(bk), _fmt_size(cap)),
              "清掉最旧的几批：zgr backup --prune")
    except (RepoError, OSError):
        pass
    try:
        from . import cache as cache_mod
        pv = cache_mod.total_bytes(os.path.join(repo.meta, "previews"))
        ms = cache_mod.total_bytes(os.path.join(repo.meta, "meshes"))
        pcap = int(cfg.get("cache_max_mb", 2048) or 0) * 1024 * 1024
        if pcap and pv > pcap:
            W("预览缓存 %s，超过上限 %s（删了不影响版本）"
              % (_fmt_size(pv), _fmt_size(pcap)),
              "清一下：zgr cache --clear")
        if ms > 8 << 30:
            W("网格缓存已经 %s" % _fmt_size(ms), "清一下：zgr cache --clear")
    except (RepoError, OSError, ImportError):
        pass

    # 长路径：Windows 普通 API 的上限是 260 个字符。程序自己已经用 \\?\ 前缀兜住了，
    # 但**别人家的程序**（SolidWorks、资源管理器、复制粘贴、备份软件）不会，
    # 某一天就会在打开某个零件时莫名其妙失败，而且没人会想到是路径太长。
    # 趁目录还能改的时候提前说出来。
    n_files = -1                  # -1 = 没扫描成功，不算数
    try:
        longest, longest_rel, n_long = 0, "", 0
        rels = repo.scan()
        n_files = len(rels)
        for rel in rels:
            L = len(os.path.join(repo.root, rel.replace("/", os.sep)))
            if L >= 260:
                n_long += 1
            if L > longest:
                longest, longest_rel = L, rel
        todo = ("把中间几层文件夹改短，或把项目挪到浅一点的盘上（比如 D:\\图纸\\项目名）。"
                "本程序自己已经能读写，但 SolidWorks 这类程序打开它们会失败。")
        if n_long:
            W("工作区有 %d 个文件路径超过 260 字符（最长 %d：%s）"
              % (n_long, longest, longest_rel[-80:]), todo)
        elif longest >= 240:
            W("工作区里最长的路径已经 %d 字符（%s），离 Windows 的 260 上限不远了"
              % (longest, longest_rel[-60:]), "现在改成本最低，越往后文件越多越难动")
    except (RepoError, OSError):
        pass

    # 忽略规则写过头：一条 "*" 或 "*.sldprt" 就能把整个项目排干净。
    # 现象是「记录成功，但一个文件都没记」——不说出来，没人会怀疑到规则上。
    try:
        if n_files == 0 and _has_any_file(repo.root):
            W("忽略规则把项目里的文件全排除了（共 %d 条规则），这样记下来的是个空版本"
              % len(repo.ignore_patterns),
              "到设置里看「忽略规则」，把写得太宽的（比如 * 或 *.sldprt）删掉")
    except (RepoError, OSError):
        pass

    try:
        free = shutil.disk_usage(repo.root).free
        if free < 2 << 30:
            B("这个盘只剩 %s" % _fmt_size(free),
              "先腾地方再干活（记录/备份/打包都写这个盘）")
        elif free < 10 << 30:
            W("这个盘只剩 %s" % _fmt_size(free), "留意一下，别等到满了再说")
        else:
            G("盘还剩 %s" % _fmt_size(free))
    except OSError:
        pass

    # 对象缓存的命中率：只在「服务里跑体检」时有意义（命令行是短命进程，计数永远是 0）。
    # 它的用处是排「打开项目为什么慢」——命中率很低说明缓存没吃到，或者对象一直在被换掉。
    try:
        cs = obj_cache_stats()
        seen = cs["hits"] + cs["misses"]
        if seen >= 50:
            G("对象缓存命中 %d%%（命中 %d / 未命中 %d / 淘汰 %d），占 %s（上限 %s）"
              % (int(round(100.0 * cs["hits"] / seen)), cs["hits"], cs["misses"],
                 cs["evicted"], _fmt_size(cs["bytes"]), _fmt_size(cs["max_bytes"])))
    except Exception:                       # 计数坏了也不该影响体检结论
        pass

    live = cfg.get("live") or {}
    if live.get("url") and live.get("enabled", True):
        who = live.get("peer_author") or live.get("peer_host") or "对方"
        G("实时同步接着 %s（%s）" % (live["url"], who))
    elif live.get("url"):
        W("实时同步配置了 %s，但现在是关着的" % live["url"],
          "要接着用：zgr live %s --url %s" % (repo.root, live["url"]))

    return {"ok": not bad, "name": repo.name, "root": repo.root,
            "branch": branch, "head": head,
            "bad": bad, "warn": warn, "good": good}
