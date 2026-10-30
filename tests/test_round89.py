# -*- coding: utf-8 -*-
"""第 89 轮：配置读-改-写必须进跨进程锁

「读整份 config.json → 改一项 → 整份写回」这种做法，在**两个进程**同时改
同一个项目的不同设置时，后写的那个会把前一个刚写的整份盖掉：

    zgr remote add 备份盘    （网页服务同时点了「忽略规则」保存）
    → 忽略规则写了，远端配置没了；或者反过来。

丢的都不是会当场报错的项：忽略规则没了 → 下次把 .zgr 里的东西当零件记录；
同步地址没了 → 第二天开机不再同步，而界面只显示「未配置」。
所以统一走 Repo.update_config()，整段在**跨进程**锁里完成。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


def _cfg(repo):
    with open(os.path.join(repo.meta, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


class UpdateConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-cfg-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("配置")
        self.path = os.path.join(self.repo.meta, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_saved_copy(self):
        got = self.repo.update_config(lambda c: c.update({"author": "老张"}))
        self.assertEqual(got["author"], "老张")
        self.assertEqual(_cfg(self.repo)["author"], "老张")

    def test_keeps_other_keys(self):
        self.repo.update_config(lambda c: c.update({"author": "老张"}))
        self.repo.update_config(lambda c: c.update({"preview_peer": "http://10.0.0.5:8795"}))
        cfg = _cfg(self.repo)
        self.assertEqual(cfg["author"], "老张")
        self.assertEqual(cfg["preview_peer"], "http://10.0.0.5:8795")
        # 项目名这类建库时写下的东西也不能丢
        self.assertEqual(cfg.get("name"), "配置")

    def test_fn_may_return_a_whole_new_dict(self):
        kept = {"author": "老张", "ignore": ["*.bak"]}
        self.repo.update_config(lambda c: kept)
        self.assertEqual(_cfg(self.repo), kept)

    def test_no_change_does_not_rewrite(self):
        """没改动就不碰文件：不然界面上每次轮询都让配置的修改时间往前走"""
        self.repo.update_config(lambda c: c.update({"author": "老张"}))
        before = os.stat(self.path).st_mtime_ns
        time.sleep(0.02)
        self.repo.update_config(lambda c: c.update({"author": "老张"}))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)

    def test_broken_config_does_not_crash(self):
        """手改坏了（存成数组）也要能继续用，而不是一保存设置就 500"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        got = self.repo.update_config(lambda c: c.update({"author": "老张"}))
        self.assertEqual(got["author"], "老张")
        self.assertEqual(_cfg(self.repo)["author"], "老张")


WORKER = r"""
import sys
sys.path.insert(0, %(root)r)
from zgr.store import Repo
repo = Repo(%(project)r)
key = %(key)r
n = %(n)d
for i in range(n):
    def fn(c):
        c[key] = i + 1
        # 计数器：每次读到的必须是「别人刚写进去的」那个值，
        # 锁没起作用时这里就会丢数
        c["count"] = int(c.get("count") or 0) + 1
    repo.update_config(fn)
"""


class CrossProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-cfgx-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("并发")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_processes_never_lose_an_update(self):
        """两个进程各改各的键：两边的键都必须留住，计数器一次都不能丢"""
        proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        n = 25
        ps = []
        for key in ("writer_a", "writer_b"):
            code = WORKER % {"root": proj, "project": self.root, "key": key, "n": n}
            ps.append(subprocess.Popen([sys.executable, "-c", code],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT))
        for p in ps:
            out = p.communicate(timeout=180)[0].decode("utf-8", "replace")
            self.assertEqual(p.returncode, 0, out)
        cfg = _cfg(self.repo)
        self.assertEqual(cfg.get("writer_a"), n, cfg)
        self.assertEqual(cfg.get("writer_b"), n, cfg)
        self.assertEqual(cfg.get("count"), 2 * n,
                         "计数器少了 → 有一方的改动被整份盖掉了：%r" % (cfg,))


MARK_WORKER = r"""
import sys
sys.path.insert(0, %(root)r)
from zgr.store import Repo
repo = Repo(%(project)r)
for oid in %(oids)r:
    for _ in range(%(n)d):
        repo.set_mark(oid, mark=%(mark)r)
"""


class MarkCrossProcessTest(unittest.TestCase):
    """marks.json 也是整份读-改-写：两个进程各打各的版本，谁的标记都不能丢

    这不只是颜色：回收（gc）靠标记保护「客户确认过的版本」，标记丢了那个版本
    就可能被当成孤儿清掉。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-markx-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("标记")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_processes_keep_every_mark(self):
        ids = []
        for i in range(6):
            with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
                f.write("第 %d 版" % i)
            ids.append(self.repo.commit("第 %d 版" % i, "我"))
        proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ps = []
        for oids, mark in ((ids[:3], "green"), (ids[3:], "red")):
            code = MARK_WORKER % {"root": proj, "project": self.root,
                                  "oids": oids, "mark": mark, "n": 5}
            ps.append(subprocess.Popen([sys.executable, "-c", code],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT))
        for p in ps:
            out = p.communicate(timeout=180)[0].decode("utf-8", "replace")
            self.assertEqual(p.returncode, 0, out)
        marks = self.repo.marks()
        for oid in ids:
            self.assertIn(oid, marks, "有一个标记被另一方的整份写回盖掉了：%r" % (marks,))
        self.assertEqual(marks[ids[0]].get("mark"), "green")
        self.assertEqual(marks[ids[-1]].get("mark"), "red")


class ApiKeepsOtherSettingsTest(unittest.TestCase):
    """网页上的设置项一个接一个保存，谁都不能把别人的整份盖掉"""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-cfgapi-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("设置")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        reg.add(self.root, "设置")
        self.app = App(reg)
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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_four_settings_all_survive(self):
        self.assertTrue(self.post("/api/author", {
            "path": self.root, "author": "老张"})["ok"])
        self.assertTrue(self.post("/api/ignore", {
            "path": self.root, "ignore": ["*.bak", "临时"]})["ok"])
        self.assertTrue(self.post("/api/auto", {
            "path": self.root, "auto_checkout": False})["ok"])
        self.assertTrue(self.post("/api/preview/peer", {
            "path": self.root, "url": "http://10.0.0.5:8795",
            "token": "t0k"})["ok"])
        cfg = _cfg(self.repo)
        self.assertEqual(cfg["author"], "老张")
        self.assertIn("*.bak", cfg["ignore"])
        self.assertEqual(cfg["auto_checkout"], False)
        self.assertEqual(cfg["preview_peer"], "http://10.0.0.5:8795")
        self.assertEqual(cfg["preview_peer_token"], "t0k")
        self.assertEqual(cfg.get("name"), "设置")

    def test_ignore_always_protects_meta_dir(self):
        """.zgr 不参与版本：用户把忽略规则清空也不能把它放进去"""
        self.post("/api/ignore", {"path": self.root, "ignore": []})
        self.assertIn(".zgr", _cfg(self.repo)["ignore"])

    def test_live_keeps_peer_and_auto_flags(self):
        """开实时同步不能把「对方是谁」「用户暂停过」这两件事抹掉"""
        self.repo.update_config(lambda c: c.update({
            "live": {"peer_host": "小冯的电脑", "peer_author": "张三",
                     "auto": False}}))
        self.app.start_live(self.repo, "http://127.0.0.1:9/", interval=600)
        live = _cfg(self.repo)["live"]
        self.assertTrue(live["enabled"])
        self.assertEqual(live["peer_host"], "小冯的电脑")
        self.assertEqual(live["peer_author"], "张三")
        self.assertFalse(live["auto"], "用户点过「暂停」，重开同步不该把自动配对又打开")
        # 再停一次：只有 enabled 变，其余照旧
        self.app.stop_live(self.root)
        live = _cfg(self.repo)["live"]
        self.assertFalse(live["enabled"])
        self.assertEqual(live["peer_host"], "小冯的电脑")

    def test_save_remote_keeps_author(self):
        from zgr import remotes as remotes_mod
        self.repo.update_config(lambda c: c.update({"author": "老张"}))
        remotes_mod.save_remote(self.repo, {"name": "备份盘", "kind": "local",
                                            "path": os.path.join(self.tmp, "nas")})
        cfg = _cfg(self.repo)
        self.assertEqual(cfg["author"], "老张")
        self.assertEqual(len(cfg["remotes"]), 1)
        remotes_mod.remove_remote(self.repo, "备份盘")
        cfg = _cfg(self.repo)
        self.assertEqual(cfg["author"], "老张")
        self.assertEqual(cfg["remotes"], [])


class CliConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-cfgcli-")
        self.root = os.path.join(self.tmp, "项目")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_watch_writes_config(self):
        import argparse
        from zgr import cli
        a = argparse.Namespace(dir=self.root, name="新项目", watch=True, interval=45)
        buf = sys.stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        try:
            cli.cmd_init(a)
        finally:
            sys.stdout.close()
            sys.stdout = buf
        cfg = _cfg(Repo(self.root))
        self.assertTrue(cfg["watch"])
        self.assertEqual(cfg["watch_interval"], 45)
        self.assertEqual(cfg.get("name"), "新项目")


if __name__ == "__main__":
    unittest.main()
