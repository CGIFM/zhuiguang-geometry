# -*- coding: utf-8 -*-
"""第 86 轮安全修复的回归测试

这一轮修的都是「局域网里、或者被同步进来之后」才会被利用的口子：
- 发现对端时不能把访问令牌广播给整个 /24；
- 解压对象要有上限（压缩炸弹几百 KB 就能吃光内存）；
- 「打开文件」只放行已知文档，不放行可执行/脚本类；
- 共享目录不能是盘符根或系统目录；
- /api/repair 不能变成「让服务端去访问任意地址」的口子。
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

import zgr.autopeer as autopeer                 # noqa: E402
import zgr.server as servermod                  # noqa: E402
from zgr.store import Repo                      # noqa: E402


class Base(unittest.TestCase):
    TOKEN = "s3cret-token"

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-sec86-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("安全")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        self.repo.commit("一版", "我")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg, token=self.TOKEN, default_project=self.root)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, route, **params):
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(self.url + route + ("?" + q if q else ""))
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}

    def post(self, route, body, token=None):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + route, data=data,
                                     headers={"Content-Type": "application/json",
                                              **({"X-ZGR-Token": token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}


class DiscoverTest(Base):
    def test_discover_is_open_so_pairing_never_needs_the_token(self):
        code, d = self.get("/api/sync/discover")
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("ok"))
        self.assertIn("projects", d)
        # 只报名字/记录人：不返回任何本地路径
        for p in d["projects"]:
            self.assertNotIn("path", p)
        self.assertNotIn(str(self.root), json.dumps(d, ensure_ascii=False))

    def test_other_sync_routes_still_need_the_token(self):
        code, _ = self.get("/api/sync/refs", project="安全")
        self.assertEqual(code, 401)

    def test_scan_never_sends_the_token(self):
        calls = []

        def fake_discover(port=8795, token="", **kw):
            calls.append(token)
            return [{"url": "http://127.0.0.1:1", "host": "x", "projects": []}]

        old = autopeer.discover
        autopeer.discover = fake_discover
        try:
            self.app.scan_peers(token=self.TOKEN, force=True)
            self.app._peer_cache = (0.0, [])
            self.app.peer_projects("", self.TOKEN)
        finally:
            autopeer.discover = old
        self.assertTrue(calls, "应该至少扫过一次")
        for tok in calls:
            self.assertEqual(tok, "", "发现阶段绝不能带令牌：%r" % (tok,))


class DecompressTest(Base):
    def test_bomb_is_rejected(self):
        import zlib
        bomb = zlib.compress(b"\x00" * (8 << 20), 9)
        self.assertLess(len(bomb), 100 << 10, "炸弹本身应该很小")
        with self.assertRaises(Exception) as ctx:
            self.repo._decompress_limited(bomb, limit=1 << 20)
        self.assertIn("上限", str(ctx.exception))
        # 归还内存：小上限下不该吃掉 8MB
        self.assertLess(len(bomb), 100 << 10)

    def test_normal_object_still_reads(self):
        oid = self.repo.head_oid()
        kind, payload = self.repo.get(oid)
        self.assertEqual(kind, "commit")
        self.assertTrue(payload)

    def test_truncated_stream_rejected(self):
        import zlib
        data = zlib.compress(b"x" * 1000)
        with self.assertRaises(Exception):
            self.repo._decompress_limited(data[:len(data) // 2])


class OpenFileTest(Base):
    def test_script_extensions_refused(self):
        with open(os.path.join(self.root, "说明.scf"), "w", encoding="utf-8") as f:
            f.write("[Shell]\r\nCommand=calc.exe")
        code, d = self.post("/api/openfile",
                            {"path": self.root, "file": "说明.scf"}, token=self.TOKEN)
        self.assertEqual(code, 403, d)
        self.assertIn("不直接打开", json.dumps(d, ensure_ascii=False))

    def test_trailing_dot_cannot_smuggle_an_exe(self):
        with open(os.path.join(self.root, "看图.exe."), "w", encoding="utf-8") as f:
            f.write("MZ")
        code, _ = self.post("/api/openfile",
                            {"path": self.root, "file": "看图.exe."}, token=self.TOKEN)
        self.assertEqual(code, 403)

    def test_whitelist_contents(self):
        from zgr.server import Handler
        self.assertNotIn(".scf", Handler.OPENABLE_EXT)
        self.assertNotIn(".py", Handler.OPENABLE_EXT)
        self.assertIn(".dwg", Handler.OPENABLE_EXT)


class SharePathTest(unittest.TestCase):
    def check(self, p):
        return servermod.check_share_path(p)

    def test_rejects_drive_root(self):
        with self.assertRaises(Exception):
            self.check("D:\\")

    def test_rejects_windows_dir(self):
        root = os.environ.get("SystemRoot") or "C:\\Windows"
        with self.assertRaises(Exception):
            self.check(os.path.join(root, "temp"))

    def test_allows_share_and_normal_dir(self):
        self.assertEqual(self.check("\\\\nas\\备份\\追光"), "\\\\nas\\备份\\追光")
        tmp = tempfile.mkdtemp(prefix="zgr-share-")
        try:
            self.assertEqual(self.check(tmp), tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            self.check("   ")


class RepairUrlTest(Base):
    def test_public_url_refused(self):
        code, d = self.post("/api/repair",
                            {"path": self.root, "url": "http://8.8.8.8:80/x"},
                            token=self.TOKEN)
        self.assertIn(code, (400, 403), d)
        self.assertNotIn("8.8.8.8", json.dumps(d, ensure_ascii=False).replace(
            "8.8.8.8 是公网地址", ""))


class TimeoutTest(unittest.TestCase):
    def test_handler_has_read_timeout(self):
        from zgr.server import Handler
        self.assertEqual(Handler.timeout, 30, "慢速连接必须有读超时，否则线程会被占满")


class RememberPeerLockTest(Base):
    """回归：`_remember_peer` 是 Handler 的方法，锁必须取 app 的。

    曾经写成 `with self.lock:` —— Handler 没有 lock，第一次手动同步
    就 AttributeError，被兜底当成「内部错误 500」，整条对端同步/合并不可用。
    """

    def test_writes_config_with_app_lock(self):
        import types
        from zgr.server import Handler
        fake = types.SimpleNamespace(app=types.SimpleNamespace(
            lock=threading.RLock()))
        Handler._remember_peer(fake, self.repo, "http://10.0.0.5:8795", "tk")
        cfg = self.repo.config
        self.assertEqual(cfg.get("preview_peer"), "http://10.0.0.5:8795")
        self.assertEqual(cfg.get("preview_peer_token"), "tk")

    def test_sync_endpoint_does_not_500_on_bad_peer(self):
        # 对端根本连不上：应该是可读的同步失败（4xx/5xx 带话），不是「内部错误」
        code, d = self.post("/api/sync", {"path": self.root,
                                          "url": "http://127.0.0.1:1",
                                          "push": False, "pull": True},
                            token=self.TOKEN)
        self.assertNotIn("内部错误", str(d.get("error") or ""), d)


if __name__ == "__main__":
    unittest.main()
