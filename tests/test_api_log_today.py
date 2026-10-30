# -*- coding: utf-8 -*-
"""接口层：/api/log 的 has_more 和 today

界面上的两句话全靠这两个字段：
- 「更早的还没加载」——以前是前端拿 `条数 >= 上限` 猜的，正好一屏的项目永远在说还有；
- 「今天 N 个版本」——今天记了几百版时，一屏数不完。

两个都必须是**真的**，猜错一次用户就会觉得版本丢了。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zgr.store as store                        # noqa: E402
from zgr.store import Repo                       # noqa: E402

DAY = 86400 * 1000


def day_start():
    t = time.localtime()
    return int(time.mktime(tuple(t[:3]) + (0, 0, 0, 0, 0, -1)) * 1000)


class ApiLogTodayTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tempfile.mkdtemp(prefix="zgr-apilog-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("看数")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.root)
        from zgr.server import Handler
        handler = type("H", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d" % self.port
        self._real = store.now_ms

    def tearDown(self):
        store.now_ms = self._real
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg, back_days=0):
        n = len(os.listdir(self.root))
        with open(os.path.join(self.root, "f%d.txt" % n), "w", encoding="utf-8") as f:
            f.write("x")
        store.now_ms = (lambda b=back_days, r=self._real: (lambda: r() - b * DAY))()
        try:
            return self.repo.commit(msg, "我")
        finally:
            store.now_ms = self._real

    def log(self, **params):
        q = urllib.parse.urlencode(dict({"path": self.root, "graph": 1}, **params))
        with urllib.request.urlopen(self.url + "/api/log?" + q, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    # ------------------------------------------------------------------ has_more
    def test_exactly_limit_says_no_more(self):
        for i in range(4):
            self.commit("第 %d 版" % (i + 1))
        d = self.log(limit=4)
        self.assertEqual(len(d["commits"]), 4)
        self.assertFalse(d["has_more"], "正好一屏时不该说还有更早的")

    def test_more_than_limit_says_more(self):
        for i in range(5):
            self.commit("第 %d 版" % (i + 1))
        d = self.log(limit=4)
        self.assertEqual(len(d["commits"]), 4)
        self.assertTrue(d["has_more"])

    # --------------------------------------------------------------------- today
    def test_today_counts_only_today(self):
        for i in range(3):
            self.commit("昨天 %d" % i, back_days=1)
        for i in range(2):
            self.commit("今天 %d" % i)
        d = self.log(limit=400)
        self.assertEqual(d["today"]["count"], 2)
        self.assertFalse(d["today"]["truncated"])
        t = time.localtime()
        self.assertEqual(d["today"]["since"],
                         int(time.mktime(tuple(t[:3]) + (0, 0, 0, 0, 0, -1)) * 1000),
                         "零点必须精确等于本地 0 点，晚一小时就会漏掉整点后的版本")

    def test_today_from_is_honoured(self):
        # 局域网里另一台机器报它自己的零点：服务端要按它数，
        # 否则 00:00–08:00 记的版本会被算到「昨天」
        self.commit("今天的")
        since = day_start() - 2 * 3600 * 1000
        d = self.log(limit=10, today_from=since)
        self.assertEqual(d["today"]["since"], since)

    def test_absurd_today_from_is_ignored(self):
        # 查询串是外部输入：几十年前的「今天零点」不跟
        self.commit("今天的")
        d = self.log(limit=10, today_from=1)
        t = time.localtime()
        self.assertEqual(d["today"]["since"],
                         int(time.mktime(tuple(t[:3]) + (0, 0, 0, 0, 0, -1)) * 1000))

    def test_bad_today_from_does_not_500(self):
        self.commit("今天的")
        d = self.log(limit=10, today_from="abc")
        self.assertEqual(d["today"]["count"], 1)

    def test_today_is_not_limited_by_window(self):
        # 今天 8 版，但只取 3 版：今天的数照样是 8
        for i in range(8):
            self.commit("今天 %d" % i)
        d = self.log(limit=3)
        self.assertEqual(len(d["commits"]), 3)
        self.assertEqual(d["today"]["count"], 8)

    def test_today_zero_when_nothing_today(self):
        for i in range(3):
            self.commit("上个月 %d" % i, back_days=30)
        d = self.log(limit=10)
        self.assertEqual(d["today"]["count"], 0)

    def test_no_graph_means_no_today_field(self):
        self.commit("一版")
        d = self.log(limit=10)
        self.assertIn("today", d)
        q = urllib.parse.urlencode({"path": self.root, "limit": 10})
        with urllib.request.urlopen(self.url + "/api/log?" + q, timeout=30) as r:
            d2 = json.loads(r.read().decode("utf-8"))
        self.assertIsNone(d2["today"])
        self.assertFalse(d2["has_more"])


if __name__ == "__main__":
    unittest.main()
