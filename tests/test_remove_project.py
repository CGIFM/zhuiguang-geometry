# -*- coding: utf-8 -*-
"""「从列表里移除项目」要把挂在它身上的东西全停掉

移除只是让它不出现在列表里，但后台还有两条线程盯着这个目录：自动记录、实时同步。
以前只停了自动记录和文件通知——同步线程会继续每几秒拉一次、往一个「已经不在
列表里」的项目写配置和对象；按目录做的缓存（状态、搜索索引、根提交）也一直留着，
同路径再建项目时可能先读到旧项目的索引。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class RemoveProjectTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-remove-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("移除")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.repo.commit("第一版", "我")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        if hasattr(reg, "add"):
            reg.add(self.root, "移除")
        self.app = App(reg)
        self.key = os.path.normcase(os.path.abspath(self.root))
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        for w in list(self.app.watchers.values()):
            w.stop()
        for w in list(self.app.lives.values()):
            w.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(self, route, **params):
        import urllib.parse
        q = urllib.parse.urlencode(params) if params else ""
        with urllib.request.urlopen(self.base + route + ("?" + q if q else ""), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_remove_stops_live_and_drops_caches(self):
        # 先把「后台还挂着东西」的状态造出来：实时同步线程 + 搜索索引 + 状态缓存
        self.app.start_live(self.repo, "http://127.0.0.1:9/", interval=600)
        self.assertTrue(self.key in self.app.lives, "实时同步线程没起来，这个用例无效")
        self.get("/api/status", path=self.root)
        self.get("/api/find", path=self.root, q="第一")
        self.assertIn(self.key, self.app._status_cache)
        self.assertIn(self.key, self.app._find_index)

        d = self.post("/api/remove", {"path": self.root})
        self.assertTrue(d["ok"], d)
        self.assertNotIn(self.key, self.app.lives, "移除后同步线程还在跑")
        self.assertNotIn(self.key, self.app.watchers)
        self.assertNotIn(self.key, self.app._status_cache)
        self.assertNotIn(self.key, self.app._find_index)
        # 项目登记也没了
        self.assertFalse(any(
            os.path.normcase(os.path.abspath(p["path"])) == self.key
            for p in self.app.registry.projects))

    def test_remove_does_not_touch_project_files(self):
        """移除只是「不再管它」：目录和 .zgr 一个字节都不能动"""
        marker = os.path.join(self.root, "a.txt")
        before = os.path.getmtime(marker)
        time.sleep(0.01)
        self.post("/api/remove", {"path": self.root})
        self.assertTrue(os.path.isfile(marker))
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".zgr")))
        self.assertEqual(os.path.getmtime(marker), before)


if __name__ == "__main__":
    unittest.main()
