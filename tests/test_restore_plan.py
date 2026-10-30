# -*- coding: utf-8 -*-
"""恢复前的试算（防呆）：先算清楚会发生什么，再让人按确认

「恢复整个项目到此版本」是这套软件里唯一一个会批量改工作区的操作。
以前确认框只说「确定恢复？」，用户看不到会覆盖几个、删掉几个；
试算就是为了让人在按下去之前看见后果。
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-plan-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("试算")
        self.write("a.txt", "甲")
        self.write("b.txt", "乙")
        self.c1 = self.repo.commit("第一版")

    def tearDown(self):
        for dp, _dn, fns in os.walk(self.tmp):        # 只读文件会让 rmtree 失败
            for fn in fns:
                try:
                    os.chmod(os.path.join(dp, fn), stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel.replace("/", os.sep)), encoding="utf-8") as f:
            return f.read()

    def test_counts_overwrite_create_remove_same(self):
        self.write("b.txt", "乙改")          # 未记录修改
        self.write("c.txt", "丙")            # 未记录新增
        p = self.repo.restore_plan(self.c1)
        self.assertEqual(p["overwrite"], 1)  # b 回到旧内容
        self.assertEqual(p["create"], 0)
        self.assertEqual(p["remove"], 1)     # c 这一版没有 → 要移除
        self.assertEqual(p["same"], 1)       # a 一个字节都不用动
        self.assertEqual(p["total"], 2)
        self.assertIn("b.txt", p["overwrite_sample"])
        self.assertIn("c.txt", p["remove_sample"])
        self.assertGreater(p["bytes"], 0)

    def test_unrecorded_changes_are_flagged(self):
        """工作区里还没记录的改动，会在恢复时被覆盖/删掉——必须提前点名"""
        self.write("b.txt", "乙改")          # 记录之后又改过
        self.write("c.txt", "丙")            # 还没记录过的新文件
        p = self.repo.restore_plan(self.c1)
        self.assertEqual(p["dirty"], 2)
        self.assertEqual(p["dirty_mod"], 1)  # b：改过
        self.assertEqual(p["dirty_new"], 1)  # c：新增且会被移除
        kinds = {d["path"]: d["kind"] for d in p["dirty_sample"]}
        self.assertEqual(kinds, {"b.txt": "mod", "c.txt": "new"})

    def test_clean_workspace_has_no_dirty(self):
        p = self.repo.restore_plan(self.c1)
        self.assertEqual(p["dirty"], 0)
        self.assertEqual(p["dirty_sample"], [])

    def test_same_content_is_not_dirty(self):
        """改完并且已经记录过的文件，回到那一版不算「未记录的改动」"""
        self.write("a.txt", "甲改")
        c2 = self.repo.commit("第二版")
        p = self.repo.restore_plan(c2)
        self.assertEqual(p["dirty"], 0)
        self.assertEqual(p["overwrite"] + p["create"] + p["remove"], 0)

    def test_plan_is_read_only(self):
        self.write("b.txt", "乙改")
        before = self.read("b.txt")
        self.repo.restore_plan(self.c1)
        self.assertEqual(self.read("b.txt"), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "c.txt")))

    def test_clean_workspace_has_nothing_to_do(self):
        p = self.repo.restore_plan(self.c1)
        self.assertEqual((p["overwrite"], p["create"], p["remove"]), (0, 0, 0))
        self.assertEqual(p["same"], 2)
        self.assertEqual(p["bytes"], 0)

    def test_blocked_when_readonly(self):
        self.write("b.txt", "乙改")
        os.chmod(os.path.join(self.root, "b.txt"), stat.S_IREAD)
        p = self.repo.restore_plan(self.c1)
        self.assertIn("b.txt", p["blocked"])

    def test_external_target_all_create(self):
        out = os.path.join(self.tmp, "导出")
        p = self.repo.restore_plan(self.c1, target=out)
        self.assertEqual(p["create"], 2)
        self.assertEqual(p["overwrite"], 0)
        self.assertEqual(p["remove"], 0)

    def test_ignored_files_not_counted_as_removed(self):
        self.write("b.txt", "乙改")
        self.write("临时.tmp.zgr-part", "半截文件")
        p = self.repo.restore_plan(self.c1)
        self.assertEqual(p["remove"], 0)     # 临时文件本来就不进版本，也不算删

    def test_bad_commit_reported(self):
        with self.assertRaises(Exception):
            self.repo.restore_plan("f" * 64)


class PlanApiTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-planapi-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        repo = Repo(self.root).init("试算接口")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.c1 = repo.commit("一")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("改了")

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg)
        handler = type("H", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, route, obj):
        req = urllib.request.Request(self.base + route,
                                     data=json.dumps(obj).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_dry_run_returns_plan_and_changes_nothing(self):
        st, d = self.post("/api/restore", {"path": self.root, "commit": self.c1,
                                           "dry": True})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["plan"]["overwrite"], 1)
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "改了")     # 试算一个字节都不动

    def test_real_restore_still_works(self):
        st, d = self.post("/api/restore", {"path": self.root, "commit": self.c1,
                                           "backup": True})
        self.assertEqual(st, 200)
        self.assertEqual(d["written"], 1)
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "一")


if __name__ == "__main__":
    unittest.main()
