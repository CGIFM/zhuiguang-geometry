"""追光几何 · 命令行"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from . import APP_NAME, __version__
from .registry import Registry
from .store import MARKS, META_DIR, Repo, RepoError
from .sync import SyncError, local_urls, sync as do_sync


def out(*a):
    print(*a)
    sys.stdout.flush()


BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> str:
    """无头渲染要用的浏览器：Edge 是 Windows 10/11 自带的，优先用它"""
    for c in BROWSER_CANDIDATES:
        if os.path.isfile(c):
            return c
    return ""


def find_repo(start=".") -> Repo:
    cur = os.path.abspath(start)
    while True:
        r = Repo(cur)
        if r.exists:
            return r
        parent = os.path.dirname(cur)
        if parent == cur:
            return Repo(os.path.abspath(start))
        cur = parent


def fmt_time(ms):
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%.1f GB" % n


# --------------------------------------------------------------------- 命令
def cmd_init(a):
    r = Repo(a.dir).init(a.name)
    entry = Registry().add(r.root, a.name or r.name)
    out("已创建项目「%s」" % entry["name"])
    out("  目录：%s" % entry["path"])
    out("  元数据：%s" % os.path.join(entry["path"], META_DIR))
    if a.watch:
        r.update_config(lambda c: c.update({"watch": True,
                                           "watch_interval": a.interval}))
        out("  自动记录：已开启（服务运行时生效，每 %d 秒检查）" % a.interval)


def cmd_demo(a):
    """建一个带两个版本的示例项目，装好就能点开试试（网页上「先看看示例项目」同一份）

    为什么不放进用户自己的目录：第一次上手的人不该拿正在做的图纸去试第一个按钮。
    """
    from .demo import make_demo
    r = make_demo()
    entry = Registry().add(r.root, r.name)
    log = r.log(limit=5)
    out("示例项目已就绪：%s" % entry["path"])
    out("  版本：%d 个（最新：%s）" % (len(log), (log[0]["message"] if log else "—")))
    out("  试这三件事：改一下「零件/侧板.sldprt」里的数字 → 看时间线上出现新改动 → "
        "在旧版本上「恢复这一版」")
    out("  不想留了：网页项目列表里右键「从列表移除」即可，文件不会被删")
    if getattr(a, "open", False):
        class _O:            # 复用 cmd_open 的「有服务就开网页、没有就起一个」
            pass
        o = _O()
        o.dir, o.port = r.root, 8795
        return cmd_open(o)
    out("  想在网页里打开：zgr open \"%s\"（或 zgr demo --open）" % entry["path"])
    return 0


def cmd_status(a):
    r = find_repo(a.dir)
    if not r.exists:
        raise RepoError("这里还不是追光几何项目：%s" % os.path.abspath(a.dir))
    st = r.status()
    out("项目：%s   (%s)" % (r.name, r.root))
    out("分支：%s   当前版本：%s" % (st["branch"], (st["head"] or "尚未记录")[:12]))
    if st["clean"]:
        out("状态：✓ 所有文件都已记录")
    else:
        out("状态：有未记录的改动  +%d  ~%d  -%d" % (
            st["counts"]["added"], st["counts"]["modified"], st["counts"]["deleted"]))
        for p in st["added"][:20]:
            out("  + %s" % p)
        for p in st["modified"][:20]:
            out("  ~ %s" % p)
        for p in st["deleted"][:20]:
            out("  - %s" % p)
        extra = sum(st["counts"].values()) - min(20, len(st["added"])) - \
            min(20, len(st["modified"])) - min(20, len(st["deleted"]))
        if extra > 0:
            out("  … 另有 %d 项" % extra)
    return 0 if st["clean"] else 1


def cmd_doctor(a):
    """一眼看清「这项目现在健不健康、下一步该做什么」。只读，不动任何文件。

    具体的检查项在 zgr/doctor.py（网页上的「体检」按钮用的是同一份）。
    这里只负责把它排成人看得懂的样子。
    """
    r = find_repo(a.dir)
    if not r.exists:
        raise RepoError("这里还不是追光几何项目：%s" % os.path.abspath(a.dir))
    from . import doctor as doctor_mod
    rep = doctor_mod.report(r)
    if getattr(a, "json", False):
        out(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    out("项目：%s（%s）" % (rep["name"], rep["root"]))
    out("分支：%s   最新版本：%s" % (rep["branch"], (rep["head"] or "尚未记录")[:12]))
    for item in rep["bad"]:
        out("✖ " + item["msg"])
        if item["todo"]:
            out("  → " + item["todo"])
    for item in rep["warn"]:
        out("! " + item["msg"])
        if item["todo"]:
            out("  → " + item["todo"])
    for item in rep["good"]:
        out("✔ " + item["msg"])
    if rep["bad"]:
        out("结论：%d 件事要先处理（上面打 ✖ 的）" % len(rep["bad"]))
        return 1
    if rep["warn"]:
        out("结论：能干活；顺手处理一下 %d 条提醒就行" % len(rep["warn"]))
        return 0
    out("结论：一切正常")
    return 0


def cmd_snapshot(a):
    r = find_repo(a.dir)
    if not r.exists:
        raise RepoError("这里还不是追光几何项目，请先 init")
    oid = r.commit(a.message or "", a.author)
    if not oid:
        out("没有变化，未产生新版本。")
        return 0
    c = r.commit_info(oid)
    out("✓ 已记录为版本 %s" % oid[:12])
    out("  %s" % c["message"])
    out("  %s · %d 个文件 · %s" % (fmt_time(c["time"]), c["stats"]["files"],
                                  fmt_size(c["stats"]["bytes"])))
    sk = [x for x in (getattr(r, "last_skipped", []) or []) if x.get("path")]
    if sk:
        out("  注意：%d 个文件正在被写入，本次没有收录：%s"
            % (len(sk), "、".join(x["path"] for x in sk[:5])))
    return 0


def cmd_log(a):
    r = find_repo(a.dir)
    commits = r.log(a.limit)
    if not commits:
        out("还没有任何记录。")
        return 0
    want_changes = bool(getattr(a, "changes", False))
    if want_changes:
        trees = {c["id"]: c.get("tree") for c in commits}
    for c in commits:
        mark = {"red": "●", "yellow": "●", "green": "●"}.get(c.get("mark") or "", " ")
        tag = (" [" + ",".join(c["tags"]) + "]") if c.get("tags") else ""
        extra = ""
        if want_changes:
            # 和网页时间线上那个「改了 3 个文件」是同一个数：只比树、不展开整棵树
            parent = (c.get("parents") or [None])[0]
            if parent and parent not in trees:
                try:
                    trees[parent] = r.commit_info(parent).get("tree")
                except RepoError:
                    parent = None
            try:
                ad, mo, de = r.tree_diff_counts(trees.get(parent) if parent else None,
                                                c.get("tree"))
                extra = "  (改 %d：+%d ~%d -%d)" % (ad + mo + de, ad, mo, de)
            except RepoError:
                extra = "  (改 ?)"
        out("%s %s  %s  %-10s %s%s%s" % (
            mark, c["id"][:10], fmt_time(c["time"]),
            "%d文件" % (c.get("stats") or {}).get("files", 0),
            (c.get("message") or "").splitlines()[0], tag, extra))
    return 0


def cmd_today(a):
    """今天记了哪些版本、各是谁记的（网页时间线上方那一行的命令行版）

    站会、交接时最常问的就是「昨天到今天改了些什么」。
    """
    r = find_repo(a.dir)
    if not r.exists:
        out("这个目录不是追光几何项目（没有 .zgr）：%s" % os.path.abspath(a.dir))
        return 2
    limit = max(1, int(a.limit or 0))
    scan = max(1, min(int(a.scan or 0), 50000))
    today = time.strftime("%Y-%m-%d")

    def day_of(c):
        return time.strftime("%Y-%m-%d",
                             time.localtime((c.get("time") or 0) / 1000.0))

    # 今天到底几版：从分支头往回数到零点为止（不是数「最近翻到的这一屏」——
    # 一天记了几百版、或者历史有好几千版时，那一屏根本数不全）
    since = int(time.mktime(tuple(time.localtime()[:3]) + (0, 0, 0, 0, 0, -1)) * 1000)
    # 上界取「明天零点」：对端时钟跑到未来的提交不该算进今天（+86400 秒再取当天零点，
    # 跨月、跨年都自然正确）
    until = int(time.mktime(tuple(time.localtime(time.time() + 86400)[:3])
                            + (0, 0, 0, 0, 0, -1)) * 1000)
    # 一次走完今天的全部版本（不是数「最近翻到的这一屏」——一天记了几百版、
    # 历史又有好几千版时，那一屏根本数不全）
    rows, cut = r.commits_since(since, until, cap=scan)
    count = len(rows)
    if not count:
        out("今天还没有记录（看更早的：zgr log）")
        return 0
    by = {}
    for c in rows:
        who = (c.get("author") or "").strip() or "（没记名字）"
        by[who] = by.get(who, 0) + 1
    who_s = " / ".join("%s %d" % (k, v) for k, v in
                       sorted(by.items(), key=lambda kv: -kv[1]))
    # 只有连「数一遍今天」都撞到上限时才说「至少」；正常情况就是准数
    head = ("今天 至少 %d 个版本" if cut else "今天 %d 个版本") % count
    if getattr(a, "md", False):
        # 粘到群里用的：一行标题 + 一版一行，不带表格和色块
        out("**%s%s**" % (head, (" · " + who_s) if rows else ""))
        if cut:
            out("（--scan 上限截断了，实际比这个多）")
        for c in rows[:limit]:
            t = time.localtime((c.get("time") or 0) / 1000.0)
            hm = time.strftime("%H:%M", t)
            msg = ((c.get("message") or "").split("\n")[0]) or "(无说明)"
            who = (c.get("author") or "").strip()
            out("- %s %s%s" % (hm, msg, ("（%s）" % who) if who else ""))
        if len(rows) > limit:
            out("- …还有 %d 个（--limit 多看几个）" % (len(rows) - limit))
        return 0
    out(head + ((" · " + who_s) if rows else ""))
    for c in rows[:limit]:
        out("  %s  %-10s  %-8s  %s" % (
            fmt_time(c.get("time") or 0), c["id"][:10], c.get("author") or "",
            (c.get("message") or "").split("\n")[0]))
    if len(rows) > limit:
        out("  …还有 %d 个（--limit 多看几个）" % (len(rows) - limit))
    if cut:
        out("（--scan 上限截断了，实际比这个多：--scan 调大）")
    return 0


def cmd_report(a):
    """近 N 天：每天几版、谁记的（站会/周报用，`--md` 直接粘到群里）"""
    r = find_repo(a.dir)
    if not r.exists:
        out("这个目录不是追光几何项目（没有 .zgr）：%s" % os.path.abspath(a.dir))
        return 2
    days = max(1, min(int(a.days if a.days is not None else 7), 366))
    cap = max(1, min(int(a.scan or 0), 200000))
    now = time.time()
    today0 = time.mktime(tuple(time.localtime()[:3]) + (0, 0, 0, 0, 0, -1))
    since = time.mktime(tuple(time.localtime(today0 - (days - 1) * 86400)[:3])
                        + (0, 0, 0, 0, 0, -1))
    until = time.mktime(tuple(time.localtime(today0 + 86400)[:3]) + (0, 0, 0, 0, 0, -1))
    rows, cut = r.commits_since(int(since * 1000), int(until * 1000), cap=cap)
    span = "%s ~ %s" % (time.strftime("%m-%d", time.localtime(since)),
                        time.strftime("%m-%d", time.localtime(now)))
    if not rows:
        out("近 %d 天（%s）没有记录" % (days, span))
        return 0
    by_day = {}
    for c in rows:
        d = time.strftime("%m-%d", time.localtime((c.get("time") or 0) / 1000.0))
        by_day.setdefault(d, []).append(c)
    total_by = {}
    for c in rows:
        who = (c.get("author") or "").strip() or "（没记名字）"
        total_by[who] = total_by.get(who, 0) + 1
    total_s = " / ".join("%s %d" % (k, v) for k, v in
                         sorted(total_by.items(), key=lambda kv: -kv[1]))
    head = "近 %d 天（%s）· %s%d 个版本 · %s" % (
        days, span, "至少 " if cut else "", len(rows), total_s)
    if getattr(a, "md", False):
        out("**%s**" % head)
    else:
        out(head)
    for d in sorted(by_day, reverse=True):
        lst = by_day[d]
        per = {}
        for c in lst:
            who = (c.get("author") or "").strip() or "（没记名字）"
            per[who] = per.get(who, 0) + 1
        per_s = " / ".join("%s %d" % (k, v) for k, v in
                           sorted(per.items(), key=lambda kv: -kv[1]))
        if getattr(a, "md", False):
            out("- %s  %d 版 · %s" % (d, len(lst), per_s))
        else:
            out("  %s  %3d 版  %s" % (d, len(lst), per_s))
    if cut:
        out("（--scan %d 上限截断了，实际比这个多：调大 --scan）" % cap)
    return 0


def cmd_cache(a):
    """看 / 清缓存：缩略图、DWG 预览、3D 网格，删了不影响任何版本"""
    from . import cache as cache_mod
    r = find_repo(a.dir)
    prev = (r.size_report(budget=30).get("previews") or {})
    mesh = (r.size_report(budget=30).get("meshes") or {})
    out("预览缓存：%d 个文件，%s" % (prev.get("files", 0), fmt_size(prev.get("bytes", 0))))
    out("网格缓存：%d 个文件，%s" % (mesh.get("files", 0), fmt_size(mesh.get("bytes", 0))))
    if a.prune:
        # 按体积删到上限以内（最旧的先删，10 分钟内生成的不动）
        pmb = a.max_mb
        mmb = a.mesh_max_mb if a.mesh_max_mb is not None else a.max_mb
        res = cache_mod.prune_repo(
            r, dry=bool(a.dry),
            preview_mb=(pmb if pmb is not None else None),
            mesh_mb=(mmb if mmb is not None else None))
        for kind, label in (("previews", "预览缓存"), ("meshes", "网格缓存")):
            d = res.get(kind) or {}
            if d is None:
                out("  %s：已关闭自动清理（cache_auto=false），这次没动" % label)
                continue
            out("  %s：%s %d 个（释放 %s），现在 %s"
                % (label, "将删" if a.dry else "删掉", len(d.get("removed") or []),
                   fmt_size(d.get("bytes") or 0), fmt_size(d.get("total") or 0)))
        if a.dry:
            out("  （--dry：只是算了一遍，什么都没删）")
        return 0
    if not a.clear:
        out("  （都是能重建的缓存，删了下次看图/转模型会重新生成；"
            "要按上限清用 --prune，全清用 --clear）")
        return 0
    for d in ("previews", "meshes"):
        p = os.path.join(r.meta, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    out("  ✓ 已清理（版本历史、图纸本身一个字节都没动）")
    return 0


def cmd_show(a):
    """看某一版里到底有哪些文件（脚本里用 --json 拿结构化结果）"""
    r = find_repo(a.dir)
    ref = (a.commit or "").strip()
    if ref.lower() in ("head", "@", "latest", "最新"):
        oid = r.head_oid()
        if not oid:
            raise RepoError("这个项目还没有任何版本")
    else:
        oid = r.resolve(ref)
    c, files = r.files_of(oid)
    c = c or {}
    rows = sorted(files.items())
    if a.file:
        kw = a.file.lower()
        rows = [(p, v) for p, v in rows if kw in p.lower()]
    if a.json:
        out(json.dumps({"commit": oid, "message": c.get("message"),
                        "time": c.get("time"), "author": c.get("author"),
                        "files": [{"path": p, "size": v["size"], "oid": v["oid"]}
                                  for p, v in rows]}, ensure_ascii=False))
        return 0
    out("版本 %s  %s" % (oid[:12], (c.get("message") or "").splitlines()[0]))
    out("  %s · %s · %d 个文件 · %s" % (
        fmt_time(c.get("time")), c.get("author") or "-", len(files),
        fmt_size(sum(v["size"] for v in files.values()))))
    if not rows:
        out("  （没有匹配的文件）")
        return 0
    shown = rows[:a.limit] if a.limit else rows
    for p, v in shown:
        out("  %9s  %s" % (fmt_size(v["size"]), p))
    if len(shown) < len(rows):
        out("  …还有 %d 个（-n 0 看全部）" % (len(rows) - len(shown)))
    return 0


def cmd_diff(a):
    r = find_repo(a.dir)
    d = r.diff(a.a, a.b)
    if not a.file:
        out("版本对比 %s → %s" % (a.a[:10], a.b[:10]))
        out("  +%d  -%d  ~%d" % (d["counts"]["added"], d["counts"]["deleted"],
                                 d["counts"]["modified"]))
        for x in d["added"]:
            out("  + %s  (%s)" % (x["path"], fmt_size(x["size"])))
        for x in d["deleted"]:
            out("  - %s  (%s)" % (x["path"], fmt_size(x["size"])))
        for x in d["modified"]:
            out("  ~ %s  (%s → %s)" % (x["path"], fmt_size(x["old_size"]),
                                       fmt_size(x["new_size"])))
        return 0
    res = r.commit_diff_text(a.a, a.b, a.file)
    if res.get("binary"):
        out("[%s] %s" % (a.file, res["reason"]))
        return 0
    for line in res["lines"]:
        out(line)
    return 0


def cmd_restore(a):
    """恢复历史版本到工作区。

    「恢复」是这个软件里最容易「点错了就没了」的动作：工作区里还没记录的东西会被
    **这一版的内容**盖掉（有备份，但人往往不知道备份在哪）。所以：
    - `--dry` 先算清楚：覆盖几个、新增几个、删掉几个、有几个未记录的改动；
    - 工作区有未记录改动时，不加 `--yes` 只报计划、不落盘；
    - 目标目录不是项目本身（导出到别处）时照旧，不多问。
    """
    r = find_repo(a.dir)
    own = os.path.normcase(os.path.abspath(a.target or r.root)) ==         os.path.normcase(os.path.abspath(r.root))
    plan = None
    if a.dry or own:
        plan = r.restore_plan(a.commit, a.target, mirror=not a.no_mirror)
    if a.dry:
        out("演练（一个文件都没动）：恢复到 %s" % a.commit[:12])
        out("  覆盖 %d 个、新增 %d 个、删除 %d 个、一样 %d 个"
            % (plan["overwrite"], plan["create"], plan["remove"], plan["same"]))
        if plan["dirty"]:
            out("  ⚠ 其中 %d 个文件有未记录的改动（恢复后只剩备份里的那一份）"
                % plan["dirty"])
            for d in plan["dirty_sample"][:8]:
                out("     %s  %s" % (d["kind"], d["path"]))
        out("  这是一个只读预演；真要恢复就去掉 --dry")
        return 0
    if own and plan["dirty"] and not getattr(a, "yes", False):
        out("工作区有 %d 个文件还没记录，恢复会覆盖/删掉它们：" % plan["dirty"])
        for d in plan["dirty_sample"][:8]:
            out("   %s  %s" % (d["kind"], d["path"]))
        out("")
        out("先看一眼会动什么：   zgr restore %s %s --dry" % (a.dir, a.commit))
        out("确实要覆盖：         zgr restore %s %s --yes   （覆盖前的文件会备份到回收站）"
            % (a.dir, a.commit))
        return 1
    res = r.restore(a.commit, a.target, mirror=not a.no_mirror, backup=not a.no_backup)
    out("✓ 已恢复到版本 %s" % a.commit[:12])
    out("  目录：%s" % res["target"])
    out("  写入 %d 个文件，移除 %d 个" % (res["written"], len(res["removed"])))
    if res["backup"]:
        out("  覆盖前的文件已备份到：%s" % res["backup"])
    return 0


def cmd_export(a):
    r = find_repo(a.dir)
    res = r.restore(a.commit, a.target, mirror=False, backup=False)
    out("✓ 已导出 %d 个文件到 %s" % (res["written"], res["target"]))
    return 0


def cmd_mark(a):
    r = find_repo(a.dir)
    mark = None if a.color in ("none", "clear", "无") else a.color
    if mark and mark not in MARKS:
        raise RepoError("颜色只能是 red / yellow / green / none")
    res = r.set_mark(a.commit, mark, a.tags.split(",") if a.tags else None)
    out("✓ 已更新 %s 的标记：%s %s" % (
        a.commit[:10], res.get("mark") or "无",
        ("标签 " + ",".join(res["tags"])) if res.get("tags") else ""))
    return 0


def watch_step(r, message=""):
    """自动记录的一步：该记就记，返回提交号；不该记返回 None。

    有 `PULL_NEEDED`（对方的新版本还没更新到工作区）时**绝不能记**：
    这时记下去的就是「把对方改动改回去」的版本，还会被快进推回对方。
    """
    if os.path.isfile(os.path.join(r.meta, "PULL_NEEDED")):
        return None
    st = r.status()
    if st["clean"]:
        return None
    return r.commit(message or ("自动记录 %s" % time.strftime("%m-%d %H:%M")))


def cmd_watch(a):
    r = find_repo(a.dir)
    if not r.exists:
        raise RepoError("这里还不是追光几何项目")
    out("自动记录中：%s（每 %d 秒检查，Ctrl+C 结束）" % (r.name, a.interval))
    warned = False
    try:
        while True:
            time.sleep(max(3, a.interval))
            if os.path.isfile(os.path.join(r.meta, "PULL_NEEDED")):
                if not warned:
                    out("⏸ 有对方的新版本还没更新到工作区，自动记录先暂停；"
                        "在网页里处理「待更新」后会自动继续")
                    warned = True
                continue
            warned = False
            st = r.status()
            oid = watch_step(r, a.message or "")
            if oid:
                out("✓ %s  +%d ~%d -%d" % (oid[:10], st["counts"]["added"],
                                           st["counts"]["modified"],
                                           st["counts"]["deleted"]))
    except KeyboardInterrupt:
        out("\n已停止自动记录。")
    return 0


def cmd_serve(a):
    from .server import serve
    # 桌面图标就是「serve --open」：服务已经在跑时不要再起一个抢端口，
    # 直接把浏览器指过去就行
    if a.open and _already_serving(a.port):
        out("服务已在运行：http://127.0.0.1:%d/" % a.port)
        return 0
    serve(port=a.port, host=a.host, token=a.token, project=a.project,
          open_browser=a.open)
    return 0


def _already_serving(port: int) -> bool:
    """该端口上是不是已经有一个本软件在跑（是的话顺手打开浏览器）"""
    import urllib.request
    import webbrowser
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port,
                                    timeout=0.8) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    if info.get("app") != APP_NAME:
        return False
    webbrowser.open("http://127.0.0.1:%d/" % port)
    return True


def cmd_sync(a):
    r = find_repo(a.dir)
    if getattr(a, "remote", None):
        from .remotes import sync_remote
        res = sync_remote(r, a.remote, push=a.push, pull=a.pull,
                          branch=a.branch, checkout=a.checkout, force=a.force,
                          log=lambda m: out("  " + str(m)))
    else:
        res = do_sync(r, a.url or "", push=a.push, pull=a.pull, token=a.token,
                      branch=a.branch, checkout=a.checkout, force=a.force,
                      share=a.share or "", project=a.remote_project or "",
                      log=lambda m: out("  " + str(m)))
    out("")
    for m in res["messages"]:
        out("· %s" % m)
    out("当前版本：%s" % (res["head"] or "空")[:12])
    return 0


def cmd_repair(a):
    """把对方当备份：缺的、坏的对象按对象号补齐（不动分支头、不动图纸）"""
    from .sync import repair as do_repair
    r = find_repo(a.dir)
    res = do_repair(r, a.url or "", token=a.token, branch=a.branch,
                    share=a.share or "", project=a.remote_project or "",
                    deep=not a.quick, log=lambda m: out("  " + str(m)))
    out("")
    out("补回 %d 个对象，移除坏对象 %d 个" % (res["fetched"], res["removed_bad"]))
    if res["ok"]:
        out("✔ 补完了，体检通过。")
        return 0
    for p in res["problems"]:
        out("✖ " + p)
    return 1


def cmd_remote(a):
    """管理远端：增删查、自检"""
    from . import remotes as R
    r = find_repo(a.dir)
    act = a.action
    if act == "list":
        items = R.list_remotes(r)
        if not items:
            out("还没有配置远端。加一个，例如：")
            out("  zgr remote add 公司NAS --kind webdav --url http://nas.local:5005/dav "
                "--user jlc --password ***")
            out("  zgr remote add 备份盘 --kind local --path \\\\NAS\\备份\\追光")
            out("  zgr remote add 图床 --kind s3 --endpoint https://s3.us-east-1.amazonaws.com "
                "--bucket zg --region us-east-1 --access-key AK --secret-key ***")
            return 0
        out("已配置 %d 个远端：" % len(items))
        for it in items:
            target = it.get("path") or it.get("url") or it.get("bucket") or ""
            extra = ""
            if it["kind"] == "s3":
                extra = "  bucket=%s region=%s" % (target, it.get("region") or "")
            elif it["kind"] == "webdav":
                extra = "  user=%s" % (it.get("user") or "-")
            mark = "（已存密码）" if it.get("has_secret") else "（无密码）"
            out("  %-12s %-8s %s%s%s" % (it["name"], it["kind"], target, extra, mark))
        return 0

    if act == "add":
        conf = {"name": a.name, "kind": a.kind, "url": a.url or "",
                "path": a.path or "", "user": a.user or "",
                "bucket": a.bucket or "", "endpoint": a.endpoint or "",
                "region": a.region or "", "prefix": a.prefix or "",
                "project": a.remote_project or ""}
        if a.no_path_style:
            conf["path_style"] = False
        secret = {}
        if a.password:
            secret["password"] = a.password
        if a.access_key:
            secret["access_key"] = a.access_key
        if a.secret_key:
            secret["secret_key"] = a.secret_key
        if a.token:
            secret["token"] = a.token
        R.save_remote(r, conf, secret=secret or None)
        out("已添加远端「%s」（%s）" % (a.name, a.kind))
        if not a.no_test:
            out("")
            _print_test(R.test_remote(r, a.name))
        return 0

    if act in ("rm", "remove", "del"):
        R.remove_remote(r, a.name)
        out("已删除远端「%s」" % a.name)
        return 0

    if act == "test":
        names = [a.name] if a.name else [x["name"] for x in R.list_remotes(r)]
        if not names:
            out("还没有配置远端")
            return 1
        bad = 0
        for n in names:
            out("检查「%s」…" % n)
            try:
                _print_test(R.test_remote(r, n))
            except SyncError as e:
                bad += 1
                out("  × %s" % e)
        return 1 if bad else 0

    raise SyncError("不认识的 remote 子命令：%s" % act)


def _print_test(d):
    if d.get("kind") == "http":
        out("  √ 连上了「%s」，分支 %d 个" % (d.get("remote") or "对端", d.get("branches") or 0))
    else:
        out("  √ 读写正常：%s（已有对象 %d 个）" % (d.get("store"), d.get("objects") or 0))


def _remote(r, a):
    from .sync import make_backend
    from .sync import verify_complete
    if getattr(a, "remote", None):
        from .remotes import make_remote_backend
        be = make_remote_backend(r, a.remote)
    else:
        be = make_backend(url=a.url or "", share=a.share or "",
                          project=a.remote_project or r.name, token=a.token)
    refs = be.refs()
    branches = refs.get("branches") or {}
    branch = a.branch or r.branch
    if branches and branch not in branches:
        branch = refs.get("default") or next(iter(branches))
    head = branches.get(branch)
    if not head:
        raise SyncError("对方分支 %s 还没有任何版本" % branch)
    need = [o for o in be.manifest(branch, r.head_oid(branch)) if not r.has(o)]
    if need:
        out("先取回 %d 个对象…" % len(need))
        be.fetch(r, need, log=lambda m: out("  " + str(m)))
    verify_complete(r, head)
    return head, branch, refs.get("name") or a.url or a.share


def cmd_merge(a):
    from . import merge as M
    r = find_repo(a.dir)
    head, branch, name = _remote(r, a)
    p = M.plan(r, head, detail=True)
    out("合并来源：%s（分支 %s，%s）" % (name, branch, head[:12]))
    out("共同祖先：%s" % (p["base"][:12] if p["base"] else "无"))
    for c in M.remote_log(r, head, p.get("base")):
        out("  对方提交 %s %s" % (c["id"][:10], (c["message"] or "").split("\n")[0]))
    out("")
    for e in p["entries"]:
        if e["status"] == "same":
            continue
        out("  %-10s %s" % (e["status"], e["path"]))
    # 「撞车」= 同一个零件两边都动过。光说「有冲突」不够：得说清另一头是谁改的，
    # 否则用户要么硬合、要么干脆不合，两种都是在赌。
    detail = p.get("clash_detail") or []
    if detail:
        out("\n⚠ 撞车 %d 个（同一个文件两边都动过）：" % len(detail))
        for d in detail[:20]:
            out("  %s" % d["path"])
            for side, label in (("ours", "本机"), ("theirs", "对方")):
                x = d.get(side)
                if not x:
                    out("    %s：没有改动记录" % label)
                    continue
                when = ""
                if x.get("time"):
                    when = time.strftime("%m-%d %H:%M",
                                         time.localtime(x["time"] / 1000.0))
                out("    %s：%s %s %s" % (label, x.get("author") or "（没写名字）",
                                          when, x.get("message") or ""))
        if len(detail) > 20:
            out("  ……其余 %d 个在网页上能看全" % (len(detail) - 20))
    if p["conflicts"]:
        out("\n冲突 %d 个，默认保留本机版本。" % len(p["conflicts"]))
    if a.dry_run:
        out("\n（--dry-run 只看不动）")
        return 0
    res = M.apply(r, p, choices={}, message=a.message,
                  checkout=not a.no_checkout)
    out("\n已合并为 %s（%d 个文件）" % (res["commit"][:12], res["files"]))
    return 0


def cmd_undo(a):
    from . import merge as M
    r = find_repo(a.dir)
    res = M.undo(r, a.commit, checkout=not a.no_checkout)
    out("已退回到 %s" % res["commit"][:12])
    return 0


def cmd_pack(a):
    from . import live as L
    r = find_repo(a.dir)
    res = L.pack(r, commit=a.commit or None, worktree=a.worktree, prefix=a.prefix or "")
    out("已打包 %d 个文件（%.1f MB）" % (res["files"], res["size"] / 1048576.0))
    out(res["file"])
    return 0


def cmd_backup(a):
    from . import live as L
    r = find_repo(a.dir)
    if getattr(a, "prune", False):
        max_mb = getattr(a, "max_mb", 0) or 0
        res = r.prune_backups(keep=getattr(a, "keep", 20) or 20,
                              dry=getattr(a, "dry", False),
                              max_bytes=(max_mb * 1024 * 1024) if max_mb else None)
        out("保留最新 %d 批，删掉 %d 批，释放 %s%s"
            % (res["kept"], len(res["removed"]), fmt_size(res["bytes"]),
               "（演练，未真删）" if res["dry"] else ""))
        return 0
    if getattr(a, "restore_file", ""):
        res = L.restore_backup_file(r, a.restore_file, getattr(a, "file", "") or "",
                                    backup=not getattr(a, "no_backup", False))
        out("已从 %s 复原 %s（%s）"
            % (res["name"], res["file"], fmt_size(res["size"])))
        if res.get("safety_backup"):
            out("  覆盖前的版本已另存一批备份：%s（恢复错了还能再恢复回来）"
                % res["safety_backup"])
        out("  这只是工作区的文件，想留成版本还要再记录一次。")
        return 0
    if getattr(a, "files", ""):
        d = L.backup_files(r, a.files, limit=getattr(a, "limit", 500) or 500)
        out("%s：共 %d 个文件%s" % (
            d["name"], d["total"],
            "（这里只列前 %d 个，用 --limit 看更多）" % len(d["files"])
            if d["truncated"] else ""))
        for x in d["files"]:
            out("  %-58s %9s  %s" % (x["path"], fmt_size(x["size"]), fmt_time(x["time"])))
        out("\n只恢复其中一个：zgr backup --restore-file %s --file <上面的路径>" % d["name"])
        return 0
    if a.restore:
        res = L.restore_backup(r, a.restore)
        out("已从 %s 复原 %d 个文件" % (a.restore, res["count"]))
        return 0
    rows = L.list_backups(r)
    if not rows:
        out("没有备份。")
        return 0
    for b in rows:
        out("%-24s %5d 个文件 %9s" % (b["name"], b["files"], fmt_size(b["bytes"])))
        if b["sample"]:
            out("    " + "、".join(b["sample"][:6]))
    return 0


def cmd_search(a):
    """跨所有版本按文件名找零件（和界面上的「🔍 找零件」同一套索引）"""
    r = find_repo(a.dir)
    t0 = time.time()
    res = r.search_history(a.q, limit=a.limit)
    if not res["total"]:
        out("没有匹配（扫过 %d 个文件，用时 %.1f 秒）" % (res["scanned"], time.time() - t0))
        return 0
    out("匹配 %d 个（列出 %d 个，扫过 %d 个文件，用时 %.1f 秒）"
        % (res["total"], len(res["rows"]), res["scanned"], time.time() - t0))
    for row in res["rows"]:
        out("%s %-52s %2d 次改动  %8s  %s" % (
            "  " if row["in_head"] else "✗ ", row["path"], row["changes"],
            fmt_size(row["size"]), fmt_time(row["last"])))
    if not all(x["in_head"] for x in res["rows"]):
        out("（✗ 表示当前版本里已经没有这个文件了）")
    return 0


def cmd_find(a):
    """在**全部历史**里按记录说明找版本（和网页搜索框同一件事）

    网页上一次只画几百个版本，搜索却要翻全部分支——命令行这里也照同样的口径，
    否则「网页搜得到、命令行搜不到」，用起来只会让人怀疑数据丢了。
    """
    r = find_repo(a.dir)
    if not r.exists:
        # 不说清楚的话，这里会一路走到「全部 0 个版本里都没有提到…」，
        # 看起来像项目里一条记录都没有，其实根本不是个项目
        out("这个目录不是追光几何项目（没有 .zgr）：%s" % os.path.abspath(a.dir))
        return 2
    q = (a.q or "").strip().lower()
    if not q:
        out("请给一个关键词，例如：zgr find 客户确认")
        return 2
    # 参数兜底：`--limit -2` 会变成 hits[:-2]（少列两条还嘴硬），
    # `--limit 0` 列表空着却退 0；`--scan` 给个天文数字就真去走那么多
    limit = max(1, int(a.limit or 0))
    scan = max(1, min(int(a.scan or 0), 50000))
    t0 = time.time()
    # 多要一条：正好等于 scan 时说不清「还有没有更早的」，多取一条就说得清
    commits, _heads = r.log_graph(scan + 1)
    limited = len(commits) > scan
    if limited:
        commits = commits[:scan]

    def why_of(c):
        """靠哪一项对上的：说明 / 版本号 / 记录人（和网页同一口径）

        只比说明的**第一行前 500 个字**：网页那边索引也只存这么多，
        两边比的范围不一样，就会出现「命令行搜得到、网页搜不到」。
        """
        why = []
        first = (c.get("message") or "").split("\n")[0][:500]
        if c["id"].startswith(q):
            why.append("版本号")
        if q in first.lower():
            why.append("说明")
        if q in (c.get("author") or "").lower():
            why.append("记录人")
        return "+".join(why)

    hits = []
    for c in commits:
        how = why_of(c)
        if how:
            hits.append((c, how))
    took = time.time() - t0
    if a.json:
        out(json.dumps({
            "ok": True, "scanned": len(commits), "scan_limited": limited,
            "hits": [{"id": c["id"], "message": (c.get("message") or "").split("\n")[0],
                      "time": c.get("time") or 0, "author": c.get("author") or "",
                      "how": how}
                     for c, how in hits[:limit]],
        }, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        out("全部 %d 个版本里都没有提到「%s」（说明、记录人、版本号都比过了）%s（用时 %.1f 秒）" % (
            len(commits), a.q, "（只翻了最近的一部分）" if limited else "", took))
        return 0
    out("全部 %d 个版本里找到 %d 个%s（列出 %d 个，用时 %.1f 秒）" % (
        len(commits), len(hits), "（只翻了最近的一部分）" if limited else "",
        min(len(hits), limit), took))
    for c, how in hits[:limit]:
        msg = (c.get("message") or "").split("\n")[0]
        # 靠记录人对上的那几行，说明里看不到关键词：点一句省得以为搜错了
        tail = "  ← 按记录人找到" if (how == "记录人") else ""
        out("%s  %-10s  %-6s  %s%s" % (fmt_time(c.get("time") or 0), c["id"][:8],
                                       c.get("author") or "", msg, tail))
    if len(hits) > limit:
        out("（还有 %d 个没列出来，加 --limit 多看几个）" % (len(hits) - limit))
    return 0


def cmd_size(a):
    """项目体积账本：图纸 / 历史 / 回收站 / 打包 / BOM 快照"""
    r = find_repo(a.dir)
    rep = r.size_report()
    rows = [("图纸文件", rep["worktree"]), ("版本历史", rep["objects"]),
            ("回收站", rep["backup"]), ("预览缓存", rep.get("previews") or {}),
            ("打包", rep["packs"]),
            ("BOM 快照", rep["bom"]), ("其它", rep["meta_other"])]
    out("项目：%s" % r.root)
    for name, d in rows:
        out("  %-8s %9s  %s" % (name, fmt_size(d["bytes"]),
                                ("%d 批" % d["batches"]) if "batches" in d
                                else ("%d 个文件" % d["files"])))
    out("  合计     %9s%s" % (fmt_size(rep["total"]),
                              "（只数了一部分）" if rep["partial"] else ""))
    out("能清的是「回收站」「预览缓存」和「版本历史」里没人引用的那部分："
        "zgr backup --prune / zgr cache --clear / zgr gc")
    return 0


def cmd_gc(a):
    """回收没人引用的对象：默认先演练，--yes 才真删"""
    r = find_repo(a.dir)
    plan = r.gc(dry=True, keep_days=a.keep_days)
    if not plan["total"]:
        out("没有可回收的对象（孤儿 %d 个，其中 %d 个是最近 %d 天内的、"
            "%d 个挂在打了标记的版本上，都不动）"
            % (plan["dangling"], plan["kept_recent"], plan["keep_days"],
               plan.get("kept_marked", 0)))
        return 0
    out("可以回收 %d 个对象，省出 %s（最近 %d 天内的 %d 个不动）"
        % (plan["total"], fmt_size(plan["bytes"]), plan["keep_days"], plan["kept_recent"]))
    for o in plan["sample"][:6]:
        out("    " + o)
    if a.dry or not a.yes:
        out("这是演练，一个都没删。真要回收加 --yes（这些版本在任何分支上都找不到了）")
        return 0
    rep = r.gc(dry=False, keep_days=a.keep_days)
    out("✔ 已回收 %d 个对象，省出 %s" % (rep["removed"], fmt_size(rep["bytes"])))
    return 0


def cmd_fsck(a):
    """体检：只读，不会改动任何图纸"""
    r = find_repo(a.dir)
    t0 = time.time()
    rep = r.fsck(deep=a.deep)
    out("对象 %d 个%s" % (rep["objects"], "（已逐个重算校验和）" if a.deep else "（只查了在不在）"))
    if rep["missing"]:
        out("✖ 缺对象 %d 个：" % len(rep["missing"]))
        for o in rep["missing"][:10]:
            out("    " + o)
    if rep["bad"]:
        out("✖ 坏对象 %d 个：" % len(rep["bad"]))
        for b in rep["bad"][:10]:
            out("    %s  %s" % (b["oid"], b["why"]))
    if rep["errors"]:
        for e in rep["errors"][:10]:
            out("✖ " + e)
    for w in (rep.get("warnings") or []):
        out("! " + w)
    if rep["dangling"]:
        out("· 孤儿对象 %d 个（没人引用，多半是以前中断的记录，不碍事）" % len(rep["dangling"]))
    if rep["ok"]:
        out("✔ 一切正常（用时 %.1f 秒）" % (time.time() - t0))
        return 0
    out("✖ 体检没通过（用时 %.1f 秒）。图纸一个字节都没动。"
        "缺的/坏的对象可以从另一台机器同步补回来：" % (time.time() - t0))
    out("    zgr sync pull --url http://另一台:8795 --project %s" % os.path.basename(r.root))
    return 1


def cmd_recover(a):
    r = find_repo(a.dir)
    j = r.pending_restore()
    if not j:
        out("没有被打断的恢复，不用处理。")
        return 0
    if getattr(a, "dismiss", False):
        r.clear_pending_restore()
        out("已清掉提示（文件没动）。")
        return 0
    if j.get("corrupt"):
        out("上次恢复被打断，但恢复记号读不出来（不知道恢复到哪个版本）。")
        out("建议先跑：zgr fsck %s，确认对象都在再决定是否重新恢复。" % a.dir)
        out("确认无误后：zgr recover %s --dismiss" % a.dir)
        return 1
    out("上次恢复到 %s 做到一半被打断了（%s）。" % (j["commit"][:12], j.get("target")))
    res = r.finish_pending_restore(a.mode)
    if res.get("mode") == "backup":
        out("已从备份拷回 %d 个文件。" % res["restored"])
    else:
        out("已重新恢复，写了 %d 个文件，删掉 %d 个。" % (res.get("written", 0),
                                                     len(res.get("removed") or [])))
    return 0


def cmd_live(a):
    from . import live as L
    r = find_repo(a.dir)
    mode = L.MODE_SPLIT if a.split else L.MODE_SHARED
    if a.split:
        note = L.ensure_personal_branch(r)
        out("一人一条线：我在分支「%s」上（%s）"
            % (note["branch"], "从 %s 分出" % note["from"] if note.get("created")
               else "已存在"))
    w = L.LiveSync(r, a.url, token=a.token or "", interval=a.interval,
                   auto_pull=not a.no_pull, auto_push=not a.no_push,
                   auto_merge=not a.no_merge, checkout=not a.no_checkout,
                   mode=mode, logger=lambda m: out("· " + m))
    def _save(c):
        # 合并而不是整份替换：live 里还存着「对方是谁」「是不是自动配对拉起的」
        # 这些不属于本次命令行参数的东西，整份覆盖会把它们抹掉（界面随即显示成一串 IP）。
        live = dict(c.get("live") or {})
        live.update({"enabled": True, "url": a.url.rstrip("/"), "interval": a.interval,
                     "mode": mode,
                     "auto_pull": not a.no_pull, "auto_push": not a.no_push,
                     "auto_merge": not a.no_merge and mode == L.MODE_SHARED,
                     "checkout": not a.no_checkout})
        c["live"] = live
        if a.token:
            c["live_token"] = a.token
    r.update_config(_save)
    out("实时同步：%s ← %s（Ctrl+C 退出）" % (r.name, a.url))
    w.start()
    try:
        while w.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
        out("\n已停止。")
    return 0


def cmd_preview(a):
    from .thumbs import get_service
    from .store import Repo as R
    r = find_repo(a.dir)
    ap = a.file if os.path.isabs(a.file) else os.path.join(r.root, a.file)
    if not os.path.isfile(ap):
        raise RepoError("文件不存在：%s" % ap)
    blob = get_service().from_file(ap, a.size)
    if not blob:
        out("这个文件没有可用缩略图（本机可能没有对应的缩略图处理器）")
        return 1
    dst = a.out or os.path.splitext(ap)[0] + ".预览.png"
    with open(dst, "wb") as f:
        f.write(blob)
    out("已写出 %s（%d 字节）" % (dst, len(blob)))
    return 0


def cmd_warm_sw(a):
    """批量给 SolidWorks 零件补缩略图（要装着 SolidWorks，且它得空着）

    每个零件都要起一次 SolidWorks，大约 20 秒，所以做成可以随时中断、
    下次接着来的样子：已经出过图的直接跳过。
    """
    import time

    from . import mesh as mesh_mod
    from .thumbs import get_service
    from .store import Repo as R
    r = find_repo(a.dir)
    if not mesh_mod.solidworks_available():
        out("这台机器没装 SolidWorks，补不了 SolidWorks 零件的缩略图")
        return 1
    head = r.head_oid()
    _, files = r.files_of(head)
    svc = get_service()
    # 这是后台补图：有人（网页请求）也要用 SolidWorks 时得主动让路，见 mesh._sw_gate
    mesh_mod.BATCH = True
    todo = [(p, v) for p, v in files.items()
            if p.lower().endswith(mesh_mod.MESHABLE_EXTS)
            and not svc.cached(r, v.get("oid") or "", a.size)
            and (a.force or not svc.empty(r, v.get("oid") or ""))]
    # 已经有图的永远跳过（--force 只用来穿透"以前失败过"的标记，不重做已完成的）
    # 小的先来：列表第一屏先有图
    todo.sort(key=lambda kv: int(kv[1].get("size") or 0))
    if a.limit:
        todo = todo[:a.limit]
    out("待补 %d 个零件（尺寸 %d）" % (len(todo), a.size))
    t_end = time.time() + a.minutes * 60 if a.minutes else None
    ok = fail = 0
    t_all = time.time()
    for i, (p, v) in enumerate(todo, 1):
        if t_end and time.time() > t_end:
            out("到时间了，先停（已完成 %d 个）" % (i - 1))
            break
        t0 = time.time()
        blob = svc.get(r, v.get("oid") or "", p.rsplit("/", 1)[-1], a.size,
                       force=a.force)
        if blob:
            ok += 1
            out("[%d/%d] %5.1fs %s" % (i, len(todo), time.time() - t0, p))
        else:
            fail += 1
            out("[%d/%d] 失败 %s" % (i, len(todo), p))
    mesh_mod.BATCH = False
    out("完成：成功 %d，失败 %d，用时 %.1f 分钟"
        % (ok, fail, (time.time() - t_all) / 60.0))
    return 0


def cmd_warm_neutral(a):
    """批量给 STEP/IGES/STL 补缩略图：用无头浏览器解析，再走同一条出图管线。

    SolidWorks 在本机打不开这些中性格式（实测 2024：OpenDoc 各种类型都返回 Nothing），
    但网页里内置的 occt-import-js 能解。让无头 Edge/Chrome 打开 `web/meshgen.html`
    解一次，网格按对象号存成 ZGM —— 之后两台机器都秒出图，样式也和 SolidWorks 零件一致。
    """
    import shutil
    import subprocess
    import tempfile
    import urllib.parse

    from . import mesh as mesh_mod
    from .thumbs import NEUTRAL_EXTS, get_service

    r = find_repo(a.dir)
    browser = a.browser or find_browser()
    if not browser:
        out("找不到 Edge/Chrome，没法在无头模式里解析中性格式")
        return 1
    svc = get_service()
    try:
        _, files = r.files_of(r.head_oid())
    except RepoError as e:
        out("读不到项目版本：%s" % e)
        return 1
    exts = NEUTRAL_EXTS + (".stl",)
    todo = [(p, v) for p, v in files.items()
            if p.lower().endswith(exts)
            and not mesh_mod.is_cached(r, v.get("oid") or "")
            and not svc.cached(r, v.get("oid") or "", a.size)]
    todo.sort(key=lambda kv: int(kv[1].get("size") or 0))
    if a.limit:
        todo = todo[:a.limit]
    out("待解析 %d 个（用 %s）" % (len(todo), browser))
    ok = fail = 0
    t_all = time.time()
    t_end = t_all + a.minutes * 60 if a.minutes else None
    for i, (p, v) in enumerate(todo, 1):
        if t_end and time.time() > t_end:
            out("到时间了，先停（已处理 %d 个）" % (i - 1))
            break
        oid = v.get("oid") or ""
        t0 = time.time()
        url = "http://127.0.0.1:8795/meshgen.html?" + urllib.parse.urlencode(
            {"path": r.root, "oid": oid, "file": p})
        prof = tempfile.mkdtemp(prefix="zgr-edge-")
        try:
            proc = subprocess.Popen(
                [browser, "--headless=new", "--disable-gpu", "--mute-audio",
                 "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                 "--disable-background-networking", "--user-data-dir=" + prof, url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=mesh_mod.CREATE_NO_WINDOW)
        except OSError as e:
            shutil.rmtree(prof, ignore_errors=True)
            out("启动浏览器失败：%s" % e)
            return 1
        got = False
        exited = None
        deadline = time.time() + a.timeout
        while time.time() < deadline:
            if mesh_mod.is_cached(r, oid):
                got = True
                break
            if proc.poll() is not None:
                if exited is None:
                    exited = time.time()
                elif time.time() - exited > 3:      # 进程退了，POST 可能刚落盘
                    break
            time.sleep(0.25)
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(prof, ignore_errors=True)
        if got:
            ok += 1
            # 网格有了就立刻出 PNG，别等下一次打开网页才生成
            try:
                svc.get(r, oid, p.rsplit("/", 1)[-1], a.size, force=True)
            except Exception:
                pass
            out("[%d/%d] %5.1fs %s" % (i, len(todo), time.time() - t0, p))
        else:
            fail += 1
            out("[%d/%d] 失败 %s" % (i, len(todo), p))
    out("完成：成功 %d，失败 %d，用时 %.1f 分钟"
        % (ok, fail, (time.time() - t_all) / 60.0))
    return 0


def cmd_projects(a):
    reg = Registry()
    if not reg.projects:
        out("还没有项目。用 `zgr init <目录>` 新建。")
        return 0
    for p in reg.projects:
        r = Repo(p["path"])
        if r.exists:
            st = r.status()
            out("%-16s %-12s %s" % (
                p["name"], (st["head"] or "空")[:10],
                "有改动" if not st["clean"] else "已记录"))
            out("    %s" % p["path"])
        else:
            out("%-16s （目录或项目元数据缺失）" % p["name"])
            out("    %s" % p["path"])
    return 0


def cmd_open(a):
    r = find_repo(a.dir)
    if not r.exists:
        raise RepoError("这里还不是追光几何项目")
    reg = Registry()
    reg.add(r.root, r.name)
    from .server import serve
    # 已有服务则直接开网页
    import urllib.request
    for port in (8795,):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=0.6) as resp:
                if json.loads(resp.read().decode("utf-8")).get("app") == APP_NAME:
                    import webbrowser
                    webbrowser.open("http://127.0.0.1:%d/#%s" % (port, urllib.parse.quote(r.root)))
                    out("已打开现有服务：http://127.0.0.1:%d" % port)
                    return 0
        except Exception:
            pass
    serve(port=a.port, host="127.0.0.1", project=r.root, open_browser=True)
    return 0


# --------------------------------------------------------------------- 入口
def build_parser():
    p = argparse.ArgumentParser(
        prog="zgr",
        description="%s v%s —— 本地优先的文件版本记录工具（无激活/无设备限制）" % (
            APP_NAME, __version__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用：
  zgr init "D:\\项目\\图纸库" --name 图纸库 --watch
  zgr serve --port 8795 --open
  zgr sync . --url http://192.168.1.10:8795 --both --checkout
""")
    p.add_argument("-v", "--version", action="version", version="%s %s" % (APP_NAME, __version__))
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="新建项目（在目录下创建 .zgr）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--name")
    s.add_argument("--watch", action="store_true", help="开启自动记录")
    s.add_argument("--interval", type=int, default=30)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("demo", help="建一个带两个版本的示例项目，试着玩玩")
    s.add_argument("-o", "--open", action="store_true",
                   help="建好后直接打开网页界面")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("status", help="查看未记录的改动")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("snapshot", aliases=["commit"], help="记录一次版本")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("-m", "--message")
    s.add_argument("--author")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("log", help="版本时间线")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("-n", "--limit", type=int, default=50)
    s.add_argument("-c", "--changes", action="store_true",
                   help="每条后面标出这一版改了几个文件（+新增 ~修改 -删除）")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("cache", help="看 / 清缓存（缩略图、DWG 预览、3D 网格，删了不影响版本）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--clear", action="store_true", help="全清（预览缓存 + 网格缓存）")
    s.add_argument("--prune", action="store_true",
                   help="按体积上限删到不超（最旧的先删，10 分钟内生成的不动）")
    s.add_argument("--max-mb", type=int, help="预览缓存上限（MB），默认取项目配置 2048")
    s.add_argument("--mesh-max-mb", type=int, help="网格缓存上限（MB），默认同 --max-mb")
    s.add_argument("--dry", action="store_true", help="只算不删")
    s.set_defaults(func=cmd_cache)

    s = sub.add_parser("show", help="看某一版里有哪些文件")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("commit")
    s.add_argument("--file", help="只列路径里含这个词的文件")
    s.add_argument("-n", "--limit", type=int, default=200,
                   help="最多列多少个（0 = 全部）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("diff", help="对比两个版本")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--file", help="只看某个文件的逐行差异")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("restore", help="恢复到某个版本")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("commit")
    s.add_argument("--target")
    s.add_argument("--no-mirror", action="store_true", help="只写文件，不删除多出来的文件")
    s.add_argument("--no-backup", action="store_true", help="覆盖前不备份")
    s.add_argument("--dry", action="store_true",
                   help="只算会覆盖/新增/删除几个文件，一个都不动")
    s.add_argument("--yes", action="store_true",
                   help="工作区有未记录改动时，确认照样恢复（覆盖前的文件会备份）")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("export", help="把某个版本导出到目录")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("commit")
    s.add_argument("target")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("mark", help="给版本标颜色/打标签")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("commit")
    s.add_argument("color", choices=list(MARKS) + ["none"])
    s.add_argument("--tags", help="逗号分隔")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("watch", help="前台自动记录（命令行窗口）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--interval", type=int, default=30)
    s.add_argument("-m", "--message")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("serve", help="启动网页服务")
    s.add_argument("--port", type=int, default=8795)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--token")
    s.add_argument("--project")
    s.add_argument("--open", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sync", help="与另一台机器 / 远端存储同步")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--remote", help="用 zgr remote add 配好的远端名字")
    s.add_argument("--url", help="对方地址，如 http://192.168.1.10:8795")
    s.add_argument("--share", help="共享文件夹路径，如 \\\\192.168.1.10\\Share174\\zgr")
    s.add_argument("--remote-project", help="共享目录里的项目名（默认与本地同名）")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--push", action="store_true", help="只推送")
    g.add_argument("--pull", action="store_true", help="只拉取")
    g.add_argument("--both", action="store_true", help="双向（默认）")
    s.add_argument("--token")
    s.add_argument("--branch")
    s.add_argument("--checkout", action="store_true", help="拉取后同时更新工作区文件")
    s.add_argument("--force", action="store_true", help="工作区有改动也强制覆盖")
    s.set_defaults(func=cmd_sync, push=None, pull=None, both=None)

    s = sub.add_parser("repair", help="从对方补齐缺的/坏的对象（两台互为备份）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--url", help="对方地址，如 http://192.168.1.10:8795")
    s.add_argument("--share", help="共享文件夹路径")
    s.add_argument("--remote-project", help="共享目录里的项目名（默认与本地同名）")
    s.add_argument("--token")
    s.add_argument("--branch")
    s.add_argument("--quick", action="store_true",
                   help="只查在不在，不做逐字节核对（快，但查不出静默损坏）")
    s.set_defaults(func=cmd_repair)

    s = sub.add_parser("remote", help="配置远端存储（WebDAV / S3 / 共享目录）")
    s.add_argument("action", choices=["list", "add", "rm", "test"])
    s.add_argument("name", nargs="?", help="远端名字（add/rm/test 用）")
    s.add_argument("--dir", default=".", help="项目目录")
    s.add_argument("--kind", choices=["webdav", "s3", "local", "http"],
                   help="webdav / s3 / local(共享目录、NAS 挂载) / http(另一台装了本软件)")
    s.add_argument("--url", help="webdav 或 http 的地址")
    s.add_argument("--path", help="local 的路径，如 \\\\NAS\\备份\\追光")
    s.add_argument("--user")
    s.add_argument("--password")
    s.add_argument("--bucket")
    s.add_argument("--endpoint", help="S3 端点，如 https://s3.us-east-1.amazonaws.com")
    s.add_argument("--region")
    s.add_argument("--access-key")
    s.add_argument("--secret-key")
    s.add_argument("--token")
    s.add_argument("--prefix", help="远端子目录前缀")
    s.add_argument("--remote-project", help="远端里的项目名（默认与本地同名）")
    s.add_argument("--no-path-style", action="store_true",
                   help="S3 用虚拟主机风格（默认路径风格，MinIO 要路径风格）")
    s.add_argument("--no-test", action="store_true", help="添加后不立刻自检")
    s.set_defaults(func=cmd_remote)

    for name, func, hlp in (("merge", cmd_merge, "把对方改动按文件合并进来"),
                            ("undo", cmd_undo, "回退到某个历史版本")):
        s = sub.add_parser(name, help=hlp)
        if name == "merge":
            s.add_argument("dir", nargs="?", default=".")
        else:
            s.add_argument("--dir", default=".")
        s.add_argument("--remote", help="用 zgr remote add 配好的远端名字")
        s.add_argument("--url")
        s.add_argument("--share")
        s.add_argument("--remote-project")
        s.add_argument("--token")
        s.add_argument("--branch")
        s.add_argument("--no-checkout", action="store_true", help="只合并历史，不动工作区文件")
        if name == "merge":
            s.add_argument("--dry-run", action="store_true", help="只看计划，不合并")
            s.add_argument("-m", "--message")
        else:
            s.add_argument("commit", help="要退回的版本号")
        s.set_defaults(func=func)

    s = sub.add_parser("pack", help="把某个版本打包成 zip")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--commit", help="版本号，默认最新")
    s.add_argument("--worktree", action="store_true", help="打包当前文件夹（含未记录改动）")
    s.add_argument("--prefix", help="zip 里的顶层目录名")
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser("fsck", help="体检：查对象缺没缺、坏没坏（只读，不动图纸）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--deep", action="store_true",
                   help="逐个重算校验和，能发现磁盘静默损坏（大项目会慢）")
    s.set_defaults(func=cmd_fsck)

    s = sub.add_parser("doctor", help="一眼看清项目健不健康、下一步该敲什么（只读）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("search", help="跨所有版本按文件名找零件（删掉的也能找到）")
    s.add_argument("q", help="文件名或路径片段，例如 侧板、8mm铜扣")
    s.add_argument("--dir", default=".", help="项目目录，默认当前目录")
    s.add_argument("--limit", type=int, default=30, help="最多列几行（默认 30）")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("find", help="在全部历史里按说明/记录人/版本号找版本（和网页搜索框同一口径）")
    s.add_argument("q", help="说明里的关键词、记录人姓名，或者版本号的开头几个字符")
    s.add_argument("--dir", default=".", help="项目目录，默认当前目录")
    s.add_argument("--limit", type=int, default=20, help="最多列几行（默认 20）")
    s.add_argument("--scan", type=int, default=20000, help="最多翻这么多个版本（默认 20000）")
    s.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("today", help="今天记了哪些版本、各是谁记的（站会、交接时看这一行）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--limit", type=int, default=20, help="最多列几行（默认 20）")
    s.add_argument("--scan", type=int, default=20000, help="最多翻这么多个版本（默认 20000）")
    s.add_argument("--md", action="store_true",
                   help="输出成一段可以直接粘到群里的话（标题 + 一版一行）")
    s.set_defaults(func=cmd_today)

    s = sub.add_parser("report", help="近 N 天每天几版、谁记的（站会/周报，--md 直接粘到群里）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--days", type=int, default=7, help="看最近几天（默认 7）")
    s.add_argument("--scan", type=int, default=50000, help="最多翻这么多个版本（默认 50000）")
    s.add_argument("--md", action="store_true", help="输出成可以直接粘到群里的话")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("size", help="看项目被什么占着：图纸/历史/回收站/打包各多大")
    s.add_argument("dir", nargs="?", default=".")
    s.set_defaults(func=cmd_size)

    s = sub.add_parser("gc", help="回收没人引用的对象（删掉的分支留下的老数据），让项目变小")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--dry", action="store_true", help="演练：只报会删多少，一个都不删")
    s.add_argument("--keep-days", type=int, default=7,
                   help="最近几天内的对象一律不删（防止误删同步中途的对象，默认 7）")
    s.add_argument("--yes", action="store_true", help="真的删（不加就是演练）")
    s.set_defaults(func=cmd_gc)

    s = sub.add_parser("backup", help="查看 / 复原回收站")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--restore", help="要复原的备份批次名（整批）")
    s.add_argument("--files", metavar="批次名", help="看这批备份里有哪些文件")
    s.add_argument("--restore-file", dest="restore_file", metavar="批次名",
                   help="只复原这批备份里的一个文件（配合 --file）")
    s.add_argument("--file", help="要复原的相对路径（用 --files 查）")
    s.add_argument("--no-backup", action="store_true", help="复原单文件时不备份现有内容")
    s.add_argument("--limit", type=int, default=500, help="--files 最多列几个（默认 500）")
    s.add_argument("--prune", action="store_true", help="清理旧备份，只留最新几批")
    s.add_argument("--keep", type=int, default=20, help="保留几批（默认 20）")
    s.add_argument("--dry", action="store_true", help="演练：只报会删什么，不真删")
    s.add_argument("--max-mb", dest="max_mb", type=int, default=0,
                   help="回收站超过这么多 MB 时从最旧的开始删（0 = 只看批数）")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("recover", help="收尾上次被断电/强杀打断的恢复")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--from", dest="mode", choices=("again", "backup"), default="again",
                   help="again=重新恢复那个版本（默认）；backup=把备份原样拷回去")
    s.add_argument("--dismiss", action="store_true", help="只清掉提示，不动文件")
    s.set_defaults(func=cmd_recover)

    s = sub.add_parser("live", help="前台实时同步（网页服务里也有）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--url", required=True)
    s.add_argument("--token")
    s.add_argument("--interval", type=int, default=15)
    s.add_argument("--no-pull", action="store_true")
    s.add_argument("--no-push", action="store_true")
    s.add_argument("--no-merge", action="store_true")
    s.add_argument("--no-checkout", action="store_true")
    s.add_argument("--split", action="store_true",
                   help="一人一条线：各记各的分支，只同步不合并（默认让程序合并）")
    s.set_defaults(func=cmd_live)

    s = sub.add_parser("preview", help="导出某个模型文件的缩略图")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("file", help="相对项目根的文件路径")
    s.add_argument("--size", type=int, default=512)
    s.add_argument("--out")
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("warm-sw", help="批量给 SolidWorks 零件补缩略图（要装 SolidWorks）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--size", type=int, default=128)
    s.add_argument("--limit", type=int, default=0, help="最多补多少个（0 = 不限）")
    s.add_argument("--minutes", type=int, default=0, help="跑多少分钟后自动停（0 = 不限）")
    s.add_argument("--force", action="store_true", help="连以前失败过的也重试")
    s.set_defaults(func=cmd_warm_sw)

    s = sub.add_parser("warm-neutral", help="批量给 STEP/IGES/STL 补缩略图（用无头浏览器解析）")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--size", type=int, default=128)
    s.add_argument("--limit", type=int, default=0, help="最多处理多少个（0 = 不限）")
    s.add_argument("--minutes", type=int, default=0, help="跑多少分钟后自动停（0 = 不限）")
    s.add_argument("--timeout", type=int, default=90, help="单个文件最多等几秒（默认 90）")
    s.add_argument("--browser", default="", help="指定 msedge.exe / chrome.exe 路径")
    s.set_defaults(func=cmd_warm_neutral)

    s = sub.add_parser("projects", help="列出所有项目")
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("open", help="打开网页界面")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--port", type=int, default=8795)
    s.set_defaults(func=cmd_open)

    return p


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 0
    if args.cmd == "sync":
        a_push = bool(args.push) or (not args.pull)
        a_pull = bool(args.pull) or (not args.push)
        args.push, args.pull = a_push, a_pull
    try:
        return args.func(args) or 0
    except (RepoError, SyncError) as e:
        print("错误：%s" % e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
