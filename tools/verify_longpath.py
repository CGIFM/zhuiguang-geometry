# -*- coding: utf-8 -*-
r"""在真实机器上验一遍长路径：建库 → 记录 → 看状态 → 改 → 恢复。

用法：python tools/verify_longpath.py [根目录]
     不给根目录就用临时目录。会先删掉 <根目录>/zgr-lp-e2e 再重建。

每一项都打一行 `PASS ...`；任何一步失败就打 `FAIL ...` 并以 1 退出。
顺手把本机的 LongPathsEnabled 打出来——它是 0 也能过，才说明 \\?\ 前缀真的生效了。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from zgr.store import Repo, fs_path   # noqa: E402

DEEP_SEG = "很长的零件文件夹名称"      # 10 个字，24 层就必然超过 260


def long_paths_enabled():
    if os.name != "nt":
        return "n/a"
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\FileSystem")
        v, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
        return str(int(v))
    except Exception as e:                 # noqa: BLE001
        return "unknown(%s)" % e


def force_prefix(p):
    r"""无条件加 \\?\ 前缀（哪怕路径本身不长）。

    用来**删掉**一棵很深的目录：shutil.rmtree 会从给定的根往下拼路径，
    根不长就不加前缀，深到某一层又删不动了，残留目录会让下一次建库直接失败。
    """
    if os.name != "nt":
        return p
    a = os.path.abspath(p).replace("/", "\\")
    if a.startswith("\\\\?\\"):
        return a
    if a.startswith("\\\\"):
        return "\\\\?\\UNC" + a[1:]
    return "\\\\?\\" + a


def rmtree_long(path):
    """删掉一棵深目录：从**加过前缀**的根开始走，每一层都是系统能打开的长路径"""
    p = force_prefix(path)
    if not os.path.isdir(p):
        return
    for dp, dns, fns in os.walk(p, topdown=False):
        for f in fns:
            try:
                os.chmod(os.path.join(dp, f), 0o666)
            except OSError:
                pass
            try:
                os.remove(os.path.join(dp, f))
            except OSError:
                pass
        try:
            os.rmdir(dp)
        except OSError:
            pass


def read(p):
    with open(fs_path(p), "r", encoding="utf-8") as f:
        return f.read()


def write(p, text):
    d = os.path.dirname(p)
    if d:
        os.makedirs(fs_path(d), exist_ok=True)
    with open(fs_path(p), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir()
    root = os.path.join(base, "zgr-lp-e2e")
    print("LONGPATH_ENABLED %s" % long_paths_enabled(), flush=True)
    rmtree_long(root)
    shutil.rmtree(fs_path(root), ignore_errors=True)   # 浅的残留也顺手清掉
    os.makedirs(fs_path(root), exist_ok=True)
    rel = "/".join([DEEP_SEG] * 24) + "/底盘总成.sldprt"
    ap = os.path.join(root, *rel.split("/"))
    print("FULLPATH_LEN %d" % len(ap), flush=True)

    try:
        repo = Repo(root).init("长路径验收")
        write(ap, "第一版内容")
        print("PASS write", flush=True)

        oid = repo.commit("长路径第一版", "验收")
        print("PASS commit %s" % (oid or "")[:12], flush=True)

        info, files = repo.files_of(oid)
        assert rel in files, "提交里没有这个文件"
        print("PASS in_commit", flush=True)

        st = repo.status()
        assert st["clean"], "记录完状态不干净：%s" % st["counts"]
        print("PASS status_clean", flush=True)

        write(ap, "第二版内容")
        st = repo.status()
        assert st["modified"] == [rel], "改动没被认出来：%s" % st["modified"]
        print("PASS status_modified", flush=True)

        plan = repo.restore_plan(oid)
        assert plan["create"] == 0 and plan["overwrite"] == 1, \
            "恢复试算算错了：create=%d overwrite=%d" % (plan["create"], plan["overwrite"])
        print("PASS restore_plan", flush=True)

        repo.restore(oid, backup=False)
        assert read(ap) == "第一版内容", "恢复后的内容不对"
        print("PASS restore", flush=True)

        rep = repo.fsck()
        assert rep["ok"], "体检不通过：%s" % rep
        print("PASS fsck", flush=True)
    except Exception:                        # noqa: BLE001
        print("FAIL", flush=True)
        traceback.print_exc()
        return 1
    finally:
        rmtree_long(root)
        shutil.rmtree(fs_path(root), ignore_errors=True)
    print("ALL OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
