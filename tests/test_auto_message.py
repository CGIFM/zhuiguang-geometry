# -*- coding: utf-8 -*-
"""不写说明时的默认说明：把「动了哪些零件」写进去

时间线上一整排「记录 2026-10-30 04:12:33」，过两天谁也认不出哪次是改哪张图的。
图纸目录里最常见的名字就是「侧板.sldprt」这种，写进说明就够认了。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class AutoMessageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-msg-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("说明")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def msg(self, oid):
        return self.repo.commit_info(oid)["message"]

    def test_lists_changed_parts_when_message_empty(self):
        self.write("侧板.sldprt", "a")
        self.write("底座.sldprt", "b")
        m1 = self.msg(self.repo.commit())
        self.assertIn("2 个文件", m1)
        self.assertIn("侧板.sldprt", m1)

        self.write("侧板.sldprt", "a2")
        m2 = self.msg(self.repo.commit(""))          # 空串也要自动写
        self.assertIn("1 个文件", m2)
        self.assertIn("侧板.sldprt", m2)
        self.assertNotIn("底座.sldprt", m2)

    def test_deleted_files_count_too(self):
        self.write("侧板.sldprt", "a")
        self.write("底座.sldprt", "b")
        self.repo.commit("第一版")
        os.remove(os.path.join(self.root, "底座.sldprt"))
        m = self.msg(self.repo.commit())
        self.assertIn("1 个文件", m)
        self.assertIn("底座.sldprt", m)

    def test_many_files_are_truncated(self):
        for i in range(6):
            self.write("零件%d.sldprt" % i, str(i))
        m = self.msg(self.repo.commit())
        self.assertIn("6 个文件", m)
        self.assertIn("等", m)
        self.assertLess(len(m.split("：")[1].split("（")[0]), 60)

    def test_explicit_message_is_kept(self):
        self.write("侧板.sldprt", "a")
        oid = self.repo.commit("客户确认的第三版")
        self.assertEqual(self.msg(oid), "客户确认的第三版")

    def test_no_change_still_returns_none(self):
        self.write("侧板.sldprt", "a")
        self.repo.commit("第一版")
        self.assertIsNone(self.repo.commit())


if __name__ == "__main__":
    unittest.main(verbosity=2)
