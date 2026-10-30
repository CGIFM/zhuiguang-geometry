# -*- coding: utf-8 -*-
"""接口乱打：随机路径/方法/畸形请求体，要求 4xx、不能 500、不能把服务打挂

工业现场没人会照着接口文档点，误操作、旧书签、半截请求都会发生。
"""
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class ApiFuzzTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-fuzz-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        repo = Repo(self.root).init("乱打")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        repo.commit("一")

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
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

    def call(self, route, data=None, method="POST", ctype="application/json"):
        req = urllib.request.Request(self.base + route, data=data, method=method,
                                     headers={"Content-Type": ctype} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_500_random_requests_no_server_error(self):
        rnd = random.Random(20261029)
        routes = ["/api/add", "/api/remove", "/api/restore", "/api/snapshot", "/api/diff",
                  "/api/status", "/api/projects", "/api/init", "/api/checkout",
                  "/api/branch", "/api/pack", "/api/undo", "/api/merge/apply",
                  "/api/export", "/api/export/step", "/api/live", "/api/preview",
                  "/api/nonexistent", "/api/../api/add", "/" + "x" * 300]
        bodies = [b"", b"{}", b"[]", b"null", b"{", b'{"path":null}', b'{"path":""}',
                  b'{"path":"C:\\\\Windows"}', b'{"path":"..\\\\..\\\\etc"}',
                  b'{"path":"\\u0000"}', b'{"commit":"zzz"}', b'{"files":123}',
                  b"[" * 200, b'{"path":"' + b"a" * 5000 + b'"}']
        bad = []
        for _ in range(500):
            route = rnd.choice(routes)
            body = rnd.choice(bodies)
            method = rnd.choice(["GET", "POST"])
            try:
                code, raw = self.call(route, body if method == "POST" else None, method)
            except Exception as e:            # noqa: BLE001
                bad.append((route, method, repr(e)))
                continue
            if code >= 500:
                bad.append((route, method, code, raw[:120]))
        self.assertEqual(bad, [], "接口出现 5xx 或直接把连接打断：%s" % bad[:5])
        # 打完这么多烂请求，服务必须还活着
        code, raw = self.call("/api/ping", None, "GET")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw.decode("utf-8"))["ok"])

    def test_binary_garbage_body(self):
        for junk in (bytes(range(256)) * 4, b"\x1f\x8b\x08\x00", b"\xff\xfe\x00"):
            code, _ = self.call("/api/snapshot", junk)
            self.assertLess(code, 500, "二进制垃圾把接口打出了 5xx")

    def test_huge_query_string(self):
        code, _ = self.call("/api/projects?" + "a=1&" * 4000, None, "GET")
        self.assertLess(code, 500)

    # ---- 回包前必须先吃掉请求体（否则 Windows 直接 RST，用户看到「连接被重置」）
    def raw_call(self, head, body=b"", timeout=20):
        """用裸 socket 发请求，拿到原始响应字节；被 RST 会抛异常"""
        import socket
        host, port = self.httpd.server_address[:2]
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            s.sendall(head)
            if body:
                s.sendall(body)
            chunks = []
            while True:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                if b"\r\n\r\n" in b"".join(chunks):
                    break
            return b"".join(chunks)
        finally:
            s.close()

    def test_unread_body_not_reset(self):
        """5000 字节 body 打到不存在的接口：必须回 404，不能断开连接。
        这条以前偶发「远程主机强迫关闭了一个现有的连接」，500 次里炸 1 次。"""
        body = b'{"path":"' + b"a" * 5000 + b'"}'
        head = ("POST /api/nonexistent-%d HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                % (len(body), len(body))).encode("ascii")
        for _ in range(120):
            raw = self.raw_call(head, body)
            self.assertIn(b" 404 ", raw.split(b"\r\n")[0], raw[:200])

    def test_bad_json_leaves_no_unread_bytes(self):
        """坏 JSON 已经吃掉了，别再等第二批字节（以前这里会挂住直到客户端超时）"""
        body = b'{"path": ' + b"x" * 300
        head = ("POST /api/init HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        import time
        t0 = time.time()
        raw = self.raw_call(head, body, timeout=15)
        self.assertIn(b" 400 ", raw.split(b"\r\n")[0], raw[:200])
        self.assertLess(time.time() - t0, 5, "坏请求体把服务端卡住了")

    def test_415_and_405_do_not_drop_connection(self):
        body = b'{"path":"x"}'
        head = ("POST /api/init HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\n"
                "Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        self.assertIn(b" 415 ", self.raw_call(head, body).split(b"\r\n")[0])
        head = ("PUT /api/init HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n" % len(body)).encode("ascii")
        self.assertIn(b" 405 ", self.raw_call(head, body).split(b"\r\n")[0])

    def test_chunked_rejected_cleanly(self):
        head = (b"POST /api/init HTTP/1.0\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n")
        raw = self.raw_call(head, b"0\r\n\r\n", timeout=10)
        self.assertIn(b" 411 ", raw.split(b"\r\n")[0], raw[:200])

    def test_server_alive_after_unread_bodies(self):
        """上面折腾完，服务还得能正常干活"""
        code, raw = self.call("/api/ping", None, "GET")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw.decode("utf-8"))["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
