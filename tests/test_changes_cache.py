# -*- coding: utf-8 -*-
"""接口层：/api/log?changes=1 的「改了几个文件」要缓存，但绝不能算错

时间线一屏 400 个版本，每个都要和父版本比一次树——大项目上这四百次比较就是
半秒。提交对象是不可变的，同一个提交的改动数永远不变，所以可以缓存。

缓存最容易出的错是「串味」：拿上一个项目的数、或者拿同一个 oid 但父提交变了
的数。这里盯的就是这两件事。
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

from zgr.store import Repo                        # noqa: E402


class ChangesCacheTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-ccache-")
        self.root = os.path.join(self.tmp, "项目A")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("缓存")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.root)
        handler = type("H", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self, path=None):
        q = urllib.parse.urlencode({"path": path or self.root, "graph": 1,
                                    "limit": 50, "changes": 1})
        with urllib.request.urlopen(self.url + "/api/log?" + q, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def write(self, name, text):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_same_answer_twice(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        self.write("a.txt", "12")
        self.write("b.txt", "new")
        self.repo.commit("二版", "我")
        a = self.log()
        b = self.log()
        self.assertEqual(a["commits"], b["commits"], "第二次（走缓存）必须和第一次一模一样")
        totals = [c["changes"]["total"] for c in a["commits"]]
        self.assertEqual(totals[0], 2, "二版：改 1 个 + 新增 1 个")
        self.assertEqual(totals[1], 1, "一版：新增 1 个")

    def test_new_commit_gets_its_own_number(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        first = self.log()
        self.write("b.txt", "x")
        self.repo.commit("二版", "我")
        second = self.log()
        by_msg = {c["message"]: c["changes"]["total"] for c in second["commits"]}
        self.assertEqual(by_msg["二版"], 1)
        self.assertEqual(by_msg["一版"], 1)
        self.assertEqual(first["commits"][0]["changes"]["total"], 1)

    def test_two_projects_do_not_mix(self):
        # 第二个项目用同一个 oid？不可能——但路径不同的项目必须各算各的
        self.write("a.txt", "1")
        self.repo.commit("A 的一版", "我")
        other = os.path.join(self.tmp, "项目B")
        os.makedirs(other)
        repo_b = Repo(other).init("另一个")
        for i in range(3):
            with open(os.path.join(other, "x%d.txt" % i), "w", encoding="utf-8") as f:
                f.write("y")
        repo_b.commit("B 的一版", "张三")
        d = self.log(path=other)
        self.assertEqual(d["commits"][0]["changes"]["total"], 3)

    def test_cache_can_be_cleared_without_changing_answers(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        before = self.log()
        # 清掉缓存（服务重启就是这个效果），再要一次必须还是同样的数
        from zgr.server import App
        app = None
        for h in (self.httpd.RequestHandlerClass,):
            app = getattr(h, "app", None)
        self.assertIsNotNone(app)
        try:
            app.clear_changes_cache()
        except AttributeError:
            self.skipTest("还没有 clear_changes_cache")
        after = self.log()
        self.assertEqual(before["commits"], after["commits"])

    def test_merge_commit_counts_against_first_parent(self):
        self.write("a.txt", "1")
        a = self.repo.commit("主线一版", "我")
        self.repo.set_branch("side", a)
        self.write("a.txt", "12")
        b = self.repo.commit("旁支一版", "我")
        self.repo.set_branch("main", a)
        self.write("c.txt", "z")
        self.repo.commit("主线二版", "我")
        tree_b = self.repo.commit_info(b)["tree"]
        head = self.repo.head_oid("main")
        self.repo.commit_tree(tree_b, [head, b], "合并", "我")
        d = self.log()
        m = [c for c in d["commits"] if c["message"] == "合并"][0]
        # 合并版和第一个父（主线二版）比：a.txt 改了、c.txt 还在 → 1 个文件
        self.assertEqual(m["changes"]["total"], 1, m["changes"])


if __name__ == "__main__":
    unittest.main()
