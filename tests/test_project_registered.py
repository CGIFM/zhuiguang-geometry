# -*- coding: utf-8 -*-
"""用链接打开的项目，必须知道自己「还没加入项目列表」

以前这种情况界面上完全看不出来：顶上开着项目，左边列表空空如也，
看着像「项目没打开」或者「列表坏了」。接口要把这件事说清楚，
界面才有办法给人一个「加入列表」的出口。

顺带守住另一条：对象个数只是给人看的数字，不能每次刷新都把几万个文件数一遍。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.registry import Registry         # noqa: E402
from zgr.server import App, Handler       # noqa: E402
from zgr.store import Repo                # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-reg-")
        self.root = os.path.join(self.tmp, "示例项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("示例项目")
        for i in range(3):
            with open(os.path.join(self.root, "件%d.sldprt" % i), "w", encoding="utf-8") as f:
                f.write("x")
        self.repo.commit("一")
        self.app = App(Registry(path=os.path.join(self.tmp, "projects.json")))

    def tearDown(self):
        self.app.fs_watch.stop_all()
        shutil.rmtree(self.tmp, ignore_errors=True)


class RegisteredFlagTest(Base):
    def test_open_by_link_is_not_registered(self):
        d = self.app.project_detail(self.root)
        self.assertIs(d["registered"], False,
                      "用链接打开、没进列表的项目必须能看出「没登记」")

    def test_after_add_it_is_registered(self):
        self.app.registry.add(self.root, "示例项目")
        d = self.app.project_detail(self.root)
        self.assertTrue(d["registered"])

    def test_path_case_and_separator_do_not_matter(self):
        self.app.registry.add(self.root.upper() if os.name == "nt" else self.root, "示例项目")
        self.assertTrue(self.app.project_detail(self.root)["registered"],
                        "c:\\a 和 C:/a 是同一个目录，不能当成两个项目")


class ObjectsCountTest(Base):
    def test_objects_counted_once_then_cached(self):
        from unittest import mock
        real_walk = os.walk
        seen = []

        def counting(path, *a, **k):
            if str(path).endswith(os.path.join(".zgr", "objects")):
                seen.append(path)
            return real_walk(path, *a, **k)

        with mock.patch("os.walk", counting):
            d1 = self.app.project_detail(self.root)
            self.app.project_detail(self.root)
            self.app.project_detail(self.root)
        self.assertGreater(d1["objects"], 0)
        self.assertEqual(len(seen), 1,
                         "对象个数只该数一次，之后一分钟内用缓存（界面每次刷新都调这个接口）")


class ApiTest(Base):
    def setUp(self):
        super().setUp()
        handler = type("HR", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, route, **params):
        url = self.base + route + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, body):
        req = urllib.request.Request(self.base + route,
                                     data=json.dumps(body).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_add_endpoint_turns_it_registered(self):
        d = self.get("/api/project", path=self.root)
        self.assertFalse(d["registered"])
        self.post("/api/add", {"path": self.root, "target": self.root, "name": "示例项目"})
        d = self.get("/api/project", path=self.root)
        self.assertTrue(d["registered"])
        names = [p["path"] for p in self.get("/api/projects")["projects"]]
        self.assertIn(self.root, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
