# -*- coding: utf-8 -*-
"""部署自检：问对面「你跑的是哪一版」

部署最容易出的岔子不是没传文件，而是**传了但服务没重启**——跑的还是旧代码。
只看复制结果看不出来，只有问服务本身才知道。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "tools"))

import check_deployed as cd          # noqa: E402
from zgr import __version__          # noqa: E402


def serve(version, token=None):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/api/ping":
                self.send_error(404)
                return
            if token and self.headers.get("X-ZGR-Token") != token:
                self.send_error(403)
                return
            body = json.dumps({"ok": True, "app": "追光几何",
                               "version": version}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.srvs = []

    def tearDown(self):
        for s in self.srvs:
            s.shutdown()

    def start(self, *a, **kw):
        s = serve(*a, **kw)
        self.srvs.append(s)
        return "http://127.0.0.1:%d" % s.server_address[1]

    def test_same_version_passes(self):
        r = cd.probe(self.start(__version__))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["version"], __version__)
        self.assertEqual(cd.report([r], __version__), 0)

    def test_old_version_is_reported(self):
        r = cd.probe(self.start("1.0.0"))
        self.assertTrue(r["ok"])
        self.assertEqual(cd.report([r], __version__), 1, "版本不一样必须报失败")

    def test_unreachable_is_not_a_crash(self):
        r = cd.probe("http://127.0.0.1:9")      # 这个端口基本不会有人监听
        self.assertFalse(r["ok"])
        self.assertIn("连不上", r["error"])
        self.assertEqual(cd.report([r], __version__), 1)

    def test_token_is_sent(self):
        url = self.start(__version__, token="s3cret")
        self.assertFalse(cd.probe(url)["ok"], "没带令牌应该被拒")
        self.assertTrue(cd.probe(url, token="s3cret")["ok"])

    def test_bare_host_becomes_url(self):
        self.assertEqual(cd.as_url("192.168.1.20"), "http://192.168.1.20:8795")
        self.assertEqual(cd.as_url("http://1.2.3.4:9000/"), "http://1.2.3.4:9000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
