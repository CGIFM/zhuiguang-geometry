# -*- coding: utf-8 -*-
"""第 90 轮：合并前的「撞车预警」

一人一条线里，界面只告诉用户「对方有 3 个新版本」。合并确认框写着「冲突 2 个」，
可这句话回答不了用户真正要问的两件事：**是哪个零件**、**他什么时候改的**。
于是只有两个选择：硬合（赌自己那版更对），或者干脆不合（两条线越走越远）。

现在计划里带 `clashes` / `clash_detail`：同一个文件两边都动过，就把两边各自
「最近一次改它的人和时间」摆出来。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import merge as merge_mod   # noqa: E402
from zgr.store import Repo           # noqa: E402


class TreeEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-te-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("路径")
        os.makedirs(os.path.join(self.root, "零件", "组01"))
        for rel in ("a.txt", "零件/底盘.sldprt", "零件/组01/件001.sldprt"):
            p = os.path.join(self.root, *rel.split("/"))
            with open(p, "w", encoding="utf-8") as f:
                f.write(rel)
        self.oid = self.repo.commit("第一版", "我")
        self.tree = self.repo.commit_info(self.oid)["tree"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_nested_file(self):
        e = self.repo.tree_entry(self.tree, "零件/组01/件001.sldprt")
        self.assertTrue(e and e["oid"])
        self.assertEqual(e["size"], len("零件/组01/件001.sldprt".encode("utf-8")))

    def test_missing_returns_none(self):
        self.assertIsNone(self.repo.tree_entry(self.tree, "没有这个.txt"))
        self.assertIsNone(self.repo.tree_entry(self.tree, "零件/没有/件.sldprt"))

    def test_directory_is_not_a_file(self):
        self.assertIsNone(self.repo.tree_entry(self.tree, "零件"))
        self.assertIsNone(self.repo.tree_entry(self.tree, "零件/组01"))

    def test_empty_inputs(self):
        self.assertIsNone(self.repo.tree_entry("", "a.txt"))
        self.assertIsNone(self.repo.tree_entry(self.tree, ""))


class ClashTest(unittest.TestCase):
    """两边各改各的：只有真正撞在一起的文件才算撞车"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-clash-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("撞车")
        for n in ("a.txt", "b.txt", "c.txt", "d.txt"):
            self.write(n, "原始")
        base = self.repo.commit("共同起点", "老张")
        self.base = base
        # 我的线：改 a（只我改）、改 c（两边都改）、改 d（对方删了它）
        self.repo._write_text("HEAD", "mine")
        self.repo.set_branch("mine", base)
        self.write("a.txt", "我改的 a")
        self.write("c.txt", "我改的 c")
        self.write("d.txt", "我改的 d")
        self.mine = self.repo.commit("我这边", "老张")
        # 对方的线：改 b（只他改）、改 c（两边都改）、删 d（我改了它）
        self.repo._write_text("HEAD", "theirs")
        self.repo.set_branch("theirs", base)
        self.write("b.txt", "他改的 b")
        self.write("c.txt", "他改的 c")
        os.remove(os.path.join(self.root, "d.txt"))
        # 提交时把「记录人」换成对方的名字
        self.theirs = self.repo.commit("他那边", "张三")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def plan(self):
        return merge_mod.plan(self.repo, self.theirs, self.mine, detail=True)

    def test_only_both_touched_files_clash(self):
        p = self.plan()
        self.assertEqual(sorted(p["clashes"]), ["c.txt", "d.txt"], p["clashes"])
        self.assertNotIn("a.txt", p["clashes"])     # 只有我改
        self.assertNotIn("b.txt", p["clashes"])     # 只有他改
        self.assertIn("c.txt", p["conflicts"])      # 内容冲突
        # 删除撞车排在内容冲突后面
        self.assertEqual(p["clashes"][0], "c.txt")

    def test_detail_names_both_sides(self):
        p = self.plan()
        d = {x["path"]: x for x in p["clash_detail"]}
        self.assertIn("c.txt", d)
        self.assertEqual(d["c.txt"]["status"], "conflict")
        self.assertEqual(d["c.txt"]["ours"]["author"], "老张")
        self.assertEqual(d["c.txt"]["theirs"]["author"], "张三")
        self.assertEqual(d["c.txt"]["ours"]["commit"], self.mine)
        self.assertEqual(d["c.txt"]["theirs"]["commit"], self.theirs)
        self.assertTrue(d["c.txt"]["ours"]["time"])
        # 对方删、我改了：合并策略上归成 ours（保留文件），但它照样是一次撞车
        self.assertEqual(d["d.txt"]["status"], "ours")

    def test_deleted_side_has_no_touch_info(self):
        """对方把 d.txt 删了：那一侧的「最近一次改动」就是删除本身，仍然要能指出来"""
        p = self.plan()
        d = {x["path"]: x for x in p["clash_detail"]}
        self.assertEqual(d["d.txt"]["ours"]["author"], "老张")
        # 删除也是一次改动：要么给出时间和人，要么明确为空（不能是错的另一版）
        t = d["d.txt"]["theirs"]
        self.assertTrue(t is None or t.get("author") == "张三")

    def test_plan_without_detail_still_lists_clashes(self):
        p = merge_mod.plan(self.repo, self.theirs, self.mine)
        self.assertEqual(sorted(p["clashes"]), ["c.txt", "d.txt"])
        self.assertNotIn("clash_detail", p)

    def test_no_clash_when_sides_differ(self):
        """两个人改的是完全不同的文件：一条撞车都没有"""
        self.repo._write_text("HEAD", "mine")
        self.repo.set_branch("mine", self.mine)
        self.write("e.txt", "只有我改")
        mine2 = self.repo.commit("再记一版", "老张")
        p = merge_mod.plan(self.repo, self.theirs, mine2, detail=True)
        self.assertNotIn("e.txt", p["clashes"])
        self.assertNotIn("a.txt", p["clashes"])

    def test_times_are_recent(self):
        p = self.plan()
        for x in p["clash_detail"]:
            for side in ("ours", "theirs"):
                t = (x.get(side) or {}).get("time")
                if t:
                    self.assertLess(abs(time.time() * 1000 - t), 3600 * 1000)


if __name__ == "__main__":
    unittest.main()
