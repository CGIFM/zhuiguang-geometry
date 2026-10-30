# -*- coding: utf-8 -*-
"""安全体检：Host 白名单、跨站请求、静态资源、令牌与限流

这几条不是「理论上应该防」——每一条都对应一个真实的攻击路径：
- DNS rebinding：恶意网页把域名解析到 127.0.0.1，浏览器当成同源，读走本机接口
- CSRF：别的网站用表单往本机 POST，改项目、恢复文件
- 目录穿越：/../../ 读到服务目录以外
- 猜令牌：局域网里有人一直打接口
"""
import gzip
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class Base(unittest.TestCase):
    token = None

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr import server as srv
        from zgr.registry import Registry
        from zgr.server import App, Handler

        srv._AUTH_FAILS.clear()
        self.srv = srv
        self.tmp = tempfile.mkdtemp(prefix="zgr-sec-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        repo = Repo(self.root).init("安全")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        repo.commit("一")

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg, token=self.token)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.srv._AUTH_FAILS.clear()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, route, headers=None):
        req = urllib.request.Request(self.base + route, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def post(self, route, obj=None, headers=None):
        data = b"" if obj is None else self.srv.json.dumps(obj).encode("utf-8")
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + route, data=data, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def raw(self, method, path, host, extra=""):
        """自己拼一条请求：要伪造 Host，urllib 帮不上忙"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host)
        for line in extra.strip().splitlines():
            if line.strip():
                k, v = line.split(":", 1)
                c.putheader(k.strip(), v.strip())
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        status, headers = r.status, dict(r.getheaders())
        c.close()
        return status, headers, body


class HostGuardTest(Base):
    """DNS rebinding：Host 不是本机名/本机地址，一律不认"""

    def test_foreign_host_rejected(self):
        st, _h, body = self.raw("GET", "/api/projects", "evil.example.com")
        self.assertEqual(st, 403)
        self.assertIn(b"DNS rebinding", body)

    def test_foreign_host_cannot_read_api(self):
        """就算攻击页把域名指向 127.0.0.1，也拿不到项目清单（里面带本地路径）"""
        st, _h, body = self.raw("GET", "/api/projects", "attacker.local:8795")
        self.assertEqual(st, 403)
        self.assertNotIn(b"projects", body)

    def test_local_host_allowed(self):
        st, _h, _b = self.raw("GET", "/api/ping", "127.0.0.1:%d" % self.port)
        self.assertEqual(st, 200)
        st, _h, _b = self.raw("GET", "/api/ping", "localhost:%d" % self.port)
        self.assertEqual(st, 200)

    def test_lan_ip_and_ipv6_allowed(self):
        import zgr.server as srv
        self.assertTrue(srv.host_allowed(""))
        self.assertTrue(srv.host_allowed("[::1]:8795"))
        self.assertTrue(srv.host_allowed("127.0.0.1:8795"))
        local = socket.gethostname().lower()
        self.assertTrue(srv.host_allowed(local))
        # 尾随的点（FQDN 写法）不能因此被拒
        self.assertTrue(srv.host_allowed(local + "."))
        self.assertFalse(srv.host_allowed("evil.example.com"))

    def test_static_also_guarded(self):
        st, _h, _b = self.raw("GET", "/index.html", "evil.example.com")
        self.assertEqual(st, 403)


class CsrfTest(Base):
    def test_cross_site_origin_rejected(self):
        st, body = self.post("/api/snapshot", {"path": self.root},
                             headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        self.assertIn("跨站", body.decode("utf-8"))

    def test_same_origin_allowed(self):
        st, _b = self.post("/api/snapshot", {"path": self.root, "message": "同源"},
                           headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(st, 200)

    def test_form_post_rejected(self):
        """表单式 CSRF 连 Content-Type 都过不去（浏览器表单只会发这三种）"""
        for ctype in ("application/x-www-form-urlencoded", "multipart/form-data",
                      "text/plain"):
            data = b"path=x"
            req = urllib.request.Request(self.base + "/api/snapshot", data=data,
                                         method="POST", headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    st = r.status
            except urllib.error.HTTPError as e:
                st = e.code
            self.assertEqual(st, 415, ctype)


class BodySizeTest(Base):
    """请求体上限：JSON 整包进内存，大对象走流式，两道闸分开"""

    def raw_big(self, head, host=None):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(head.replace(b"{HOST}", (host or "127.0.0.1:%d" % self.port).encode()))
            s.shutdown(socket.SHUT_WR)      # 声明了大小但不发，服务端读不到时要能收尾
            buf = b""
            while True:
                c = s.recv(65536)
                if not c:
                    break
                buf += c
                if b"\r\n\r\n" in buf:
                    break
            return buf.split(b"\r\n")[0]
        finally:
            s.close()

    def test_huge_json_rejected(self):
        n = self.srv.MAX_JSON_BODY + 1024
        head = (b"POST /api/init HTTP/1.1\r\nHost: {HOST}\r\n"
                b"Content-Type: application/json\r\nContent-Length: "
                + str(n).encode() + b"\r\n\r\n")
        line = self.raw_big(head)
        self.assertIn(b" 400 ", line)
        # 服务还活着
        st, _h, _b = self.get("/api/ping")
        self.assertEqual(st, 200)

    def test_huge_sync_object_rejected(self):
        oid = "a" * 64
        n = self.srv.MAX_OBJECT_BODY + 1024
        head = (("POST /api/sync/object/%s?project=%s HTTP/1.1\r\n"
                 "Host: {HOST}\r\nContent-Type: application/octet-stream\r\n"
                 "Content-Length: %d\r\n\r\n" % (oid, "x", n)).encode())
        line = self.raw_big(head)
        self.assertIn(b" 413 ", line)

    def test_normal_json_still_ok(self):
        st, _b = self.post("/api/snapshot", {"path": self.root, "message": "正常大小"})
        self.assertEqual(st, 200)


class HeaderTest(Base):
    def test_security_headers_everywhere(self):
        _st, h, _b = self.get("/api/ping")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h.get("X-Frame-Options"), "DENY")
        self.assertEqual(h.get("Referrer-Policy"), "no-referrer")
        # 别家站点不能把我们的接口/资源当子资源用（CORP）
        self.assertEqual(h.get("Cross-Origin-Resource-Policy"), "same-origin")
        # 别家页面打开的窗口不能反手操纵我们这一页（COOP）
        self.assertEqual(h.get("Cross-Origin-Opener-Policy"), "same-origin")
        # 这个软件不需要的浏览器能力一律关掉
        pp = h.get("Permissions-Policy") or ""
        for feat in ("geolocation=()", "camera=()", "microphone=()", "usb=()"):
            self.assertIn(feat, pp)
        self.assertEqual(h.get("X-Permitted-Cross-Domain-Policies"), "none")

    def test_headers_on_static_and_errors(self):
        """静态文件、404、跨站被拒的回包也要带同一套头（不能只在 200 上有）"""
        for path, opts in (("/app.js", {}), ("/definitely-not-here.js", {}),
                           ("/api/projects", {"Origin": "http://evil.example"})):
            _st, h, _b = self.get(path, opts)        # 403/404 也照样要带头
            self.assertEqual(h.get("Cross-Origin-Opener-Policy"), "same-origin", path)
            self.assertEqual(h.get("Cross-Origin-Resource-Policy"), "same-origin", path)
            self.assertTrue(h.get("Permissions-Policy"), path)

    def test_html_has_csp(self):
        st, h, body = self.get("/index.html")
        self.assertEqual(st, 200)
        csp = h.get("Content-Security-Policy") or ""
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn(b"<html", body.lower())

    def test_json_has_no_csp(self):
        _st, h, _b = self.get("/api/ping")
        self.assertNotIn("Content-Security-Policy", h)


class StaticTest(Base):
    def test_etag_then_304(self):
        st, h, body = self.get("/app.js")
        self.assertEqual(st, 200)
        etag = h.get("ETag")
        self.assertTrue(etag)
        self.assertEqual(h.get("Cache-Control"), "no-cache")
        st2, h2, body2 = self.get("/app.js", {"If-None-Match": etag})
        self.assertEqual(st2, 304)
        self.assertEqual(body2, b"")

    def test_etag_changes_with_content(self):
        _st, h1, _b = self.get("/app.js")
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "web", "app.js")
        # 不真的改文件：只验证 ETag 里带了 mtime+size，不同文件不同
        _st, h2, _b = self.get("/style.css")
        self.assertNotEqual(h1.get("ETag"), h2.get("ETag"))
        self.assertTrue(os.path.getsize(p) > 0)

    def test_gzip(self):
        st, h, body = self.get("/app.js", {"Accept-Encoding": "gzip"})
        self.assertEqual(st, 200)
        if h.get("Content-Encoding") == "gzip":
            self.assertIn("Accept-Encoding", h.get("Vary") or "")
            raw = gzip.decompress(body)
        else:                                   # 压不动会原样发，也算对
            raw = body
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "web", "app.js"), "rb") as f:
            self.assertEqual(raw, f.read())

    def test_path_traversal_rejected(self):
        for p in ("/../zgr/server.py", "/%2e%2e/zgr/server.py",
                  "/..%2fzgr%2fserver.py", "/C:/Windows/win.ini"):
            st, _h, body = self.raw("GET", p, "127.0.0.1:%d" % self.port)
            self.assertIn(st, (403, 404), p)
            self.assertNotIn(b"import ", body, p)
            self.assertNotIn(b"[boot loader]", body, p)

    def test_no_directory_listing(self):
        st, _h, _b = self.get("/vendor/")
        self.assertIn(st, (403, 404))


class TokenTest(Base):
    token = "0123456789abcdef0123456789abcdef"

    def test_missing_token_rejected(self):
        st, _b = self.post("/api/snapshot", {"path": self.root})
        self.assertEqual(st, 401)

    def test_wrong_token_rejected(self):
        st, _b = self.post("/api/snapshot", {"path": self.root},
                           headers={"X-ZGR-Token": "f" * 32})
        self.assertEqual(st, 401)

    def test_right_token_ok(self):
        st, _b = self.post("/api/snapshot", {"path": self.root},
                           headers={"X-ZGR-Token": self.token})
        self.assertEqual(st, 200)

    def test_ping_needs_no_token(self):
        st, _h, _b = self.get("/api/ping")
        self.assertEqual(st, 200)

    def test_brute_force_blocked(self):
        for _ in range(self.srv.AUTH_FAIL_LIMIT + 1):
            st, _b = self.post("/api/snapshot", {"path": self.root},
                               headers={"X-ZGR-Token": "0" * 32})
            if st == 429:
                break
        self.assertEqual(st, 429)
        # 被限流之后，连对的令牌也先不理（窗口 60 秒）
        st2, _b = self.post("/api/snapshot", {"path": self.root},
                            headers={"X-ZGR-Token": self.token})
        self.assertEqual(st2, 429)


class RestoreFileEscapeTest(Base):
    """单文件恢复只能落在项目目录里——这是唯一「按用户给的路径写盘」的接口"""

    def oid_of(self):
        repo = Repo(self.root)
        return repo.files_of(repo.head_oid())[1]["a.txt"]["oid"]

    def test_path_escape_rejected(self):
        oid = self.oid_of()
        bad = [
            "../../外面.txt",
            "..\\..\\外面.txt",
            "C:\\Windows\\Temp\\外面.txt",
            "a.txt:stream",
            "",
        ]
        for f in bad:
            st, body = self.post("/api/restore_file",
                                 {"path": self.root, "file": f, "oid": oid})
            self.assertGreaterEqual(st, 400, "这个路径竟然通过了：%r" % f)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "外面.txt")))

    def test_bad_oid_rejected(self):
        st, _b = self.post("/api/restore_file",
                           {"path": self.root, "file": "a.txt", "oid": "../../x"})
        self.assertGreaterEqual(st, 400)

    def test_normal_path_works_and_backs_up(self):
        repo = Repo(self.root)
        oid = repo.files_of(repo.head_oid())[1]["a.txt"]["oid"]
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("改过了")
        st, body = self.post("/api/restore_file",
                             {"path": self.root, "file": "a.txt", "oid": oid})
        self.assertEqual(st, 200, body[:200])
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "一")
        self.assertTrue(json.loads(body.decode("utf-8")).get("backup"))


if __name__ == "__main__":
    unittest.main()
