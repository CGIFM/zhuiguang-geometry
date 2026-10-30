# -*- coding: utf-8 -*-
"""搜索也能按「记录人」找

一个人一条线的项目里，最常见的问法不是「哪一版改了什么」，而是
「小冯上周改的那几版在哪」。这说明里其实没有他的名字 —— 所以搜「张三」
以前是空的，用户会以为那几版不存在。现在说明 / 版本号 / 记录人一起找，
并且如实告诉你是哪一项对上的（不然结果里看不到关键词，会以为搜错了）。
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


class FindAuthorTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-findauthor-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("按人找")
        self.ids = {}

        def add(msg, author):
            with open(os.path.join(self.root, "%s.txt" % len(self.ids)), "w",
                      encoding="utf-8") as f:
                f.write(msg)
            self.ids[msg] = self.repo.commit(msg, author)

        add("调尺寸", "我")
        add("客户确认", "张三")
        add("料盘改成四工位", "张三")
        add("导出 BOM", "jlc")
        add("张三说要加一个定位销", "我")      # 说明里出现人名，但记录人是「我」

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        if hasattr(reg, "add"):
            reg.add(self.root, "按人找")
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

    def find(self, q, **params):
        params.update({"path": self.root, "q": q})
        with urllib.request.urlopen(
                self.base + "/api/find?" + urllib.parse.urlencode(params),
                timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_finds_versions_by_author(self):
        d = self.find("张三")
        got = {h["id"] for h in d["hits"]}
        self.assertIn(self.ids["客户确认"], got)
        self.assertIn(self.ids["料盘改成四工位"], got)

    def test_author_match_is_case_insensitive(self):
        d = self.find("JLC")
        self.assertEqual([h["id"] for h in d["hits"]], [self.ids["导出 BOM"]])

    def test_how_says_why_it_matched(self):
        d = self.find("客户确认")
        how = d["hits"][0]["how"]
        self.assertEqual(how, "说明")
        d = self.find("张三")          # 一版靠说明，一版靠记录人
        by_id = {h["id"]: h["how"] for h in d["hits"]}
        self.assertIn("记录人", by_id[self.ids["客户确认"]])
        self.assertIn("说明", by_id[self.ids["张三说要加一个定位销"]])

    def test_both_reasons_are_reported(self):
        d = self.find("张三说要加一个定位销")
        self.assertIn("说明", d["hits"][0]["how"])

    def test_version_prefix_still_works(self):
        oid = self.ids["调尺寸"]
        d = self.find(oid[:8])
        self.assertEqual([h["id"] for h in d["hits"]], [oid])
        self.assertEqual(d["hits"][0]["how"], "版本号")


if __name__ == "__main__":
    unittest.main()
