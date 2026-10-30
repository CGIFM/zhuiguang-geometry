"""追光几何 · 选择性合并

把对方（云端/另一台机器）的改动并进本机，**按文件挑**，不搞全有全无：

    base   = 两边最近的共同祖先
    ours   = 本机当前版本
    theirs = 对方当前版本

每个文件的默认规则：
    只有一边改过            → 自动采用改过的那边
    两边改成完全一样的        → 直接采用
    楼主新增、对方新增且不同   → 冲突（默认留本机，可改选对方或两边都留）
    两边都改且不同            → 冲突（默认留本机）
    一边删了、另一边没动       → 标记出来，默认**保留文件**（宁可不删，绝不丢文件）

合并结果是一个双父提交，因此合并后既能继续往前走，也能回退到任一历史版本。
"""
from __future__ import annotations

import difflib
import json
from collections import deque
import os
import time

from .store import Repo, RepoError

# 智能合并只处理这么大的文本文件；再大就老老实实让人选
TEXT_MERGE_MAX = 4 << 20


class MergeError(Exception):
    pass


# --------------------------------------------------------------------- 祖先
def ancestors(repo: Repo, oid: str | None, limit: int = 200000) -> dict:
    """返回 {oid: 距离}，沿全部父链"""
    if not oid:
        return {}
    dist = {oid: 0}
    queue = deque([oid])      # 历史长的项目上 list.pop(0) 是 O(n²)
    while queue and len(dist) < limit:
        cur = queue.popleft()
        d = dist[cur]
        try:
            c = repo.commit_info(cur)
        except RepoError:
            continue
        for p in c.get("parents") or []:
            if p and p not in dist and repo.has(p):
                dist[p] = d + 1
                queue.append(p)
    return dist


def common_ancestor(repo: Repo, a: str | None, b: str | None):
    da = ancestors(repo, a)
    db = ancestors(repo, b)
    both = set(da) & set(db)
    if not both:
        return None
    # 取离两边都近的
    return min(both, key=lambda x: (max(da[x], db[x]), da[x] + db[x]))


# --------------------------------------------------------------------- 三路文本合并
def _hunks(base, side):
    """把 base→side 的差异拆成「改动块」：替换 base[b0:b1] 为 lines"""
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.append({"b0": i1, "b1": i2, "lines": side[j1:j2]})
    return out


def _overlap(a, b):
    """两个改动块是否必须一起决定（插入点落在对方改动区间内算重叠）"""
    a0, a1, b0, b1 = a["b0"], a["b1"], b["b0"], b["b1"]
    if a0 == a1 and b0 == b1:
        return a0 == b0
    if a0 == a1:
        return b0 <= a0 <= b1
    if b0 == b1:
        return a0 <= b0 <= a1
    return a0 < b1 and b0 < a1


def _clusters(hunks):
    """把互相重叠的改动块聚成组；不同组之间可以各自独立合并"""
    groups = []
    for h in sorted(hunks, key=lambda x: (x["b0"], x["b1"])):
        hit = [g for g in groups if any(_overlap(h, x) for x in g)]
        if not hit:
            groups.append([h])
            continue
        base_g = hit[0]
        base_g.append(h)
        for g in hit[1:]:               # 这个块把几组连起来了
            base_g.extend(g)
            groups.remove(g)
    return groups


def _apply_hunks(base, s, e, hunks):
    out, cur = [], s
    for h in sorted(hunks, key=lambda x: (x["b0"], x["b1"])):
        if h["b0"] < s or h["b1"] > e:
            continue
        if h["b0"] < cur:               # 同一侧不会重叠，保护性跳过
            continue
        out.extend(base[cur:h["b0"]])
        out.extend(h["lines"])
        cur = h["b1"]
    out.extend(base[cur:e])
    return out


def merge3_lines(base, ours, theirs):
    """逐行三路合并。返回 (合并后的行, 冲突块数)。

    规则（和 git diff3 一致）：
      只有一边改          → 采用改过的一边
      两边改成一样        → 采用
      两边改同一处且不同  → 冲突，这一块保留本机
      两边改不同地方      → 两边的改动都留下
    """
    ho = [dict(h, side="o") for h in _hunks(base, ours)]
    ht = [dict(h, side="t") for h in _hunks(base, theirs)]
    groups = _clusters(ho + ht)
    out, cur, conflicts = [], 0, 0
    for g in sorted(groups, key=lambda x: (min(h["b0"] for h in x),
                                           max(h["b1"] for h in x))):
        s = min(h["b0"] for h in g)
        e = max(h["b1"] for h in g)
        if s < cur:
            continue
        out.extend(base[cur:s])
        og = [h for h in g if h["side"] == "o"]
        tg = [h for h in g if h["side"] == "t"]
        oseg = _apply_hunks(base, s, e, og)
        tseg = _apply_hunks(base, s, e, tg)
        if oseg == tseg:
            out.extend(oseg)
        elif not og:
            out.extend(tseg)
        elif not tg:
            out.extend(oseg)
        else:
            conflicts += 1
            out.extend(oseg)
        cur = e
    out.extend(base[cur:])
    return out, conflicts


def _load_text(repo: Repo, oid):
    if not oid:
        return b""
    kind, payload = repo.get(oid)
    if kind != "blob":
        return None
    if len(payload) > TEXT_MERGE_MAX or b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def text_merge(repo: Repo, base_oid, ours_oid, theirs_oid):
    """尝试逐行合并三个版本。返回 {data, conflicts}；不是可合并文本返回 None"""
    texts = []
    for oid in (base_oid, ours_oid, theirs_oid):
        t = _load_text(repo, oid)
        if t is None:
            return None
        texts.append(t)
    merged, conflicts = merge3_lines(
        texts[0].splitlines(keepends=True),
        texts[1].splitlines(keepends=True),
        texts[2].splitlines(keepends=True))
    return {"data": "".join(merged).encode("utf-8"), "conflicts": conflicts}


def _smart_resolve(repo: Repo, e: dict):
    """冲突文件试一次自动三路合并；成功就把结果写进对象库并改默认选择"""
    m = text_merge(repo, (e.get("base") or {}).get("oid"),
                   (e.get("ours") or {}).get("oid"),
                   (e.get("theirs") or {}).get("oid"))
    if not m or m["conflicts"]:
        return False
    oid = repo.put("blob", m["data"])
    e["merged"] = {"oid": oid, "size": len(m["data"])}
    e["pick"] = "merged"
    e["auto"] = "text"
    return True


# --------------------------------------------------------------------- 计划
def _entry(b, o, t):
    """单个文件的合并判定"""
    bo = (b or {}).get("oid")
    oo = (o or {}).get("oid")
    to = (t or {}).get("oid")
    size = lambda x: int((x or {}).get("size") or 0)  # noqa: E731
    if oo == to:
        return {"status": "same", "pick": "ours" if oo else "none",
                "ours": o, "theirs": t, "base": b,
                "size": size(o or t)}
    if o and not t:
        if bo and bo == oo:
            return {"status": "theirs_deleted", "pick": "ours",   # 默认不删文件
                    "ours": o, "theirs": None, "base": b, "size": size(o)}
        return {"status": "ours", "pick": "ours", "ours": o, "theirs": None,
                "base": b, "size": size(o)}
    if t and not o:
        if bo and bo == to:
            return {"status": "ours_deleted", "pick": "theirs",   # 本机删了、对方没动 → 跟随删除
                    "ours": None, "theirs": t, "base": b, "size": size(t)}
        return {"status": "theirs_only", "pick": "theirs", "ours": None, "theirs": t,
                "base": b, "size": size(t)}
    # 两边都有且不同
    if bo and bo == oo:
        return {"status": "theirs", "pick": "theirs", "ours": o, "theirs": t,
                "base": b, "size": size(t)}
    if bo and bo == to:
        return {"status": "ours", "pick": "ours", "ours": o, "theirs": t,
                "base": b, "size": size(o)}
    return {"status": "conflict", "pick": "ours", "ours": o, "theirs": t,
            "base": b, "size": size(t)}


# 「谁动过它」要沿提交往下翻：翻太多没有意义（真撞车的那几个文件，最近几版就找到了）
CLASH_SCAN = 400
# 撞车的文件超过这么多就不逐个查作者了：翻几百个提交只为了一张看不完的清单，不值
CLASH_FILES = 60

# 双方都动过同一个文件的三种情形
CLASH_STATUS = ("conflict", "ours_deleted", "theirs_deleted")


def _touched(repo: Repo, head: str, base: str | None, paths, limit: int = CLASH_SCAN) -> dict:
    """这些文件在**这一侧**最近一次改动：{路径: {commit, author, time, message}}

    从这一侧的头往下走，第一个让该文件内容变化的提交就是答案
    （一人一条线是线性历史，沿第一父走即可）。只解析需要的文件，不展开整棵树。
    """
    want, out = set(paths), {}
    oid, seen, n = head, set(), 0
    while oid and oid != base and oid not in seen and want and n < limit:
        seen.add(oid)
        n += 1
        try:
            c = repo.commit_info(oid)
            cur_tree = c.get("tree")
            parents = [p for p in (c.get("parents") or []) if p]
            old_tree = repo.tree_of(parents[0]) if parents else None
        except (RepoError, OSError, IndexError):
            break
        for p in list(want):
            try:
                new = repo.tree_entry(cur_tree, p) or {}
                old = repo.tree_entry(old_tree, p) or {}
            except (RepoError, OSError):
                continue
            if new.get("oid") != old.get("oid"):
                out[p] = {"commit": oid,
                          "author": c.get("author") or "",
                          "time": c.get("time"),
                          "message": (c.get("message") or "").split("\n")[0][:80]}
                want.discard(p)
        oid = parents[0] if parents else None
    return out


def plan(repo: Repo, theirs: str, ours: str | None = None,
         smart: bool = False, detail: bool = False) -> dict:
    """算出每个文件怎么合；不落盘（smart 时会把自动合并结果写进对象库）

    detail=True 时多算一份 `clash_detail`：撞车的文件**两边各自是谁、什么时候改的**。
    一人一条线合并时，用户最想知道的就是「这个零件他也动过，该找谁」，
    而默认的 entries 只说了「有冲突」，说不出人。
    """
    ours = ours or repo.head_oid()
    if not theirs:
        raise MergeError("对方还没有任何版本")
    if not ours:
        raise MergeError("本机还没有任何版本，直接拉取即可")
    base = common_ancestor(repo, ours, theirs)
    _, fb = repo.files_of(base) if base else ({}, {})
    _, fo = repo.files_of(ours)
    _, ft = repo.files_of(theirs)
    entries = []
    for path in sorted(set(fo) | set(ft) | set(fb)):
        e = {"path": path, **_entry(fb.get(path), fo.get(path), ft.get(path))}
        if smart and e["status"] == "conflict":
            _smart_resolve(repo, e)
        entries.append(e)
    counts = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    auto = [e["path"] for e in entries if e.get("auto")]
    status = {e["path"]: e["status"] for e in entries}

    def oid_of(m, p):
        return ((m or {}).get(p) or {}).get("oid")

    # 撞车的判定**直接看两边相对共同祖先有没有各自动过**，而不是看合并策略给的标签：
    # 「我改了、他删了」在策略上被归成 ours（保留文件、不丢内容），但那两个人确实
    # 对同一个零件做了相反的事——该在人合并前说出来，而不是替他默默决定。
    clashes = []
    for p in (set(fo) | set(ft) | set(fb)):
        ob, oo, ot = oid_of(fb, p), oid_of(fo, p), oid_of(ft, p)
        if oo != ot and oo != ob and ot != ob:
            clashes.append(p)
    # 真冲突排前面：删/改撞车往往只需要「留哪个」一句，内容冲突才要逐行看
    clashes.sort(key=lambda p: (status.get(p) != "conflict", p))
    out = {"base": base, "ours": ours, "theirs": theirs,
           "entries": entries, "counts": counts, "auto": auto,
           "conflicts": [e["path"] for e in entries if e["status"] == "conflict"],
           "clashes": clashes}
    if detail and clashes and len(clashes) <= CLASH_FILES:
        mine = _touched(repo, ours, base, clashes)
        theirs_touch = _touched(repo, theirs, base, clashes)
        out["clash_detail"] = [{"path": p, "status": status[p],
                                "ours": mine.get(p), "theirs": theirs_touch.get(p)}
                               for p in clashes]
    elif detail:
        out["clash_detail"] = []
        out["clash_skipped"] = len(clashes)
    return out


def remote_log(repo: Repo, theirs: str, base: str | None, limit: int = 50):
    """对方比共同祖先多出来的提交（新→旧），用于「云端有哪些变化」"""
    out, seen, oid = [], set(), theirs
    while oid and oid not in seen and oid != base and len(out) < limit:
        seen.add(oid)
        try:
            c = repo.commit_info(oid)
        except RepoError:
            break
        out.append({"id": oid, "message": c.get("message"), "time": c.get("time"),
                    "author": c.get("author"), "stats": c.get("stats")})
        parents = c.get("parents") or []
        oid = parents[0] if parents else None
    return out


# --------------------------------------------------------------------- 执行
def apply(repo: Repo, p: dict, choices: dict | None = None, message: str = "",
          checkout: bool = True, backup: bool = True, author: str | None = None,
          strategy: str = "manual"):
    """按计划与选择合并成一个双父提交

    strategy="smart" 时，没被手工指定的冲突文件会先尝试逐行三路合并；
    合不动的仍然保留本机内容（绝不丢文件），并在 decisions 里标出来。
    """
    choices = choices or {}
    # 计划是「在当前这一版上」算出来的：如果这中间又记了一版（网页自动记录、
    # 命令行同时在跑），合并提交挂到旧的父提交上，那一版就从分支上掉下去了。
    head = repo.head_oid()
    base_oid = p.get("ours")
    if base_oid and head != base_oid:
        raise MergeError(
            "合并计划是在 %s 这一版上算的，这中间本机又记录了 %s。"
            "为免把刚记的那一版挤出分支，请重新点一次「检查对方改动」再合并。"
            % (base_oid[:10], (head or "空")[:10]))
    entries = p["entries"]
    files = {}
    decisions = []
    for e in entries:
        path = e["path"]
        pick = (choices.get(path) or e["pick"] or "ours").lower()
        o, t = e.get("ours"), e.get("theirs")
        if (strategy == "smart" and path not in choices
                and e["status"] == "conflict" and not e.get("merged")):
            _smart_resolve(repo, e)
            pick = (e["pick"] or "ours").lower()
        if pick == "merged" and e.get("merged"):
            files[path] = {"oid": e["merged"]["oid"],
                           "size": int(e["merged"].get("size") or 0)}
            decisions.append({"path": path, "pick": "merged"})
            continue
        if pick == "both" and o and t:
            files[path] = {"oid": o["oid"], "size": int(o.get("size") or 0)}
            root, ext = os.path.splitext(path)
            mine = "%s.本机版本%s" % (root, ext)
            # 名字可能已经被占：同一次合并里点了两次「都留」，或者项目里本来就有这么个文件。
            # 不查就会把对方那一版悄悄覆盖掉——用户选了「都留」，结果只剩一份。
            taken = set(files) | {x["path"] for x in entries}
            n = 1
            while mine in taken:
                n += 1
                mine = "%s.本机版本(%d)%s" % (root, n, ext)
            files[mine] = {"oid": t["oid"], "size": int(t.get("size") or 0)}
            decisions.append({"path": path, "pick": "both", "extra": mine})
            continue
        if pick == "theirs":
            if t:
                files[path] = {"oid": t["oid"], "size": int(t.get("size") or 0)}
            decisions.append({"path": path, "pick": "theirs" if t else "delete"})
        elif pick == "delete":
            decisions.append({"path": path, "pick": "delete"})
        elif pick == "none":
            decisions.append({"path": path, "pick": "none"})
        else:                                   # ours（默认）
            if o:
                files[path] = {"oid": o["oid"], "size": int(o.get("size") or 0)}
            decisions.append({"path": path, "pick": "ours" if o else "delete"})
    if not files:
        raise MergeError("合并结果为空，已取消")
    tree = repo._build_trees(files)
    stats = {"files": len(files), "bytes": sum(v["size"] for v in files.values())}
    msg = message or "合并对方改动（%s）" % time.strftime("%Y-%m-%d %H:%M")
    parents = [p["ours"], p["theirs"]]
    oid = repo.commit_tree(tree, parents, msg, author=author, stats=stats)
    res = {"commit": oid, "base": p.get("base"), "parents": parents,
           "files": len(files), "decisions": decisions, "cleaned": False}
    if checkout:
        res.update(repo.restore(oid, backup=backup))
        res["commit"] = oid
    return res


def undo(repo: Repo, commit: str, checkout: bool = True, backup: bool = True,
         message: str = "", author: str | None = None):
    """回退到某个历史版本的内容。

    做成一个**新的普通提交**（内容 = 目标版本，父 = 当前版本），而不是把分支指针
    往回拨：这样被回退掉的那次合并仍在历史里，谁都找得回来，推送也不需要强推。
    """
    target = repo.resolve(commit)
    head = repo.head_oid()
    _, files = repo.files_of(target)
    plain = {p: {"oid": v["oid"], "size": int(v.get("size") or 0)}
             for p, v in files.items()}
    tree = repo._build_trees(plain)
    stats = {"files": len(plain), "bytes": sum(v["size"] for v in plain.values())}
    msg = message or "回退到 %s（%s）" % (
        target[:10], time.strftime("%Y-%m-%d %H:%M"))
    oid = repo.commit_tree(tree, [head] if head else [], msg, stats=stats,
                           author=author)
    repo.set_branch(repo.branch, oid)
    out = {"commit": oid, "target": target, "previous": head, "branch": repo.branch}
    if checkout:
        out.update(repo.restore(oid, backup=backup))
        out["commit"] = oid
    return out
