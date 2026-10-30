# -*- coding: utf-8 -*-
"""项目体积账本：图纸 / 历史 / 回收站 / 打包各占多少

这个功能的用处只有一个——让人知道「点清理能省出多少」。
所以它必须**分得清、加得对、不把 .zgr 算进图纸里**。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class SizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-size-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("体积")
        self.write("a.sldprt", "x" * 1000)
        self.repo.commit("一")
        self.write("b.sldprt", "y" * 2000)
        self.repo.commit("二")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_worktree_excludes_meta(self):
        rep = self.repo.size_report()
        self.assertEqual(rep["worktree"]["files"], 2)
        self.assertEqual(rep["worktree"]["bytes"], 3000)      # 不多算 .zgr
        self.assertGreater(rep["objects"]["bytes"], 0)
        self.assertGreaterEqual(rep["total"], 3000)

    def test_objects_and_backup_counted_separately(self):
        bak = os.path.join(self.root, ".zgr", "backup", "改-20261030-010203")
        os.makedirs(bak)
        with open(os.path.join(bak, "a.sldprt"), "w", encoding="utf-8") as f:
            f.write("z" * 500)
        os.makedirs(os.path.join(self.root, ".zgr", "packs"), exist_ok=True)
        with open(os.path.join(self.root, ".zgr", "packs", "p.zip"), "wb") as f:
            f.write(b"p" * 300)
        rep = self.repo.size_report()
        self.assertEqual(rep["backup"]["bytes"], 500)
        self.assertEqual(rep["backup"]["batches"], 1)
        self.assertEqual(rep["packs"]["bytes"], 300)
        self.assertEqual(rep["worktree"]["bytes"], 3000)      # 备份不算进图纸
        # 各分项加起来不超过总数（其它项把它们都包在 .zgr 里）
        parts = (rep["objects"]["bytes"] + rep["backup"]["bytes"]
                 + rep["packs"]["bytes"] + rep["bom"]["bytes"])
        self.assertLessEqual(parts, rep["total"] + rep["meta_other"]["bytes"])

    def test_previews_counted_separately(self):
        """预览缓存单独成项：它是唯一「删了完全不影响版本」的大头"""
        pv = os.path.join(self.root, ".zgr", "previews", "aa")
        os.makedirs(pv)
        with open(os.path.join(pv, "x-512.png"), "wb") as f:
            f.write(b"P" * 400)
        rep = self.repo.size_report()
        self.assertEqual(rep["previews"]["bytes"], 400)
        self.assertEqual(rep["previews"]["files"], 1)
        parts = (rep["objects"]["bytes"] + rep["backup"]["bytes"]
                 + rep["packs"]["bytes"] + rep["bom"]["bytes"]
                 + rep["previews"]["bytes"] + rep["meta_other"]["bytes"])
        self.assertLessEqual(parts, rep["total"])

    def test_meta_other_never_negative(self):
        rep = self.repo.size_report()
        self.assertGreaterEqual(rep["meta_other"]["bytes"], 0)
        self.assertGreaterEqual(rep["meta_other"]["files"], 0)

    def test_budget_reports_partial(self):
        rep = self.repo.size_report(budget=-1)     # 时间预算已经用完
        self.assertTrue(rep["partial"])
        self.assertFalse(self.repo.size_report(budget=60)["partial"])

    def test_total_covers_all_parts_when_partial(self):
        """预算用完时合计不能小于各分项之和（否则占比条会超过 100%）"""
        rep = self.repo.size_report(budget=-1)
        self.assertTrue(rep["partial"])
        parts = [rep["worktree"], rep["objects"], rep["backup"],
                 rep["packs"], rep["bom"], rep["meta_other"]]
        self.assertGreaterEqual(rep["total"], sum(p["bytes"] for p in parts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
