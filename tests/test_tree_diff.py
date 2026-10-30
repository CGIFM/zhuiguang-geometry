# -*- coding: utf-8 -*-
"""时间线上的「改了几个文件」必须和逐文件对比的结果一模一样

以前每条提交都调 diff()，两边各展开一次整棵树：三千个零件、四百条提交时
光这一个接口就要一秒（实测 985 ms）。现在只沿有差异的目录往下走。
快是好事，但数字要是算错了，用户看到的就是「这版明明没动几个零件，却写着改了 300 个」，
比慢严重得多——所以这里拿老办法的结果逐条对拍。
"""
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo            # noqa: E402


def old_counts(repo, commits):
    """老办法：逐提交 diff()，两边全展开"""
    out = {}
    for c in commits:
        parent = (c.get("parents") or [None])[0]
        try:
            if parent and repo.has(parent):
                d = repo.diff(parent, c["id"])
            elif not parent:
                d = repo.diff("", c["id"])
            else:
                continue
        except Exception:
            continue
        n = d["counts"]
        out[c["id"]] = (n["added"], n["modified"], n["deleted"])
    return out


def new_counts(repo, commits):
    out = {}
    for c in commits:
        parent = (c.get("parents") or [None])[0]
        tree_a = None
        if parent:
            try:
                tree_a = repo.commit_info(parent).get("tree")
            except Exception:
                continue
        a, m, d = repo.tree_diff_counts(tree_a, c.get("tree"))
        out[c["id"]] = (a, m, d)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-treediff-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("对比")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)


class ParityTest(Base):
    def test_every_kind_of_change(self):
        r = self.repo
        self.write("a/侧板.sldprt", "1")
        self.write("a/底板.sldprt", "1")
        self.write("b/顶板.sldprt", "1")
        self.write("根.sldprt", "1")
        r.commit("第一版")                                    # 全新增

        self.write("a/侧板.sldprt", "2")                      # 改一个
        r.commit("改一个")

        self.write("a/新件.sldprt", "1")                      # 新增一个
        r.commit("新增")

        os.remove(os.path.join(r.root, "b", "顶板.sldprt"))   # 删一个
        r.commit("删一个")

        shutil.rmtree(os.path.join(r.root, "b"))              # 删整个目录
        r.commit("删目录")

        self.write("新目录/深/里面.sldprt", "1")              # 新增整个目录树
        r.commit("加目录")

        # 文件和目录互换：不能凭空少算
        os.remove(os.path.join(r.root, "根.sldprt"))
        self.write("根.sldprt/子件.sldprt", "1")
        r.commit("文件变目录")

        self.write("a/侧板.sldprt", "3")                      # 内容变回旧值也算改动
        r.commit("再改")

        r.commit("什么都没改")                                # 空提交/无变化

        commits = r.log(100)
        self.assertGreaterEqual(len(commits), 3)
        old = old_counts(r, commits)
        new = new_counts(r, commits)
        self.assertEqual(new, old, "新旧两种算法的改动数必须一致")
        # 「文件变目录」那一步：根.sldprt 没了、根.sldprt/子件.sldprt 来了，
        # 老办法按路径比出来是 1 删 1 增，新办法必须一样
        self.assertIn((1, 0, 1), list(new.values()))

    def test_first_commit_counts_all_as_added(self):
        for i in range(5):
            self.write("件%d.sldprt" % i, "x")
        self.repo.commit("一")
        c = self.repo.log(1)[0]
        a, m, d = self.repo.tree_diff_counts(None, c["tree"])
        self.assertEqual((a, m, d), (5, 0, 0))

    def test_identical_tree_is_free(self):
        self.write("a/件.sldprt", "1")
        self.repo.commit("一")
        c = self.repo.log(1)[0]
        self.assertEqual(self.repo.tree_diff_counts(c["tree"], c["tree"]), (0, 0, 0))

    def test_counts_ignore_nested_same_trees(self):
        """没变的子目录不该被展开：换个 60 个目录的项目，随机改一个"""
        for i in range(60):
            self.repo_write_dir(i)
        self.repo.commit("一")
        self.write("d07/件.sldprt", "改了")
        self.repo.commit("二")
        c = self.repo.log(1)[0]
        p = c["parents"][0]
        tree_a = self.repo.commit_info(p)["tree"]
        self.assertEqual(self.repo.tree_diff_counts(tree_a, c["tree"]), (0, 1, 0))

    def repo_write_dir(self, i):
        self.write("d%02d/件.sldprt" % i, "x")


class DepthGuardTest(Base):
    """对象可能是对端推来的：几千层嵌套不能让请求线程爆栈"""

    def test_too_deep_tree_is_a_clean_error(self):
        from zgr.store import RepoError, MAX_TREE_DEPTH
        rel = "/".join("d%02d" % i for i in range(MAX_TREE_DEPTH + 8)) + "/件.sldprt"
        self.write(rel, "x")
        try:
            self.repo.commit("很深")
        except Exception as e:                     # noqa: BLE001
            self.skipTest("这个系统上建不出这么深的目录：%s" % e)
        c = self.repo.log(1)[0]
        with self.assertRaises(RepoError) as cm:
            self.repo.tree_diff_counts(None, c["tree"])
        self.assertIn("太深", str(cm.exception))
        with self.assertRaises(RepoError):
            self.repo.count_files(c["tree"])

    def test_normal_depth_is_fine(self):
        self.write("a/b/c/d/e/件.sldprt", "x")
        self.repo.commit("正常")
        c = self.repo.log(1)[0]
        self.assertEqual(self.repo.tree_diff_counts(None, c["tree"]), (1, 0, 0))


class ServerCountsTest(Base):
    """接口层：/api/log?changes=1 给的数字必须和逐条 diff 一致"""

    def setUp(self):
        super().setUp()
        self.write("a/侧板.sldprt", "1")
        self.write("b/底板.sldprt", "1")
        self.repo.commit("一")
        self.write("a/侧板.sldprt", "2")
        self.write("c/新.sldprt", "1")
        self.repo.commit("二")
        os.remove(os.path.join(self.root, "b", "底板.sldprt"))
        self.repo.commit("三")

    def test_attach_matches_reference(self):
        from zgr.server import Handler
        commits = self.repo.log(50)
        Handler._attach_change_counts(types.SimpleNamespace(app=None), self.repo, commits)
        got = {c["id"]: (c["changes"]["added"], c["changes"]["modified"],
                         c["changes"]["deleted"]) for c in commits}
        want = old_counts(self.repo, commits)
        self.assertEqual(got, want)
        # 界面要的总数也得在
        for c in commits:
            self.assertEqual(c["changes"]["total"], sum(c["changes"][k] for k in
                                                        ("added", "modified", "deleted")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
