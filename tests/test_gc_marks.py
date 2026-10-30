# -*- coding: utf-8 -*-
"""打了标记/标签的版本，就算分支被删了也不能被清理掉

这是「回收孤儿对象」最容易埋下的坑：标记只记在 marks.json 里，
分支一删，那个版本在可达性上就成了孤儿——GC 一跑，用户精心标了
「客户确认版」的那一版就永久消失了，而且没有任何提示。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class MarkedGcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-gcmark-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("标记")
        self.write("a.sldprt", "一")
        self.c1 = self.repo.commit("第一版")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def age_all(self, days=30):
        t = time.time() - days * 86400
        base = self.repo._p("objects")
        for d in os.listdir(base):
            sub = os.path.join(base, d)
            if not os.path.isdir(sub):
                continue
            for fn in os.listdir(sub):
                os.utime(os.path.join(sub, fn), (t, t))

    def test_marked_commit_survives_gc(self):
        self.write("b.sldprt", "客户确认的版本内容" * 20)
        c2 = self.repo.commit("客户确认版")
        self.repo.set_mark(c2, mark="green", tags=["客户确认"])
        # 把这个版本从分支上摘掉：只有标记还指着它
        self.repo._set_head(self.c1, self.repo.branch)
        self.assertIn(c2, self.repo.fsck()["dangling"])   # 分支没了：可达性上确实是孤儿
        self.age_all()
        plan = self.repo.gc(dry=True)
        self.assertGreaterEqual(plan.get("kept_marked", 0), 1)
        self.assertNotIn(c2, plan["sample"])
        self.repo.gc(dry=False)
        self.assertTrue(self.repo.has(c2), "打了标记的版本被清理掉了")
        # 那个版本的文件内容也必须还在（不然标记指向一个读不出来的版本）
        _c, files = self.repo.files_of(c2)
        self.assertTrue(any("b.sldprt" in p for p in files))
        for p in files.values():
            self.assertTrue(self.repo.has(p["oid"]), "标记版本里的文件对象被删了")

    def test_unmarked_orphan_still_collectable(self):
        """没打标记的孤儿照旧要能回收，别因为这条保护把 gc 变成摆设"""
        oid = self.repo.put("blob", b"nobody")
        self.age_all()
        rep = self.repo.gc(dry=False)
        self.assertGreaterEqual(rep["removed"], 1)
        self.assertFalse(self.repo.has(oid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
