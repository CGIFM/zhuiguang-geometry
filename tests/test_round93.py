# -*- coding: utf-8 -*-
"""第 93 轮：空项目列表里的「先看看示例」

两台上线之后项目列表一直是空的——不是功能不够，是第一次打开只见一片空白。
示例项目把「记录一版 → 改点东西 → 有第二版 → 能对比能恢复」先演一遍，
用户拿它试手，试完在列表里移除即可（文件不删）。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import demo as demo_mod          # noqa: E402
from zgr.store import Repo                # noqa: E402


class DemoProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-demo-")
        self._orig_root = demo_mod.demo_root
        demo_mod.demo_root = lambda: os.path.join(self.tmp, demo_mod.DEMO_NAME)
        self.repo = demo_mod.make_demo()

    def tearDown(self):
        demo_mod.demo_root = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_versions(self):
        log = self.repo.log()
        self.assertEqual([c["message"] for c in log],
                         ["侧板加厚到 10 mm", "示例项目：初始版本"])

    def test_worktree_clean_after_build(self):
        st = self.repo.status()
        self.assertTrue(st["clean"], st["counts"])

    def test_has_files_and_no_meta(self):
        _info, files = self.repo.files_of(self.repo.head_oid())
        self.assertGreaterEqual(len(files), 5)
        self.assertFalse([p for p in files if p.startswith(".zgr")])
        self.assertIn("图纸/底板轮廓.dxf", files)

    def test_second_call_does_not_add_versions(self):
        before = len(self.repo.log())
        again = demo_mod.make_demo()
        self.assertEqual(again.root, self.repo.root)
        self.assertEqual(len(again.log()), before)

    def test_occupied_dir_does_not_get_overwritten(self):
        """同名目录里已经有别的东西时，绝不覆盖，换一个名字"""
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, demo_mod.DEMO_NAME), exist_ok=True)
        with open(os.path.join(self.tmp, demo_mod.DEMO_NAME, "我的图纸.sldprt"),
                  "w", encoding="utf-8") as f:
            f.write("别人的东西")
        r = demo_mod.make_demo()
        self.assertTrue(r.root.endswith("-2"), r.root)
        with open(os.path.join(self.tmp, demo_mod.DEMO_NAME, "我的图纸.sldprt"),
                  "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "别人的东西")

    def test_restore_shows_what_a_version_means(self):
        """示例里那一版真的能恢复：侧板厚度从 10 回到 8"""
        old = self.repo.log()[-1]["id"]
        p = os.path.join(self.repo.root, "零件", "侧板.sldprt")
        self.repo.restore(old, backup=False)
        with open(p, "r", encoding="utf-8") as f:
            self.assertIn("厚度: 8 mm", f.read())


class DemoApiTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-demoapi-")
        self._orig_root = demo_mod.demo_root
        demo_mod.demo_root = lambda: os.path.join(self.tmp, demo_mod.DEMO_NAME)
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        for w in list(self.app.watchers.values()):
            w.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        demo_mod.demo_root = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_demo_endpoint(self):
        r = self.post("/api/demo", {})
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isdir(os.path.join(r["project"]["path"], ".zgr")))
        # 建完就进了项目列表，界面刷新后能直接选中
        self.assertEqual([p["path"] for p in self.app.registry.projects],
                         [r["project"]["path"]])
        # 再点一次不会建第二个、也不会多出版本
        r2 = self.post("/api/demo", {})
        self.assertEqual(r2["project"]["path"], r["project"]["path"])
        self.assertEqual(len(self.app.registry.projects), 1)


if __name__ == "__main__":
    unittest.main()
