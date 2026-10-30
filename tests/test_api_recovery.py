# -*- coding: utf-8 -*-
"""体检 / 恢复收尾的接口打通（界面点的那两个按钮）"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class ApiRecoveryTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-apirec-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("接口")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.c1 = self.repo.commit("一")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        reg.add(self.root, "接口") if hasattr(reg, "add") else None
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

    def get(self, route, **params):
        q = urllib.parse.urlencode(params) if params else ""
        with urllib.request.urlopen(self.base + route + ("?" + q if q else ""), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))

    def test_fsck_endpoint(self):
        d = self.get("/api/fsck", path=self.root)
        self.assertTrue(d["ok"])
        self.assertTrue(d["report"]["ok"])
        self.assertGreater(d["report"]["objects"], 0)
        d = self.get("/api/fsck", path=self.root, deep="1")
        self.assertTrue(d["report"]["ok"])
        self.assertTrue(d["report"]["deep"])

    def test_recovery_endpoint_finishes_interrupted_restore(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("半截")
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            json.dump({"commit": self.c1, "target": self.root, "backup": None}, f)
        d = self.get("/api/recovery", path=self.root)
        self.assertTrue(d["pending"]["commit"])
        r = self.post("/api/recovery", {"path": self.root, "mode": "again"})
        self.assertTrue(r["ok"], r)
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "一")
        self.assertIsNone(self.get("/api/recovery", path=self.root)["pending"])

    def test_restore_invalidates_status_cache(self):
        """恢复完立刻问状态，拿到的必须是「恢复后」的，不是缓存里恢复前的。

        界面是这样用的：点「恢复整个项目到此版本」→ 立刻刷新时间线和「未保存 N 个文件」。
        缓存 1.2 秒 + 文件通知的宽限，如果不主动作废，这一两秒里显示的还是旧状态，
        用户会以为没恢复成功。"""
        with open(os.path.join(self.root, "b.txt"), "w", encoding="utf-8") as f:
            f.write("二")
        c2 = self.repo.commit("第二版")
        # 先问一次，把状态灌进缓存
        d0 = self.get("/api/status", path=self.root)
        self.assertEqual(d0["head"], c2)
        r = self.post("/api/restore", {"path": self.root, "commit": self.c1,
                                       "backup": True})
        self.assertTrue(r["ok"], r)
        d1 = self.get("/api/status", path=self.root)
        # 恢复到旧版本不动分支指针，但工作区已经不是 head 那一版了：
        # 缓存没作废的话，这一秒里它还会说「干净」，看起来像恢复没生效
        self.assertFalse(d1["clean"],
                         "恢复后马上问，状态不能还是缓存里那句「干净」")
        gone = [e.get("path") for e in (d1.get("entries") or [])
                if e.get("path") == "b.txt"]
        self.assertTrue(gone, "状态里要能看见 b.txt 已经不在了")
        self.assertFalse(os.path.exists(os.path.join(self.root, "b.txt")),
                         "工作区应当已经回到第一版")

    def test_recovery_dismiss(self):
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            json.dump({"commit": self.c1, "target": self.root, "backup": None}, f)
        r = self.post("/api/recovery", {"path": self.root, "dismiss": True})
        self.assertTrue(r["dismissed"])
        self.assertIsNone(self.get("/api/recovery", path=self.root)["pending"])


import urllib.parse  # noqa: E402

if __name__ == "__main__":
    unittest.main(verbosity=2)
