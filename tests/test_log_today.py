# -*- coding: utf-8 -*-
"""log_graph_ex 的「还有更早的」和 count_since 的「今天几版」

界面上一行「今天 N 个版本」要的是今天的**全部**版本，不是时间线那一屏里的。
两条都容易在边界上说谎：
- 项目正好有「一屏」那么多版本时，`条数 >= 上限` 会一直说还有更早的；
- 一人一条线、分支一堆时，那一屏里今天和上个月混着，数出来既不是今天也不是全部。
"""
import os
import shutil
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import zgr.store as store                        # noqa: E402
from zgr.store import Repo                       # noqa: E402

DAY = 86400 * 1000


def day_start(back_days=0):
    import time
    t = time.localtime()
    return int(time.mktime(tuple(t[:3]) + (0, 0, 0, 0, 0, -1)) * 1000) - back_days * DAY


class LogTodayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-logtoday-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("数今天")
        self._real = store.now_ms

    def tearDown(self):
        store.now_ms = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg, back_days=0):
        pid = len(os.listdir(self.root))
        with open(os.path.join(self.root, "f%d.txt" % pid), "w", encoding="utf-8") as f:
            f.write("x")
        store.now_ms = (lambda b=back_days, r=self._real: (lambda: r() - b * DAY))()
        try:
            return self.repo.commit(msg, "我")
        finally:
            store.now_ms = self._real

    # ---------------------------------------------------------------- has_more
    def test_has_more_false_when_exactly_limit(self):
        for i in range(4):
            self.commit("第 %d 版" % (i + 1))
        commits, _heads, has_more = self.repo.log_graph_ex(4)
        self.assertEqual(len(commits), 4)
        self.assertFalse(has_more, "正好一屏时不该说还有更早的")

    def test_has_more_true_when_truncated(self):
        for i in range(5):
            self.commit("第 %d 版" % (i + 1))
        commits, _heads, has_more = self.repo.log_graph_ex(4)
        self.assertEqual(len(commits), 4)
        self.assertTrue(has_more)

    def test_has_more_false_on_merge_at_exact_limit(self):
        # 合并的基点会被两个子提交各入队一次：取满一屏时队列里可能只剩重复 oid，
        # 那不是「还有更早的」——老写法在这里会永远显示「更早的还没加载」
        a = self.commit("A")
        b = self.commit("B")
        m = self.repo.commit_tree(self.repo.commit_info(b)["tree"], [a, b], "M", "我")
        self.repo.set_branch("main", b)
        self.repo.set_branch("other", m)
        commits, _h, has_more = self.repo.log_graph_ex(3)
        self.assertEqual(len(commits), 3)
        self.assertFalse(has_more, "总共就 3 个版本，取 3 个不该说还有更早的")

    def test_log_graph_backwards_compatible(self):
        self.commit("一版")
        out = self.repo.log_graph(10)
        self.assertEqual(len(out), 2, "log_graph 仍然返回两个值")
        self.assertEqual(len(out[0]), 1)

    # -------------------------------------------------------------- count_since
    def test_counts_only_today(self):
        for i in range(3):
            self.commit("昨天 %d" % i, back_days=1)
        for i in range(2):
            self.commit("今天 %d" % i)
        n, cut = self.repo.count_since(day_start())
        self.assertEqual(n, 2)
        self.assertFalse(cut)

    def test_counts_across_branches_not_limited_by_window(self):
        # 一条老分支（全是上个月）+ 主线今天 6 版：数今天不能受「一屏只有 4 个」影响
        for i in range(4):
            self.commit("老分支 %d" % i, back_days=30)
        self.repo.set_branch("side", self.repo.head_oid())
        for i in range(6):
            self.commit("主线 %d" % i)
        n, _cut = self.repo.count_since(day_start())
        self.assertEqual(n, 6)
        # 时间线只拿 4 个时，今天的版本数不到 6——这就是为什么不能数这一屏
        commits, _h, _m = self.repo.log_graph_ex(4)
        today_in_window = [c for c in commits if (c.get("time") or 0) >= day_start()]
        self.assertLess(len(today_in_window), 6)

    def test_future_timestamps_not_counted_as_today(self):
        # 对端机器时钟快，提交时间跑到明天：那是「明天」的版本，不是今天的
        self.commit("今天的")
        self.commit("明天的", back_days=-1)
        n, _cut = self.repo.count_since(day_start(), day_start() + DAY)
        self.assertEqual(n, 1)

    def test_missing_time_still_walks_parents(self):
        # 时间戳丢了（t=0）的提交既不能算，也不能据此停止往上走，
        # 否则它下面今天真正记的版本会整条被剪掉
        import json
        a = self.commit("今天的一版")
        tree = self.repo.commit_info(a)["tree"]
        payload = json.dumps({"tree": tree, "parents": [a], "time": 0,
                              "author": "我", "message": "时钟坏了", "stats": {}},
                             ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        bad = self.repo.put("commit", payload)
        self.repo.set_branch("main", bad)
        n, _cut = self.repo.count_since(day_start(), day_start() + DAY)
        self.assertEqual(n, 1, "跳过一个坏时间戳的提交后，它下面今天的版本还得数到")

    def test_cap_reports_truncation(self):
        for i in range(5):
            self.commit("今天 %d" % i)
        n, cut = self.repo.count_since(day_start(), cap=2)
        self.assertTrue(cut)
        self.assertLess(n, 5)

    def test_empty_repo_is_zero(self):
        n, cut = self.repo.count_since(day_start())
        self.assertEqual(n, 0)
        self.assertFalse(cut)

    def test_merge_commit_counts_once(self):
        a = self.commit("主线一版")
        self.repo.set_branch("side", a)
        b = self.commit("旁支一版")
        self.repo.set_branch("main", a)
        # 直接造一个合并提交：两个父
        tree = self.repo.commit_info(b)["tree"]
        m = self.repo.commit_tree(tree, [a, b], "合并", "我")
        self.repo.set_branch("main", m)
        n, _cut = self.repo.count_since(day_start())
        self.assertEqual(n, 3, "合并提交只算一次，父提交各算一次")


if __name__ == "__main__":
    unittest.main()
