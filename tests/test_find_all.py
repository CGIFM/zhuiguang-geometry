# -*- coding: utf-8 -*-
"""全历史搜索接口 /api/find

界面上时间线一次只取几百个版本，搜索框以前只过滤这一屏。这个接口要保证：
**翻的是全部历史**（所有分支可达的提交），并且如实回答「一共翻了多少个」。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class FindAllTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-find-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("翻旧账")
        self.ids = {}
        for i, m in enumerate(["最早的一版", "客户确认的第三版", "改料盘", "调尺寸"]):
            with open(os.path.join(self.root, "件%d.txt" % i), "w", encoding="utf-8") as f:
                f.write("v%d" % i)
            self.ids[m] = self.repo.commit(m)

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        if hasattr(reg, "add"):
            reg.add(self.root, "翻旧账")
        self.app = App(reg)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def find(self, **params):
        params.setdefault("path", self.root)
        q = urllib.parse.urlencode(params)
        with urllib.request.urlopen(self.base + "/api/find?" + q, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_remove_project_drops_cached_index(self):
        """项目从列表里去掉，按目录做的索引也该跟着丢

        留着它，同路径再建一个项目时可能先读到旧项目的索引，搜索会报出
        这个新项目里根本不存在的版本。"""
        key = os.path.normcase(os.path.abspath(self.root))
        self.find(q="预热")
        self.assertIn(key, self.app._find_index)
        self.post("/api/remove", {"path": self.root})
        self.assertNotIn(key, self.app._find_index)
        self.assertNotIn(key, self.app._status_cache)

    def test_long_message_matches_past_200_chars(self):
        """说明第一行很长时，关键词在第 200 字之后也要搜得到

        索引以前只存前 200 个字还拿它做匹配，而命令行比的是全文——
        于是「网页搜不到、命令行搜得到」，正是这一轮要消灭的那种不一致。"""
        long_msg = "前置说明" * 60 + "末尾关键词在这里"       # 第 244 字之后
        with open(os.path.join(self.root, "长说明.txt"), "w", encoding="utf-8") as f:
            f.write("z")
        oid = self.repo.commit(long_msg)
        d = self.find(q="末尾关键词")
        self.assertEqual([h["id"] for h in d["hits"]], [oid])
        # 回给界面的说明仍然截到 200 字，别把一整段塞进列表
        self.assertLessEqual(len(d["hits"][0]["message"]), 200)

    def test_head_advancing_beyond_cap_does_not_wipe_index(self):
        """分支头前进了、而这次 scan 很小：不能把它当成「分支被删」把索引清掉

        重建的代价是整段历史重读一遍；误判一次就把增量索引的收益全丢了。"""
        self.find(q="预热", scan=20000)                 # 建好 4 行索引
        key = os.path.normcase(os.path.abspath(self.root))
        self.assertEqual(len(self.app._find_index[key][1]), 4)
        with open(os.path.join(self.root, "新件3.txt"), "w", encoding="utf-8") as f:
            f.write("z")
        self.repo.commit("又记了一版")
        self.find(q="预热", scan=1)                      # 很小的一次，撞到 cap
        self.assertEqual(len(self.app._find_index[key][1]), 5,
                         "索引被误判成「分支被删」清掉了")
        d = self.find(q="最早", scan=20000)
        self.assertEqual(len(d["hits"]), 1, "最老那一版还在索引里")

    def test_scan_param_is_capped(self):
        """scan 是查询参数：给个天文数字不能真的去扫那么多（内存/时间都在本机）"""
        d = self.find(q="的", scan=99999999)
        self.assertTrue(d["ok"], d)
        self.assertLessEqual(d["scanned"], 50000)

    def test_finds_old_commit_beyond_first_screen(self):
        """最老那一版也得能找到——界面只加载了几个版本不影响这里"""
        d = self.find(q="最早")
        self.assertTrue(d["ok"])
        self.assertEqual([h["id"] for h in d["hits"]], [self.ids["最早的一版"]])
        self.assertEqual(d["scanned"], 4)

    def test_case_insensitive(self):
        with open(os.path.join(self.root, "BOM.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.repo.commit("导出 BOM 清单")
        d = self.find(q="bom")
        self.assertEqual(len(d["hits"]), 1)
        self.assertIn("BOM", d["hits"][0]["message"])

    def test_search_by_version_prefix(self):
        oid = self.ids["改料盘"]
        d = self.find(q=oid[:8])
        self.assertEqual([h["id"] for h in d["hits"]], [oid])

    def test_no_hit_reports_how_many_scanned(self):
        d = self.find(q="压根没有这句话")
        self.assertEqual(d["hits"], [])
        self.assertEqual(d["scanned"], 4)

    def test_limit_caps_hits_but_not_scan(self):
        d = self.find(q="的", limit=1)
        self.assertEqual(len(d["hits"]), 1)
        self.assertEqual(d["scanned"], 4)

    def test_empty_query_returns_nothing(self):
        d = self.find(q="")
        self.assertEqual(d["hits"], [])

    def test_finds_commits_on_other_branches(self):
        """分支上的提交也算历史：只在主线里翻会漏掉"""
        self.repo.set_branch("改料盘方案", self.ids["改料盘"])
        with open(os.path.join(self.root, "新方案.txt"), "w", encoding="utf-8") as f:
            f.write("y")
        self.repo.commit("料盘改成四工位", branch="改料盘方案")
        d = self.find(q="四工位")
        self.assertEqual(len(d["hits"]), 1)
        self.assertGreaterEqual(d["scanned"], 5)

    def test_second_search_does_not_reread_history(self):
        """边打字边搜：同一个历史不该每敲一个字就把对象库重读一遍"""
        from unittest import mock
        from zgr.store import Repo as R
        real = R.commit_info
        calls = []

        def counting(self, oid):
            calls.append(oid)
            return real(self, oid)

        with mock.patch.object(R, "commit_info", counting):
            self.find(q="客户")
            first = len(calls)
            self.find(q="客户确认")
            self.find(q="料盘")
        self.assertEqual(first, 4, "第一次搜索要把 4 个提交都读一遍，实际 %d" % first)
        self.assertEqual(len(calls), 4,
                         "后面两次搜索不该再读提交对象，实际又读了 %d 次"
                         % (len(calls) - 4))

    def test_index_rebuilds_after_new_commit(self):
        """新记的一版必须马上能搜到，不能因为「有缓存」就看不见"""
        self.find(q="预热")                     # 先把索引建起来
        with open(os.path.join(self.root, "新件.txt"), "w", encoding="utf-8") as f:
            f.write("z")
        self.repo.commit("客户又确认了一次")
        d = self.find(q="又确认")
        self.assertEqual(len(d["hits"]), 1, "新提交要立刻出现在结果里")
        self.assertEqual(d["scanned"], 5)

    def test_new_commit_costs_one_read(self):
        """记了新的一版之后，重搜只该读那个新提交，不该把整段历史重读一遍"""
        from unittest import mock
        from zgr.store import Repo as R
        self.find(q="预热")                       # 先把索引建起来
        with open(os.path.join(self.root, "新件2.txt"), "w", encoding="utf-8") as f:
            f.write("z")
        self.repo.commit("增量的一条")
        real = R.commit_info
        calls = []

        def counting(self, oid):
            calls.append(oid)
            return real(self, oid)

        with mock.patch.object(R, "commit_info", counting):
            d = self.find(q="增量")
        self.assertEqual(len(calls), 1, "只该读新提交一个，实际 %d 个" % len(calls))
        self.assertEqual(len(d["hits"]), 1)

    def test_small_scan_can_be_expanded(self):
        """先用很小的 scan 建了索引，之后要更大的也必须补得上。

        以前「行数已经等于 cap」就直接跳过扫描，于是索引被第一次的小 scan 锁死，
        后面怎么问都是那几条，界面上还写着「全部 N 个版本」。"""
        d = self.find(q="的", scan=2)
        self.assertEqual(d["scanned"], 2)
        self.assertTrue(d["scan_limited"])
        d2 = self.find(q="最早", scan=20000)
        self.assertEqual(d2["scanned"], 4, "更大的 scan 要能把索引续上")

    def test_new_commit_survives_full_cap(self):
        """索引已经攒满 cap 时，新记的一版也必须进得来（挤掉最老的）"""
        self.find(q="预热", scan=4)                  # 正好等于当前版本数
        with open(os.path.join(self.root, "刚记的.txt"), "w", encoding="utf-8") as f:
            f.write("z")
        self.repo.commit("刚记的一版")
        d = self.find(q="刚记的", scan=4)
        self.assertEqual(len(d["hits"]), 1, "攒满 cap 之后新版本也必须能搜到")

    def test_deleted_branch_drops_its_commits(self):
        """删掉分支之后，只在那个分支上的版本不该再被搜出来。

        留着它，用户点进结果只会得到「对象不存在」——而且跑过清理之后就真没了。"""
        self.repo.set_branch("侧线", self.ids["改料盘"])
        with open(os.path.join(self.root, "侧线上的.txt"), "w", encoding="utf-8") as f:
            f.write("y")
        self.repo.commit("侧线上的一版", branch="侧线")
        d = self.find(q="侧线上的一版")
        self.assertEqual(len(d["hits"]), 1)
        self.repo.delete_branch("侧线")
        d2 = self.find(q="侧线上的一版")
        self.assertEqual(d2["hits"], [], "分支删了，它的版本不该再出现在搜索结果里")
        self.assertNotIn(d["hits"][0]["id"],
                         [r for r in []])        # 只是让上面的失败信息更清楚

    def test_hit_carries_author_and_time(self):
        d = self.find(q="客户确认")
        h = d["hits"][0]
        self.assertEqual(h["message"], "客户确认的第三版")
        self.assertTrue(h["time"] > 0)
        self.assertIn("author", h)


if __name__ == "__main__":
    unittest.main()
