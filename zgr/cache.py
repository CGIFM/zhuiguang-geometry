# -*- coding: utf-8 -*-
"""缓存总量清理：预览图、3D 网格、DWG 转换结果

为什么单独有个模块：
    这三样和「历史」「回收站」不一样 —— 它们**全都能重新生成**，丢了一点不心疼；
    但它们同样**只会越用越大**，而且长在没人看的地方（`.zgr/previews`、
    `.zgr/meshes`、`%APPDATA%/…/converted`）。167/174 上跑一个月，
    预览图能到几万个文件、几百 MB；网格动辄几十 MB 一个。
    盘满的时候连一次记录都做不成，那才是这套软件真正危险的状态，
    所以清缓存得像清回收站一样自动、定期、有账可查。

原则（和回收站一致）：
    - 只碰我们自己命名的文件（`.png/.miss/.zgm/.json/.dxf/.gz`），别人的文件一个不动；
    - 最近 10 分钟动过的文件不删 —— 它可能正在被生成或回存；
    - 从最旧的开始删，删到不超上限为止；删不掉的跳过并如实回报；
    - `dry=True` 只算不删。
"""
from __future__ import annotations

import os
import time

# 参与清理的扩展名（我们自己写进去的那几种）
CACHE_EXTS = (".png", ".miss", ".zgm", ".json", ".dxf", ".gz", ".empty")
GRACE = 600                      # 秒：这么新的文件不碰


def _walk(path):
    """列出目录下所有缓存文件：(路径, 大小, 修改时间)"""
    out = []
    for dp, _dn, fns in os.walk(path):
        for f in fns:
            if not f.lower().endswith(CACHE_EXTS):
                continue
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((p, int(st.st_size), st.st_mtime))
    return out


def total_bytes(path: str) -> int:
    return sum(s for _p, s, _m in _walk(path))


def prune_dir(path: str, max_bytes: int, dry: bool = False,
              grace: float = GRACE) -> dict:
    """把一个缓存目录删到不超过 max_bytes；返回账目（删了哪些、释放多少）"""
    if not path or not os.path.isdir(path):
        return {"removed": [], "bytes": 0, "kept": 0, "total": 0, "dry": bool(dry)}
    if not max_bytes or max_bytes <= 0:
        rows = _walk(path)
        return {"removed": [], "bytes": 0, "kept": len(rows),
                "total": sum(r[1] for r in rows), "dry": bool(dry)}
    rows = _walk(path)
    rows.sort(key=lambda r: r[2])            # 最旧的在前
    total = sum(r[1] for r in rows)
    now = time.time()
    freed, removed = 0, []
    for p, size, mtime in rows:
        if total <= max_bytes:
            break
        if now - mtime < grace:
            continue                          # 太新：可能正在写，别动
        if not dry:
            try:
                os.remove(p)
            except OSError:
                continue                      # 被占用就跳过，别谎报
        total -= size
        freed += size
        removed.append(p)
    return {"removed": removed, "bytes": freed,
            "kept": len(rows) - len(removed), "total": total, "dry": bool(dry)}


def _mb(v, default):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default * 1024 * 1024
    return 0 if n <= 0 else n * 1024 * 1024


def prune_repo(repo, dry: bool = False, preview_mb=None, mesh_mb=None) -> dict:
    """按项目配置清理这个项目的预览图与网格缓存

    配置键（写在 `.zgr/config.json`，用户随时能改）：
        cache_max_mb        预览图上限，默认 2048（0 = 不限）
        mesh_max_mb         网格上限，默认 2048（0 = 不限）
        cache_auto          设成 false 就完全不自动清
    """
    try:
        cfg = repo.config
    except Exception:                        # noqa: BLE001 - 配置坏了就按默认值来
        cfg = {}
    if cfg.get("cache_auto", True) is False:
        return {"previews": None, "meshes": None, "skipped": "cache_auto=false"}
    pb = _mb(cfg.get("cache_max_mb", 2048), 2048) if preview_mb is None \
        else max(0, int(preview_mb)) * 1024 * 1024
    mb = _mb(cfg.get("mesh_max_mb", 2048), 2048) if mesh_mb is None \
        else max(0, int(mesh_mb)) * 1024 * 1024
    return {"previews": prune_dir(os.path.join(repo.meta, "previews"), pb, dry=dry),
            "meshes": prune_dir(os.path.join(repo.meta, "meshes"), mb, dry=dry)}


def mesh_oids(repo) -> set:
    """网格缓存里出现过的对象号集合（判断哪些已经是死文件用）"""
    d = os.path.join(repo.meta, "meshes")
    out = set()
    if not os.path.isdir(d):
        return out
    for p, _size, _m in _walk(d):
        oid = os.path.basename(p).split(".", 1)[0]
        if len(oid) == 64:
            out.add(oid)
    return out


def orphans(repo, keep_oids) -> list:
    """网格缓存里那些对象已经不在任何版本里的文件（gc 用）

    keep_oids 是一个 oid 集合：只有**明确不在**里面的才删。调用方要保证
    这个集合是「所有分支可达的对象」的全集，少算一个就会删掉还在用的网格。
    """
    d = os.path.join(repo.meta, "meshes")
    out = []
    if not os.path.isdir(d):
        return out
    for p, _size, _m in _walk(d):
        name = os.path.basename(p)
        oid = name.split(".", 1)[0]
        if len(oid) == 64 and oid not in keep_oids:
            out.append(p)
    return out
