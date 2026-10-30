# -*- coding: utf-8 -*-
"""「先核对、再合并」的中间被人插了一版

界面上合并是两步：先点「与这一版合并」看清单，确认后才真合。这两步之间本机可能
又记了一版（后台自动记录、对方推过来、隔壁窗口点的记录）。核对时看到的清单就不是
真正要合的东西了 —— 所以确认时把核对那一刻的本机版本带回服务端，对不上就 409 让
人重新核对。这个测试盯着那道闸。
"""
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


class MergeGuardTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-mguard-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("合并闸")
        self.write("a.txt", "一")
        self.c1 = self.repo.commit("第一版")
        # 分出一条线：在上面加 b.txt，形成真正的分叉
        self.repo.set_branch("方案B", self.c1)
        self.write("b.txt", "b")
        self.c2 = self.repo.commit("方案B 加了一个件", branch="方案B")
        # 主线上再改一次 a.txt
        self.write("a.txt", "二")
        self.c3 = self.repo.commit("主线改了 a")
        self.assertEqual(self.repo.head_oid(), self.c3)

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        if hasattr(reg, "add"):
            reg.add(self.root, "合并闸")
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

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_merge_from_rejects_when_head_moved(self):
        code, d = self.post("/api/merge/from", {
            "path": self.root, "commit": self.c2, "apply": True,
            "expect": "0" * 64})
        self.assertEqual(code, 409, d)
        self.assertIn("重新点", d["error"])
        self.assertEqual(self.repo.head_oid(), self.c3, "被拒时不该动任何东西")

    def test_merge_from_accepts_matching_expect(self):
        code, d = self.post("/api/merge/from", {
            "path": self.root, "commit": self.c2, "apply": True,
            "expect": self.c3})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["commit"])
        self.assertNotEqual(self.repo.head_oid(), self.c3)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")))

    def test_branch_merge_rejects_when_head_moved(self):
        code, d = self.post("/api/branch/merge", {
            "path": self.root, "branch": "方案B", "apply": True,
            "expect": "0" * 64})
        self.assertEqual(code, 409, d)
        self.assertIn("重新点", d["error"])

    def test_branch_merge_rejects_when_branch_advanced(self):
        """被并入的那条分支在你核对之后又前进了一版，同样要拦

        分支名不会变，指针会变（对方随时可能推上来）。只核对本机这一边，
        等于确认时合的是另一组内容。"""
        with open(os.path.join(self.root, "c.txt"), "w", encoding="utf-8") as f:
            f.write("c")
        self.repo.commit("方案B 又加了一个件", branch="方案B")
        code, d = self.post("/api/branch/merge", {
            "path": self.root, "branch": "方案B", "apply": True,
            "expect": self.c3, "expect_theirs": self.c2})
        self.assertEqual(code, 409, d)
        self.assertIn("又前进", d["error"])

    def test_branch_merge_accepts_matching_expect(self):
        code, d = self.post("/api/branch/merge", {
            "path": self.root, "branch": "方案B", "apply": True,
            "expect": self.c3})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["ok"], d)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")))

    def test_preview_does_not_need_expect(self):
        """只看清单（apply=false）不该被闸拦住——那时还没有「确认过的东西」"""
        code, d = self.post("/api/merge/from", {
            "path": self.root, "commit": self.c2, "apply": False})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["ok"], d)


if __name__ == "__main__":
    unittest.main()
