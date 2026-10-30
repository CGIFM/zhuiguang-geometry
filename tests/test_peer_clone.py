# -*- coding: utf-8 -*-
"""对方机器上新建的项目，本机一键拉过来继续做

模拟两台机器（同一进程里起两个服务，各自独立的注册表）：
  A 上有一个项目「活塞支架」，B 还没见过它
  → B 问 A「你有哪些项目」：能看到 活塞支架 的名字/分支/大小，但拿不到 A 的本地路径
  → B 选一个空文件夹把它拉过来：分支指针、历史、工作区文件全都要对得上
  → B 拉完自动登记成项目（左侧栏马上能看到），并接着和 A 实时同步
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from zgr import peerjoin
from zgr.store import Repo


def _BaseHandler():
    from zgr.server import Handler
    return Handler


class _Server:
    """一台「机器」：自己的注册表 + 自己的项目目录"""

    def __init__(self, tmp, name):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tmp
        self.reg_path = os.path.join(tmp, "projects.json")
        reg = Registry(path=self.reg_path)
        self.app = App(reg)
        handler = type("H", (_BaseHandler(),), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def get(self, route, **q):
        url = self.url + route
        if q:
            url += "?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, body):
        req = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_err(self, route, body):
        try:
            self.post(route, body)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw).get("error") or raw
            except ValueError:
                return e.code, raw
        self.fail("预期失败，实际成功了：%s %s" % (route, body))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()



class PeerCloneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-peer-")
        self.a = _Server(os.path.join(self.tmp, "A"), "A")
        self.b = _Server(os.path.join(self.tmp, "B"), "B")

        # A 上造一个有两版历史的项目
        self.src = os.path.join(self.tmp, "A", "活塞支架")
        os.makedirs(self.src)
        repo = Repo(self.src).init("活塞支架")
        self.write(self.src, "总装.sldasm", "版本一")
        self.write(self.src, "零件/活塞.sldprt", "活塞 v1")
        repo.commit("起点")
        self.write(self.src, "零件/活塞.sldprt", "活塞 v2（加倒角）")
        self.c2 = repo.commit("活塞加倒角")
        self.a.app.registry.add(self.src, "活塞支架")

    def tearDown(self):
        for s in (getattr(self, "a", None), getattr(self, "b", None)):
            if s:
                try:
                    s.close()
                except Exception:                             # noqa: BLE001
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write(root, name, text):
        p = os.path.join(root, *name.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    # ------------------------------------------------------------------ 对方项目清单
    def test_peer_list_has_metadata_but_no_paths(self):
        d = self.a.get("/api/peer/list")
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["projects"]), 1)
        p = d["projects"][0]
        self.assertEqual(p["name"], "活塞支架")
        self.assertEqual(p["head"], self.c2)
        self.assertEqual(p["files"], 2)
        self.assertGreater(p["bytes"], 0)
        self.assertTrue(p["message"])
        self.assertNotIn("path", p, "绝对不能让对方知道本机路径")
        self.assertNotIn("root", p)
        # 整个响应里不该出现测试目录的名字
        self.assertNotIn(self.src, json.dumps(d, ensure_ascii=False))

    def test_b_sees_a_projects(self):
        d = self.b.get("/api/peer/projects", url=self.a.url)
        self.assertTrue(d["ok"])
        items = d["projects"]
        self.assertEqual([x["name"] for x in items], ["活塞支架"])
        self.assertFalse(items[0]["known"], "B 本机还没有这个项目")
        self.assertEqual(items[0]["url"], self.a.url)
        self.assertEqual(items[0]["files"], 2)

    # ------------------------------------------------------------------ 目标目录体检
    def test_check_dest_rules(self):
        empty = os.path.join(self.tmp, "空目录")
        os.makedirs(empty)
        self.assertEqual(peerjoin.check_dest(empty), os.path.abspath(empty))

        with self.assertRaises(peerjoin.JoinError):
            peerjoin.check_dest("")

        busy = os.path.join(self.tmp, "有东西")
        os.makedirs(busy)
        open(os.path.join(busy, "x.txt"), "w").close()
        with self.assertRaises(peerjoin.JoinError) as e:
            peerjoin.check_dest(busy)
        self.assertIn("不是空", str(e.exception))

        with self.assertRaises(peerjoin.JoinError) as e:
            peerjoin.check_dest(self.src)
        self.assertIn("追光几何项目", str(e.exception))

        with self.assertRaises(peerjoin.JoinError) as e:
            peerjoin.check_dest(os.path.join(self.src, "里面"))
        self.assertIn("已有项目", str(e.exception))

    def test_clone_rejects_busy_dest_in_http(self):
        busy = os.path.join(self.tmp, "B", "有东西")
        os.makedirs(busy)
        open(os.path.join(busy, "x.txt"), "w").close()
        code, msg = self.b.post_err("/api/peer/clone", {
            "url": self.a.url, "project": "活塞支架", "dest": busy})
        self.assertEqual(code, 400)
        self.assertIn("不是空", msg)

    # ------------------------------------------------------------------ 完整克隆
    def _clone(self, project="活塞支架", dest=None):
        dest = dest or os.path.join(self.tmp, "B", "活塞支架-本地")
        d = self.b.post("/api/peer/clone", {"url": self.a.url, "project": project,
                                            "dest": dest})
        self.assertTrue(d["ok"])
        jid = d["job"]["id"]
        deadline = time.time() + 40
        while time.time() < deadline:
            st = self.b.get("/api/peer/clone/status", id=jid)["job"]
            if st["state"] != "running":
                return dest, st
            time.sleep(0.1)
        self.fail("克隆一直没结束")

    def test_full_clone(self):
        dest, st = self._clone()
        self.assertEqual(st["state"], "done", st.get("error"))
        res = st["result"]
        self.assertEqual(res["head"], self.c2)
        self.assertEqual(res["branch"], "main")
        self.assertEqual(res["peer"], self.a.url)
        self.assertEqual(res["objects"] >= 5, True)      # 2 提交 + 若干树 + 2 文件

        # 历史对得上
        r = Repo(dest)
        self.assertTrue(r.exists)
        self.assertEqual(r.head_oid("main"), self.c2)
        self.assertEqual(r.commit_info(self.c2)["message"], "活塞加倒角")

        # 工作区铺开了，而且是最新内容
        with open(os.path.join(dest, "总装.sldasm"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "版本一")
        with open(os.path.join(dest, "零件", "活塞.sldprt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "活塞 v2（加倒角）")

        # 没有残留「待更新」标记
        self.assertFalse(os.path.exists(os.path.join(dest, ".zgr", "PULL_NEEDED")))

        # B 的左侧栏里出现了它，并且标成已有
        names = [p["name"] for p in self.b.get("/api/projects")["projects"]]
        self.assertIn(res["name"], names)
        items = self.b.get("/api/peer/projects", url=self.a.url)["projects"]
        self.assertTrue(items[0]["known"])

    def test_clone_unknown_project_reports_error(self):
        dest = os.path.join(self.tmp, "B", "x")
        d = self.b.post("/api/peer/clone", {"url": self.a.url, "project": "不存在的项目",
                                            "dest": dest})
        jid = d["job"]["id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            st = self.b.get("/api/peer/clone/status", id=jid)["job"]
            if st["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(st["state"], "error")
        self.assertTrue(st["error"])

    def test_status_unknown_id_is_404(self):
        code, msg = self.b.post_err("/api/peer/clone", {"url": self.a.url,
                                                        "project": "", "dest": ""})
        self.assertEqual(code, 400)
        try:
            self.b.get("/api/peer/clone/status", id="nope")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        else:
            self.fail("未知任务应该是 404")


if __name__ == "__main__":
    unittest.main(verbosity=2)
