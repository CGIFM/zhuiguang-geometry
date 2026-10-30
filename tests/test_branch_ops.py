# -*- coding: utf-8 -*-
"""分支的「汇总」与「删除」：/api/branch/merge、/api/branch/delete

口径跟 git 对齐：
  · 合并 = 生成一个双父提交，两条分支的历史一条都不丢（合并提交的第二个父是对方分支头）
  · 删除 = 只删 refs/heads/<名字>，一个对象都不删；当前分支不许删；
           还有没合并的版本时必须显式 force，否则拒绝（等价于 git branch -d / -D）
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo


def _BaseHandler():
    from zgr.server import Handler
    return Handler


class BranchOpsTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tempfile.mkdtemp(prefix="zgr-bops-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("分支合并测试")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.root)
        handler = type("H", (_BaseHandler(),), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def post(self, route, body):
        req = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_err(self, route, body):
        """返回 (状态码, 错误文字)"""
        try:
            self.post(route, body)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw).get("error") or raw
            except ValueError:
                return e.code, raw
        self.fail("预期失败，实际成功了：%s %s" % (route, body))

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def graph(self):
        q = urllib.parse.urlencode({"path": self.root, "graph": 1, "limit": 50})
        with urllib.request.urlopen(self.url + "/api/log?" + q, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def fork(self):
        """造一个真的分叉：main 和 side 各自往前走一步，返回 (c_main, c_side)"""
        self.write("a.txt", "1")
        c1 = self.repo.commit("起点")
        self.post("/api/branch", {"path": self.root, "branch": "side", "commit": c1})
        self.write("b.txt", "分支里新增的零件")
        c_side = self.repo.commit("分支上改一版")
        # 切回主线要连工作区一起换（force）：分支上新增的 b.txt 会被移走，
        # 否则下一次记录会把「分支的文件」带进主干这一版
        self.post("/api/branch", {"path": self.root, "branch": "main", "force": True})
        if os.path.exists(os.path.join(self.root, "b.txt")):
            os.remove(os.path.join(self.root, "b.txt"))
        self.write("a.txt", "2 主干往前走")
        c_main = self.repo.commit("主干上改一版")
        return c_main, c_side

    # ------------------------------------------------------------------ 合并
    def test_merge_creates_two_parent_commit(self):
        c_main, c_side = self.fork()
        p = self.post("/api/branch/merge", {"path": self.root, "branch": "side"})
        self.assertFalse(p["apply"] if "apply" in p else False)
        self.assertEqual(p["versions"], 1, "只该并进 1 个版本")
        self.assertEqual(p["conflicts"], [])

        r = self.post("/api/branch/merge",
                      {"path": self.root, "branch": "side", "apply": True, "strategy": "smart"})
        m = r["commit"]
        info = self.repo.commit_info(m)
        self.assertEqual(set(info["parents"]), {c_main, c_side},
                         "合并提交必须同时指向两边（双父），否则分支历史就断了")
        self.assertEqual(self.repo.head_oid("main"), m)
        self.assertEqual(self.repo.branch, "main")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")),
                        "分支上的新文件要落到主干工作区")
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertIn("2 主干", f.read(), "主干自己的改动不能被覆盖")

    def test_merge_marks_branch_as_merged(self):
        self.fork()
        self.post("/api/branch/merge",
                  {"path": self.root, "branch": "side", "apply": True})
        d = self.graph()
        st = d["branch_status"]
        self.assertEqual(st["side"]["merged_into"], "main")
        self.assertTrue(st["side"]["merged"])
        self.assertFalse(st["main"]["merged"], "主干自己没被合并过")

    def test_merge_twice_is_rejected(self):
        self.fork()
        self.post("/api/branch/merge", {"path": self.root, "branch": "side", "apply": True})
        code, msg = self.post_err("/api/branch/merge",
                                  {"path": self.root, "branch": "side", "apply": True})
        self.assertEqual(code, 409)
        self.assertIn("已经在当前分支里", msg)

    def test_merge_current_branch_rejected(self):
        self.fork()
        code, _ = self.post_err("/api/branch/merge", {"path": self.root, "branch": "main"})
        self.assertEqual(code, 400)

    # ------------------------------------------------------------------ 删除
    def test_cannot_delete_current_branch(self):
        self.fork()
        code, msg = self.post_err("/api/branch/delete",
                                  {"path": self.root, "branch": "main"})
        self.assertEqual(code, 400)
        self.assertIn("当前所在的分支", msg)
        self.assertIn("main", self.repo.branches())

    def test_delete_merged_branch_keeps_objects(self):
        _, c_side = self.fork()
        self.post("/api/branch/merge", {"path": self.root, "branch": "side", "apply": True})
        r = self.post("/api/branch/delete", {"path": self.root, "branch": "side"})
        self.assertEqual(r["merged_into"], "main")
        self.assertNotIn("side", self.repo.branches())
        self.assertTrue(self.repo.has(c_side), "删分支不能删提交对象")
        self.assertIn(c_side, self.repo.commit_reachable(self.repo.head_oid()),
                      "分支的提交仍然在主干历史里")

    def test_delete_unmerged_requires_force(self):
        _, c_side = self.fork()
        code, msg = self.post_err("/api/branch/delete",
                                  {"path": self.root, "branch": "side"})
        self.assertEqual(code, 409)
        self.assertIn("没合并", msg)
        self.assertIn("side", self.repo.branches(), "拒绝了就不能真删")

        r = self.post("/api/branch/delete",
                      {"path": self.root, "branch": "side", "force": True})
        self.assertEqual(r["merged_into"], None)
        self.assertNotIn("side", self.repo.branches())
        self.assertTrue(self.repo.has(c_side), "强删也只是删指针，对象还在")

    def test_delete_missing_branch(self):
        self.fork()
        code, _ = self.post_err("/api/branch/delete",
                                {"path": self.root, "branch": "不存在的分支"})
        self.assertEqual(code, 404)


class CommitReachableTest(unittest.TestCase):
    """commit_reachable 必须沿全部父链走，否则「合并过的分支」会被误判成没合并"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-reach-")
        self.repo = Repo(self.tmp).init("可达性")
        self.p = os.path.join(self.tmp, "a.txt")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def w(self, text):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_second_parent_counts(self):
        self.w("1")
        c1 = self.repo.commit("v1")
        self.repo.set_branch("side", c1)
        self.repo._write_text("HEAD", "side")
        self.w("1 side")
        c2 = self.repo.commit("side 一版")
        self.repo._write_text("HEAD", "main")
        self.w("1 main")
        c3 = self.repo.commit("main 一版")
        # 手工造一个双父合并提交：c3 的树，父是 [c3, c2]
        tree = self.repo.commit_info(c3)["tree"]
        payload = json.dumps({"tree": tree, "parents": [c3, c2],
                              "time": 1, "message": "merge", "author": "我"},
                             ensure_ascii=False).encode("utf-8")
        m = self.repo.put("commit", payload)
        self.assertIn(c2, self.repo.commit_reachable(m),
                      "沿第一父走不到 c2，必须走第二条父链才算合并")
        self.assertIn(c1, self.repo.commit_reachable(m))
        self.assertNotIn(c2, self.repo.commit_reachable(c3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
