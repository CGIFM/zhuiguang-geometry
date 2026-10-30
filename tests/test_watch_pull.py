# -*- coding: utf-8 -*-
"""命令行自动记录：有「待更新」标记时一版都不能记

网页服务里的自动记录早就会让开这个状态，`zgr watch` 以前不会。
后果是一条完整的丢数据链：同步时图纸被占着 → 分支前进、工作区还是旧的 → 留了待更新标记
→ 命令行自动记录把旧工作区记成「新版本」→ 再被快进推回对方。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.cli import watch_step            # noqa: E402
from zgr.store import Repo                # noqa: E402


class WatchStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-watch-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("自动")
        self.write("侧板.sldprt", "一")
        self.repo.commit("第一版")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def marker(self):
        with open(os.path.join(self.repo.meta, "PULL_NEEDED"), "w", encoding="utf-8") as f:
            json.dump({"head": self.repo.head_oid(), "reason": "工作区更新失败"}, f)

    def test_commits_when_there_is_a_change(self):
        self.write("侧板.sldprt", "二")
        oid = watch_step(self.repo)
        self.assertIsNotNone(oid)

    def test_nothing_to_commit(self):
        self.assertIsNone(watch_step(self.repo))

    def test_never_commits_while_pull_is_pending(self):
        head = self.repo.head_oid()
        self.write("侧板.sldprt", "旧内容（对方的新版本还没落地）")
        self.marker()
        self.assertIsNone(watch_step(self.repo),
                          "有「待更新」时记录了：会把对方改动记成一次「改回去」")
        self.assertEqual(self.repo.head_oid(), head)

    def test_resumes_after_marker_cleared(self):
        self.write("侧板.sldprt", "三")
        self.marker()
        self.assertIsNone(watch_step(self.repo))
        os.remove(os.path.join(self.repo.meta, "PULL_NEEDED"))
        self.assertIsNotNone(watch_step(self.repo), "标记清掉后应该自动继续")


if __name__ == "__main__":
    unittest.main(verbosity=2)
