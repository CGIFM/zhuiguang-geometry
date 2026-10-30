# -*- coding: utf-8 -*-
r"""第 92 轮：长路径（Windows 260 字符上限）

167 上实测复现过：工作区里只要有一个文件的完整路径超过 260 字符，
「记录一版」就失败，而且报的是「有 1 个文件还在被写入（多半是 SolidWorks
刚点了保存、还没写完）」——完全指错方向，关掉 SolidWorks 一万次也没用。

现在所有真正交给系统的那一步（open / stat / makedirs / replace / walk）
统一走 `fs_path()`：过长的路径加 `\\?\` 前缀，绕过 260 的限制，不用改注册表、
不用管理员权限。万一还是打不开，就把「路径太长」单独认出来说清楚。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import doctor as doctor_mod          # noqa: E402
from zgr import live as live_mod              # noqa: E402
from zgr.store import Repo, RepoError, fs_path, _too_long_err  # noqa: E402

import contextlib                                     # noqa: E402

REAL_SCANDIR = os.scandir


@contextlib.contextmanager
def path_limit():
    """把「超过 260 字符就 206」这条真实限制装回来。

    本机 LongPathsEnabled=1，不加前缀也能扫到长路径，所以本机测试**验不出**
    「某一层目录太长就整棵子树消失」这个 bug——167 上是 0，实测真的丢过文件。
    这里让 scandir 对着没加前缀的超长路径抛 206，模拟 167 的行为。
    """
    if os.name != "nt":
        yield
        return

    def fake(path):
        p = os.fspath(path)
        if isinstance(p, bytes):
            p = p.decode("utf-8", "replace")
        if not p.startswith("\\\\?\\") and len(os.path.abspath(p)) > 260:
            e = OSError(206, "文件名或扩展名太长")
            e.winerror = 206
            raise e
        return REAL_SCANDIR(path)

    os.scandir = fake
    try:
        yield
    finally:
        os.scandir = REAL_SCANDIR


def write_raw(path, text):
    """按需加前缀地建文件：测试自己也要能造出超长路径"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(fs_path(d), exist_ok=True)
    with open(fs_path(path), "w", encoding="utf-8") as f:
        f.write(text)


def read_raw(path):
    with open(fs_path(path), "r", encoding="utf-8") as f:
        return f.read()


class FsPathTest(unittest.TestCase):
    def test_short_path_untouched(self):
        p = os.path.join(tempfile.gettempdir(), "a.txt")
        self.assertEqual(fs_path(p), os.path.abspath(p))

    def test_long_path_gets_prefix(self):
        if os.name != "nt":
            self.skipTest("只有 Windows 有 260 上限")
        p = "C:\\" + os.sep.join(["很长的目录名称"] * 40) + "\\a.txt"
        out = fs_path(p)
        self.assertTrue(out.startswith("\\\\?\\"))
        self.assertIn("很长的目录名称", out)
        # 前缀只能加一层：重复调用不能变成 \\?\C:\?\...
        self.assertEqual(fs_path(out), out)

    def test_unc_path_uses_unc_prefix(self):
        if os.name != "nt":
            self.skipTest("UNC 前缀只在 Windows 有意义")
        p = "\\\\服务器\\共享\\" + os.sep.join(["很长的目录名称"] * 40) + "\\a.txt"
        out = fs_path(p)
        self.assertTrue(out.startswith("\\\\?\\UNC\\"), out[:20])
        self.assertEqual(fs_path(out), out)

    def test_empty_ok(self):
        self.assertEqual(fs_path(""), "")


class TooLongErrTest(unittest.TestCase):
    def test_recognises_winerror_206(self):
        e = OSError(206, "文件名或扩展名太长")
        e.winerror = 206
        msg = _too_long_err(e, "C:\\" + "x" * 300)
        self.assertIn("路径太长", msg)
        self.assertIn("260", msg)

    def test_recognises_enametoolong(self):
        e = OSError(36, "File name too long")     # 36 = ENAMETOOLONG
        self.assertIn("路径太长", _too_long_err(e, "/tmp/" + "x" * 300))

    def test_other_errors_ignored(self):
        self.assertEqual(_too_long_err(PermissionError(13, "被占用"), "C:\\a.txt"), "")


class LongPathRepoTest(unittest.TestCase):
    """真刀真枪：在超过 260 字符的路径上建库、记录、看状态、恢复"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-lp-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.deep = os.sep.join(["很长的零件文件夹名称"] * 20)      # 约 200 字符
        self.rel = "/".join(["很长的零件文件夹名称"] * 20) + "/底盘.sldprt"
        self.ap = os.path.join(self.root, self.deep, "底盘.sldprt")
        self.repo = Repo(self.root).init("长路径")

    def tearDown(self):
        shutil.rmtree(fs_path(self.tmp), ignore_errors=True)

    def _check_len(self):
        if len(self.ap) <= 260:
            self.skipTest("临时目录太短（%d 字符），造不出超长路径" % len(self.ap))

    def test_commit_and_status_and_restore(self):
        self._check_len()
        write_raw(self.ap, "深路径第一版")
        oid = self.repo.commit("深路径", "我")
        self.assertTrue(oid)
        info, files = self.repo.files_of(oid)
        self.assertIn(self.rel, files)

        st = self.repo.status()
        self.assertTrue(st["clean"], st["counts"])
        self.assertEqual([x for x in st["skipped"] if x.get("long")], [])

        write_raw(self.ap, "深路径第二版")
        st2 = self.repo.status()
        self.assertEqual(st2["modified"], [self.rel])

        self.repo.restore(oid, backup=False)
        self.assertEqual(read_raw(self.ap), "深路径第一版")

    def test_restore_plan_sees_existing(self):
        self._check_len()
        write_raw(self.ap, "第一版")
        oid = self.repo.commit("第一版", "我")
        write_raw(self.ap, "第二版")
        plan = self.repo.restore_plan(oid)
        # 超长路径以前 stat 不到，会被算成「新增」，恢复前确认框里的账就是错的
        self.assertEqual(plan["create"], 0)
        self.assertEqual(plan["overwrite"], 1)

    def test_unwritable_long_path_does_not_break_commit(self):
        """没变的超长文件不应每次都被重新读（缓存命中路径也要能用）"""
        self._check_len()
        write_raw(self.ap, "内容")
        self.repo.commit("第一版", "我")
        self.assertIsNone(self.repo.commit("没改", "我"))    # 无变化 → 不产生新版本


class LongPathFatalTest(unittest.TestCase):
    """就算前缀也救不回来（比如网络盘/极端路径），也必须是「路径太长」而不是「正被写入」"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-lf-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("报错")
        with open(os.path.join(self.root, "a.sldprt"), "w", encoding="utf-8") as f:
            f.write("x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _boom(self, *a, **kw):
        e = OSError(206, "文件名或扩展名太长")
        e.winerror = 206
        raise e

    def test_commit_says_path_too_long(self):
        self.repo._store_file = self._boom
        with self.assertRaises(RepoError) as cm:
            self.repo.commit("记录", "我")
        msg = str(cm.exception)
        self.assertIn("路径太长", msg)
        self.assertIn("260", msg)
        # 绝不能再说成「SolidWorks 还没写完」
        self.assertNotIn("还在被写入", msg)

    def test_status_does_not_explode(self):
        self.repo._hash_file = self._boom
        st = self.repo.status()          # 只是看状态，不能连状态都看不了
        self.assertIn("counts", st)
        self.assertTrue(any(x.get("long") for x in st["skipped"]))


class DoctorLongPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-dl-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("体检")
        self.deep = os.sep.join(["很长的零件文件夹名称"] * 20)
        self.ap = os.path.join(self.root, self.deep, "零件.sldprt")

    def tearDown(self):
        shutil.rmtree(fs_path(self.tmp), ignore_errors=True)

    def test_warns_about_long_path(self):
        if len(self.ap) <= 260:
            self.skipTest("临时目录太短，造不出超长路径")
        write_raw(self.ap, "内容")
        rep = doctor_mod.report(self.repo)
        texts = " ".join(x["msg"] for x in rep["warn"])
        self.assertIn("260", texts)
        self.assertTrue(any(x.get("todo") for x in rep["warn"]))


class LiveBackupLongPathTest(unittest.TestCase):
    """回收站里的长路径：列清单、挑一个恢复都要能用"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-lb-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("回收站长路径")
        self.deep = os.sep.join(["很长的零件文件夹名称"] * 20)
        self.rel = "/".join(["很长的零件文件夹名称"] * 20) + "/底盘.sldprt"
        self.ap = os.path.join(self.root, self.deep, "底盘.sldprt")
        self.batch = "20261030-130000"

    def tearDown(self):
        shutil.rmtree(fs_path(self.tmp), ignore_errors=True)

    def test_list_and_restore_one(self):
        if len(self.ap) <= 260:
            self.skipTest("临时目录太短，造不出超长路径")
        bak = os.path.join(self.repo.meta, "backup", self.batch)
        write_raw(os.path.join(bak, self.deep, "底盘.sldprt"), "旧底盘")
        write_raw(self.ap, "新底盘")
        d = live_mod.backup_files(self.repo, self.batch)
        self.assertEqual([x["path"] for x in d["files"]], [self.rel])
        res = live_mod.restore_backup_file(self.repo, self.batch, self.rel)
        self.assertTrue(res["safety_backup"])
        self.assertEqual(read_raw(self.ap), "旧底盘")

    def test_restore_whole_batch(self):
        if len(self.ap) <= 260:
            self.skipTest("临时目录太短，造不出超长路径")
        bak = os.path.join(self.repo.meta, "backup", self.batch)
        write_raw(os.path.join(bak, self.deep, "底盘.sldprt"), "旧底盘")
        write_raw(self.ap, "新底盘")
        res = live_mod.restore_backup(self.repo, self.batch)
        self.assertEqual(res["count"], 1)
        self.assertEqual(read_raw(self.ap), "旧底盘")


class ScanLimitTest(unittest.TestCase):
    """扫描必须能下到很深的目录里（167 实测过：文件写出来了却记录不进去）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-sl-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("深目录")
        self.deep = os.sep.join(["很长的零件文件夹名称"] * 24)
        self.rel = "/".join(["很长的零件文件夹名称"] * 24) + "/底盘.sldprt"
        self.ap = os.path.join(self.root, self.deep, "底盘.sldprt")

    def tearDown(self):
        shutil.rmtree(fs_path(self.tmp), ignore_errors=True)

    def test_scan_finds_deep_file(self):
        if len(self.ap) <= 300:
            self.skipTest("临时目录太短，造不出足够深的路径")
        write_raw(self.ap, "深")
        with path_limit():
            found = self.repo.scan()
        self.assertIn(self.rel, found)

    def test_commit_includes_deep_file(self):
        if len(self.ap) <= 300:
            self.skipTest("临时目录太短，造不出足够深的路径")
        write_raw(self.ap, "深")
        with path_limit():
            oid = self.repo.commit("深目录", "我")
            _, files = self.repo.files_of(oid)
        self.assertIn(self.rel, files)


if __name__ == "__main__":
    unittest.main()
