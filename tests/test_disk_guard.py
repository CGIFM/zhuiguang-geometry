# -*- coding: utf-8 -*-
"""记录之前先看盘：写不下就别动文件，并说清楚怎么腾地方

以前完全没有这道检查。盘快满的时候，记录会在写到一半时抛出「没有磁盘空间」，
对象库里留下半截文件，用户看到的是一句看不懂的系统错误——而且 SolidWorks
那边紧接着也会存盘失败，比「这次没记上」严重得多。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import store as store_mod          # noqa: E402
from zgr.store import Repo, RepoError       # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-disk-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("盘")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.repo.commit("一")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_disk(self, free):
        return {"free": free, "total": 100 << 30, "low": free < store_mod.MIN_FREE_BYTES,
                "min_free": store_mod.MIN_FREE_BYTES}


class DiskReportTest(Base):
    def test_disk_reads_real_drive(self):
        d = self.repo.disk()
        self.assertGreater(d["free"], 0)
        self.assertGreaterEqual(d["total"], d["free"])
        self.assertIn("low", d)
        self.assertIn("min_free", d)

    def test_disk_failure_is_not_fatal(self):
        with mock.patch.object(store_mod.shutil, "disk_usage",
                               side_effect=OSError("没有这个盘")):
            d = self.repo.disk()
        self.assertEqual(d["free"], -1)
        self.assertFalse(d["low"], "读不到余量时绝不能拦任何操作")

    def test_status_carries_disk(self):
        st = self.repo.status()
        self.assertIn("disk", st)
        self.assertGreater(st["disk"]["free"], 0)


class PendingBytesTest(Base):
    def test_zero_when_clean(self):
        self.assertEqual(self.repo.pending_bytes(), 0)

    def test_counts_changed_files(self):
        p = os.path.join(self.root, "a.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("二" * 500)
        need = self.repo.pending_bytes()
        self.assertGreaterEqual(need, 1024, "改过的文件要算进这次要写的量")
        with open(os.path.join(self.root, "新.bin"), "wb") as f:
            f.write(b"x" * 4096)
        self.assertGreaterEqual(self.repo.pending_bytes(), need + 4096)


class CommitGuardTest(Base):
    def test_blocks_when_disk_too_small(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("二" * 5000)
        before = self.repo.head_oid()
        with mock.patch.object(Repo, "disk", new=_disk_of(1024)):
            with self.assertRaises(RepoError) as cm:
                self.repo.commit("塞不下")
        msg = str(cm.exception)
        self.assertIn("盘上只剩", msg)
        self.assertIn("体积", msg, "要告诉用户去哪儿腾地方")
        self.assertEqual(self.repo.head_oid(), before, "被拦住时一个版本都不该多出来")

    def test_allows_when_disk_is_fine(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("三" * 5000)
        with mock.patch.object(Repo, "disk", new=_disk_of(10 << 30)):
            oid = self.repo.commit("写得下")
        self.assertTrue(oid)

    def test_unknown_free_space_never_blocks(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("四" * 5000)
        with mock.patch.object(Repo, "disk", new=_disk_of(-1)):
            oid = self.repo.commit("读不到余量也要能记")
        self.assertTrue(oid)

    def test_clean_workspace_skips_check(self):
        """没有改动时不该因为盘小就去拦（本来也不用写什么）"""
        with mock.patch.object(Repo, "disk", new=_disk_of(1)):
            self.assertIsNone(self.repo.commit("没变化"))


def _disk_of(free):
    def _inner(self):
        return {"free": free, "total": 100 << 30,
                "low": 0 <= free < store_mod.MIN_FREE_BYTES,
                "min_free": store_mod.MIN_FREE_BYTES}
    return _inner


if __name__ == "__main__":
    unittest.main(verbosity=2)
