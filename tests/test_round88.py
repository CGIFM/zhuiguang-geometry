# -*- coding: utf-8 -*-
"""第 88 轮：体检搬到网页上；自动记录的说明说人话；命令行恢复先预演再动手

三件事都指向同一个问题：**事后能不能认出来**。
- 时间线上「自动记录 10-30 14:05」一天十几条，分不清哪条是改底盘那回；
- 网页上只有一个「体检」，查的是对象坏没坏，但人更常踩的是「有东西没记录」「盘满了」；
- `zgr restore` 一句话就把工作区盖掉，连「会覆盖几个」都不说。
"""
import argparse
import contextlib
import io
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

from zgr import cli                                  # noqa: E402
from zgr import doctor as doctor_mod                 # noqa: E402
from zgr.server import auto_commit_message           # noqa: E402
from zgr.store import Repo                            # noqa: E402


class AutoMessageTest(unittest.TestCase):
    def test_counts_and_names(self):
        st = {"added": ["新零件.sldprt"], "modified": ["底盘.sldasm"],
              "deleted": [], "counts": {"added": 1, "modified": 1, "deleted": 0}}
        msg = auto_commit_message(st, now_ms=1793000000000)
        self.assertTrue(msg.startswith("自动记录 "), msg)
        self.assertIn("改 2 个", msg)
        self.assertIn("底盘.sldasm", msg)
        self.assertIn("新零件.sldprt", msg)

    def test_many_files_are_summarised(self):
        st = {"added": ["a.sldprt", "b.sldprt", "c.sldprt", "d.sldprt"],
              "modified": [], "deleted": []}
        msg = auto_commit_message(st, now_ms=1793000000000)
        self.assertIn("改 4 个", msg)
        self.assertIn("等", msg, "超过两个要写成「…等」，不能一列到底")

    def test_long_name_is_cut(self):
        st = {"added": ["超长名字" * 10 + ".sldprt"], "modified": [], "deleted": []}
        msg = auto_commit_message(st, now_ms=1793000000000)
        self.assertLess(len(msg), 80, msg)
        self.assertIn("…", msg)

    def test_nothing_changed_still_valid(self):
        msg = auto_commit_message({"added": [], "modified": [], "deleted": []},
                                  now_ms=1793000000000)
        self.assertTrue(msg.startswith("自动记录 "))
        self.assertNotIn("改 0 个", msg)


class DoctorReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-r88-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("八八")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_report_shape(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        rep = doctor_mod.report(self.repo)
        for key in ("ok", "name", "root", "branch", "head", "bad", "warn", "good"):
            self.assertIn(key, rep)
        self.assertTrue(any("工作区干净" in x["msg"] for x in rep["good"]), rep)
        for item in rep["bad"] + rep["warn"]:
            self.assertIn("msg", item)
            self.assertIn("todo", item)

    def test_dirty_is_bad_and_has_todo(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        self.write("b.txt", "2")
        rep = doctor_mod.report(self.repo)
        self.assertFalse(rep["ok"])
        hits = [x for x in rep["bad"] if "还没记录" in x["msg"]]
        self.assertTrue(hits, rep["bad"])
        self.assertIn("zgr snapshot", hits[0]["todo"])

    def test_cli_json_output(self):
        self.write("a.txt", "1")
        self.repo.commit("一版", "我")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_doctor(argparse.Namespace(dir=self.root, json=True))
        d = json.loads(buf.getvalue())
        self.assertEqual(d["name"], "八八")


class DoctorApiTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler
        self.tmp = tempfile.mkdtemp(prefix="zgr-r88api-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("八八接口")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("1")
        self.repo.commit("一版", "我")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.root)
        handler = type("H", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_doctor_endpoint(self):
        q = urllib.parse.urlencode({"path": self.root})
        with urllib.request.urlopen(self.url + "/api/doctor?" + q, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        self.assertTrue(d["ok"])
        rep = d["report"]
        self.assertEqual(rep["name"], "八八接口")
        self.assertTrue(any("工作区干净" in x["msg"] for x in rep["good"]), rep)


class CliRestoreGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-r88rst-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("恢复")
        self.write("a.txt", "一版")
        self.c1 = self.repo.commit("一版", "我")
        self.write("a.txt", "二版")
        self.c2 = self.repo.commit("二版", "我")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def restore(self, **kw):
        a = argparse.Namespace(dir=self.root, commit=self.c1, target=None,
                               no_mirror=False, no_backup=False,
                               dry=False, yes=False)
        for k, v in kw.items():
            setattr(a, k, v)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_restore(a)
        return code, buf.getvalue()

    def read(self):
        with open(os.path.join(self.root, "a.txt"), "r", encoding="utf-8") as f:
            return f.read()

    def test_dry_run_touches_nothing(self):
        self.write("a.txt", "还没记录的改动")
        code, text = self.restore(dry=True)
        self.assertEqual(code, 0, text)
        self.assertIn("演练", text)
        self.assertIn("覆盖 1 个", text)
        self.assertEqual(self.read(), "还没记录的改动", "预演不能动文件")

    def test_dirty_worktree_needs_yes(self):
        self.write("a.txt", "还没记录的改动")
        code, text = self.restore()
        self.assertEqual(code, 1, text)
        self.assertIn("--yes", text)
        self.assertEqual(self.read(), "还没记录的改动")

    def test_dirty_with_yes_restores(self):
        self.write("a.txt", "还没记录的改动")
        code, text = self.restore(yes=True)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.read(), "一版")

    def test_clean_worktree_does_not_ask(self):
        code, text = self.restore()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.read(), "一版")


if __name__ == "__main__":
    unittest.main()
