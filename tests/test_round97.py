# -*- coding: utf-8 -*-
"""第 97 轮：历史长的时候，别让「今天改了什么」把整个历史翻一遍

「今天 3 个版本」这一行不能只数已加载的那一屏（大项目一次只取 400 版），
所以它要从分支头往回走、数落在今天的提交（`count_since`）。这条路和另外两处
广度优先（时间线的分支树、全历史搜索的索引）以前都用 `list.pop(0)` 出队——
历史越长，光挪列表就是 O(n²)。这轮换成 `deque`，并把这些遍历的正确性钉死：
顺序、截断标记、剪枝边界一个都不能变。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import cli as cli_mod          # noqa: E402
from zgr.store import Repo              # noqa: E402


class DeepHistoryTest(unittest.TestCase):
    """长历史只需要造一次：下面每个用例都只读，重建 6 遍纯属浪费（一轮多花 3 分钟）"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zgr-deep97-")
        cls.root = os.path.join(cls.tmp, "项目")
        os.makedirs(cls.root)
        cls.repo = Repo(cls.root).init("长历史")
        cls.n = 200
        for i in range(cls.n):
            with open(os.path.join(cls.root, "a.txt"), "w", encoding="utf-8") as f:
                f.write("v%d\n" % i)
            cls.repo.commit("第 %d 版" % i, "测试")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_log_graph_returns_newest_first(self):
        out, heads = self.repo.log_graph(50)
        self.assertEqual(len(out), 50)
        self.assertEqual(out[0]["message"], "第 %d 版" % (self.n - 1))
        times = [c["time"] for c in out]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(heads), 1)

    def test_log_graph_has_more_flag(self):
        _out, _heads, more = self.repo.log_graph_ex(50)
        self.assertTrue(more)
        out, _heads, more = self.repo.log_graph_ex(self.n + 10)
        self.assertEqual(len(out), self.n)
        self.assertFalse(more)

    def test_count_since_counts_whole_history(self):
        """「今天」这一行必须数全部历史，而不是已加载的那一屏"""
        since = int((time.time() - 3600) * 1000)
        n, cut = self.repo.count_since(since)
        self.assertEqual(n, self.n)
        self.assertFalse(cut)

    def test_count_since_ignores_old_commits(self):
        since = int((time.time() + 3600) * 1000)      # 未来：今天一个都没有
        n, cut = self.repo.count_since(since)
        self.assertEqual(n, 0)
        self.assertFalse(cut)

    def test_commits_since_is_sorted_desc(self):
        rows, cut = self.repo.commits_since(int((time.time() - 3600) * 1000))
        self.assertEqual(len(rows), self.n)
        self.assertFalse(cut)
        times = [c["time"] for c in rows]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_cap_marks_truncated(self):
        rows, cut = self.repo.commits_since(int((time.time() - 3600) * 1000), cap=10)
        self.assertEqual(len(rows), 10)
        self.assertTrue(cut)


class BranchAncestorsTest(unittest.TestCase):
    """合并用得到「共同祖先是哪个」：这里也换成了 deque，距离算错会合错分支"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-anc97-")
        self.repo = Repo(self.tmp).init("分支")
        p = os.path.join(self.tmp, "a.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
        self.base = self.repo.commit("基", "测试")
        self.repo.set_branch("side", self.base)      # 从「基」分出 side
        with open(p, "w", encoding="utf-8") as f:
            f.write("2")
        self.side = self.repo.commit("侧", "测试", branch="side")
        self.repo.set_branch("main", self.base)
        with open(p, "w", encoding="utf-8") as f:
            f.write("3")
        self.main = self.repo.commit("主", "测试", branch="main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ancestors_distances(self):
        from zgr.merge import ancestors
        d = ancestors(self.repo, self.main)
        self.assertEqual(d[self.main], 0)
        self.assertEqual(d[self.base], 1)

    def test_common_ancestor_found_from_both_heads(self):
        from zgr.merge import ancestors
        a = ancestors(self.repo, self.main)
        b = ancestors(self.repo, self.side)
        common = set(a) & set(b)
        self.assertIn(self.base, common)


class LogChangesCliTest(unittest.TestCase):
    """`zgr log -c`：命令行也能看到「这一版改了 3 个文件」，和网页时间线同一个数"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-logc97-")
        self.repo = Repo(self.tmp).init("日志")
        p = os.path.join(self.tmp, "a.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
        self.repo.commit("第一版", "测试")
        with open(p, "w", encoding="utf-8") as f:
            f.write("22")
        with open(os.path.join(self.tmp, "b.txt"), "w", encoding="utf-8") as f:
            f.write("new")
        self.repo.commit("第二版", "测试")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_mod.main(argv)
        return rc, buf.getvalue()

    def test_shows_change_counts(self):
        rc, text = self._run(["log", self.tmp, "-c"])
        self.assertEqual(rc, 0)
        self.assertIn("改 2：+1 ~1 -0", text)     # 第二版：新增 b.txt、改了 a.txt
        self.assertIn("改 1：+1 ~0 -0", text)     # 第一版：全部是新增

    def test_without_flag_no_change_column(self):
        rc, text = self._run(["log", self.tmp])
        self.assertEqual(rc, 0)
        self.assertNotIn("改 1：", text)


class DayCountCacheTest(unittest.TestCase):
    """「今天几版」按分支头记忆：反复问不重走历史，一有新版本立刻重算"""

    def setUp(self):
        from zgr.registry import Registry
        from zgr.server import App
        self.tmp = tempfile.mkdtemp(prefix="zgr-day97-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("今天")
        self.app = App(Registry(path=os.path.join(self.tmp, "projects.json")))
        self.since = int((time.time() - 3600) * 1000)

    def tearDown(self):
        for w in list(self.app.watchers.values()):
            w.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, text):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        self.repo.commit(text, "测试")

    def test_repeat_call_does_not_walk_again(self):
        from unittest import mock
        self._commit("一")
        first = self.app.day_count(self.repo, self.since)
        self.assertEqual(first, (1, False))
        with mock.patch.object(Repo, "count_since",
                               side_effect=AssertionError("又走了一遍历史")):
            again = self.app.day_count(self.repo, self.since)
        self.assertEqual(again, first, "分支头没动就必须直接给上次的答案")

    def test_new_commit_recounts(self):
        self._commit("一")
        self.assertEqual(self.app.day_count(self.repo, self.since)[0], 1)
        self._commit("二")
        self.assertEqual(self.app.day_count(self.repo, self.since)[0], 2,
                         "记了新版本还报旧数字比慢更糟")

    def test_another_day_recomputes(self):
        self._commit("一")
        self.app.day_count(self.repo, self.since)
        future = int((time.time() + 3600) * 1000)
        self.assertEqual(self.app.day_count(self.repo, future)[0], 0)


class SolidWorksSafetyTest(unittest.TestCase):
    """BOM 是唯一会「借用」用户正开着的 SolidWorks 的功能：
    收尾只能关我们自己打开的文档，绝不关用户手上那份，也绝不退出用户的实例。"""

    def test_bom_only_closes_doc_it_opened(self):
        from zgr import bom
        v = bom._VBS
        self.assertIn("GetOpenDocumentByName", v,
                      "打开前必须判断这份装配体是不是本来就在")
        self.assertIn("If Not wasOpen Then sw.CloseDoc", v,
                      "本来就在的文档一个字都不许关")

    def test_bom_never_exits_user_instance(self):
        from zgr import bom
        v = bom._VBS
        self.assertIn("If Not hadInst Then sw.ExitApp", v)
        # 注释里写着「绝不 CloseAllDocuments」是给下一个人看的，不算调用：
        # 只看真正会执行的代码行
        code = [ln.strip() for ln in v.splitlines() if ln.strip() and not ln.strip().startswith("'")]
        bad = [ln for ln in code if "CloseAllDocuments" in ln]
        self.assertEqual(bad, [], "绝不一把全关：%s" % bad[:2])

    def test_step_and_mesh_refuse_when_solidworks_running(self):
        """STEP 导出和 3D 预览遇到用户开着 SolidWorks 要直接拒绝，不去抢它"""
        from zgr import mesh, swstep

        def src(mod):
            with open(mod.__file__, encoding="utf-8") as f:
                return f.read()

        self.assertIn("正开着", mesh._VBS + src(mesh))
        self.assertIn("正开着", src(swstep))


class DiskMessageTest(unittest.TestCase):
    """「盘整体快满」和「这一次改动太大」是两件事，提示必须说对是哪一件

    今天真机上撞到过：盘只剩 354 MB，改动只有几百字节，提示却说
    「这次记录大约要写 0 MB，还要留 537 MB 余量」——看着像软件坏了。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-disk97-")
        self.repo = Repo(self.tmp).init("盘")
        with open(os.path.join(self.tmp, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _disk_of(self, free):
        from zgr.store import MIN_FREE_BYTES

        def f(_self):
            return {"free": free, "total": 200 << 30,
                    "low": free < MIN_FREE_BYTES, "min_free": MIN_FREE_BYTES}
        return f

    def test_whole_disk_full_says_so(self):
        """整盘低于安全线：要说「整块盘快满了」，而不是「这次要写 0 MB」"""
        from unittest import mock
        from zgr.store import MIN_FREE_BYTES, RepoError
        with mock.patch.object(Repo, "disk", new=self._disk_of(MIN_FREE_BYTES // 3)):
            with self.assertRaises(RepoError) as cm:
                self.repo.commit("记一下")
        msg = str(cm.exception)
        self.assertIn("整块盘快满了", msg)
        self.assertIn("不是改动太大", msg)
        self.assertIn("体积", msg, "仍要告诉用户去哪儿腾地方")
        self.assertNotIn("大约要写", msg)

    def test_big_change_says_change_is_big(self):
        """余量够、是这一次改动太大：必须说清是改动本身写不下"""
        from unittest import mock
        from zgr.store import MIN_FREE_BYTES, RepoError
        need = 200 << 20
        with mock.patch.object(Repo, "disk", new=self._disk_of(MIN_FREE_BYTES + (50 << 20))):
            with mock.patch.object(Repo, "pending_bytes",
                                   new=lambda _self, scanned=None: need):
                with self.assertRaises(RepoError) as cm:
                    self.repo.commit("记一下")
        msg = str(cm.exception)
        self.assertIn("这次记录要写", msg)
        self.assertIn("安全线", msg)
        self.assertNotIn("整块盘快满了", msg)


if __name__ == "__main__":
    unittest.main()
