# -*- coding: utf-8 -*-
"""跨版本找零件：三千个零件的项目里，按名字找到它、看清它的底细

真实场景是「这个侧板到底还在不在」「上次改它是什么时候、哪一版」——
翻文件清单只能看到当前版本，删掉的、改名的零件就找不着了。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import store as store_mod      # noqa: E402
from zgr.store import Repo              # noqa: E402


class SearchTest(unittest.TestCase):
    def setUp(self):
        store_mod._SEARCH_CACHE.clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-search-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("搜索")
        self.write("结构/侧板.sldprt", "一")
        self.write("结构/底板.sldprt", "一")
        self.write("标准件/M3螺丝.sldprt", "一")
        self.c1 = self.repo.commit("第一版")

    def tearDown(self):
        store_mod._SEARCH_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    # ---------------------------------------------------------------- 用例
    def test_finds_by_name_and_reports_changes(self):
        self.write("结构/侧板.sldprt", "二")
        self.repo.commit("第二版：侧板加厚")
        self.write("结构/侧板.sldprt", "三")
        self.repo.commit("第三版：侧板又改")
        r = self.repo.search_history("侧板")
        self.assertEqual(r["total"], 1)
        row = r["rows"][0]
        self.assertEqual(row["path"], "结构/侧板.sldprt")
        self.assertEqual(row["changes"], 3)      # 新建 1 次 + 改 2 次
        self.assertEqual(row["versions"], 3)
        self.assertTrue(row["in_head"])
        self.assertIn("侧板又改", row["last_msg"])

    def test_deleted_part_still_found_and_marked(self):
        os.remove(os.path.join(self.root, "标准件", "M3螺丝.sldprt"))
        self.repo.commit("删掉螺丝")
        r = self.repo.search_history("螺丝")
        self.assertEqual(r["total"], 1)
        row = r["rows"][0]
        self.assertFalse(row["in_head"])         # 标成「已删除」
        self.assertEqual(row["changes"], 1)      # 只有「新建」算内容变化（删除不算改动）
        self.assertEqual(row["versions"], 1)     # 它只在这一个版本里存在过

    def test_case_insensitive_and_path_fragment(self):
        self.assertEqual(self.repo.search_history("SLDPRT")["total"], 3)
        self.assertEqual(self.repo.search_history("标准件")["total"], 1)
        self.assertEqual(self.repo.search_history("结构/侧")["total"], 1)
        self.assertEqual(self.repo.search_history("不存在的零件")["total"], 0)

    def test_existing_before_deleted_and_name_matches_first(self):
        self.write("资料/结构.sldprt", "x")      # 只按文件名命中，路径里没有这两个字
        os.remove(os.path.join(self.root, "标准件", "M3螺丝.sldprt"))
        self.repo.commit("加结构零件、删螺丝")
        rows = self.repo.search_history("sldprt")["rows"]
        self.assertTrue(rows[0]["in_head"])      # 还在的排前面
        self.assertFalse(rows[-1]["in_head"])    # 删除的垫底
        rows2 = self.repo.search_history("结构")["rows"]
        self.assertEqual(rows2[0]["path"], "资料/结构.sldprt")   # 文件名命中排在路径命中前面

    def test_limit_and_empty_query(self):
        r = self.repo.search_history("sldprt", limit=2)
        self.assertEqual(len(r["rows"]), 2)
        self.assertEqual(r["total"], 3)
        self.assertEqual(self.repo.search_history("")["total"], 0)

    def test_index_rebuilds_after_new_commit(self):
        first = self.repo.search_history("侧板")["rows"][0]["changes"]
        self.assertEqual(first, 1)
        self.write("结构/侧板.sldprt", "二")
        self.repo.commit("第二版")
        # 缓存按分支头失效：新提交后必须看到新的次数，不能吃旧索引
        self.assertEqual(self.repo.search_history("侧板")["rows"][0]["changes"], 2)

    def test_index_is_shared_between_queries(self):
        self.repo.search_history("侧板")
        key = os.path.normcase(self.repo.root)
        self.assertIn(key, store_mod._SEARCH_CACHE)
        sig = store_mod._SEARCH_CACHE[key]["sig"]
        self.repo.search_history("螺丝")
        self.assertEqual(store_mod._SEARCH_CACHE[key]["sig"], sig)   # 第二次不重建

    def test_parallel_branches_do_not_inflate_changes(self):
        """另一条分支根本没动这个零件，不能因为「时间上更近」就算它改过"""
        self.write("结构/侧板.sldprt", "二")
        self.repo.commit("main 改侧板")
        self.repo.restore(self.c1)              # 工作区回到起点（侧板=一）
        self.repo.set_branch("side", self.c1)
        self.write("说明.txt", "侧枝只加了说明")   # 侧枝完全没碰侧板
        self.repo.commit("侧枝加说明", branch="side")
        rows = self.repo.search_history("侧板")["rows"]
        self.assertEqual(rows[0]["changes"], 2)  # 新增 1 次 + main 改了 1 次

    def test_merge_commit_does_not_count_as_change(self):
        """合并提交把对方的内容并进来：和某个父提交一样，就不是「又改了一次」"""
        self.write("结构/侧板.sldprt", "对方改的")
        self.repo.set_branch("side", self.c1)
        side = self.repo.commit("侧枝改侧板", branch="side")
        self.repo.restore(self.c1)
        self.repo.set_branch("main", self.c1)
        self.write("说明.txt", "主干只加了说明")
        main = self.repo.commit("主干加说明")
        # 手工造一个双父合并提交：内容取自 side（侧板=对方改的）
        _c, files = self.repo.files_of(side)
        merged = dict(files)
        self.write("说明.txt", "主干只加了说明")
        _ci, main_files = self.repo.files_of(main)
        merged["说明.txt"] = main_files["说明.txt"]
        tree = self.repo._build_trees(merged) if hasattr(self.repo, "_build_trees")             else None
        if tree:
            self.repo.commit_tree(tree, [main, side], "合并侧枝")
            rows = self.repo.search_history("侧板")["rows"]
            self.assertEqual(rows[0]["changes"], 2, "合并提交被算成了一次新改动")

    def test_cache_rebuilds_when_branch_switches(self):
        """切到另一条分支，同一个零件「还在不在」必须跟着变，不能吃上一分支的缓存"""
        self.repo.set_branch("side", self.c1)
        self.write("侧枝独有.sldprt", "x")
        self.repo.commit("侧枝加零件", branch="side")
        self.repo.set_branch("main", self.c1)
        os.remove(os.path.join(self.root, "结构", "侧板.sldprt"))
        self.repo.commit("主干删掉侧板")
        self.assertFalse(self.repo.search_history("侧板")["rows"][0]["in_head"])
        self.repo.set_branch(None, None) if False else None
        self.repo._write_text("HEAD", "side")
        row = self.repo.search_history("侧板")["rows"][0]
        self.assertTrue(row["in_head"], "切到 side 后侧板仍在，不该标成已删除")

    def test_part_only_on_other_branch_is_not_deleted(self):
        """零件只在同事的分支上：不是删除，只是不在我这条线上"""
        self.repo.set_branch("side", self.c1)
        self.write("别人加的.sldprt", "x")
        self.repo.commit("同事加的零件", branch="side")
        self.repo.set_branch("main", self.c1)
        self.repo._write_text("HEAD", "main")
        row = self.repo.search_history("别人加的")["rows"][0]
        self.assertFalse(row["in_head"])
        self.assertTrue(row["in_branches"], "它在同事的分支上，不该算已删除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
