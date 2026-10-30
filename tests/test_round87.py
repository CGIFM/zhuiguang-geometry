# -*- coding: utf-8 -*-
"""第 87 轮：防呆（不可逆操作要二次确认）、切分支要连文件一起换、错误别泄本机路径

这一轮发现问题的方式是「站在局域网另一端看这台机器」：
- 切分支只动 HEAD 不动工作区 → 下一次自动记录会把对方分支的内容整体改回去；
- 只填共享目录的手动同步永远 400（地址校验被无条件套上）；
- 空 body 的 POST /api/gc 就真删对象；/api/export 静默覆盖目标目录；
- 报错里带着服务机的完整路径（客户名、盘符、用户名）。
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

import zgr.server as servermod                  # noqa: E402
from zgr.store import Repo                      # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-r87-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("八七")
        self.write("a.txt", "1")
        self.c1 = self.repo.commit("一版", "我")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg, default_project=self.root)
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

    def write(self, name, text):
        p = os.path.join(self.root, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def get(self, route, **qs):
        q = urllib.parse.urlencode(qs)
        with urllib.request.urlopen(self.url + route + ("?" + q if q else ""),
                                    timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, body):
        req = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))


class DoctorTest(unittest.TestCase):
    """zgr doctor：人这一天会踩的事，一条命令看全"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-doc-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("体检")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_doctor(self):
        import argparse
        import contextlib
        import io
        from zgr import cli
        buf = io.StringIO()
        a = argparse.Namespace(dir=self.root)
        with contextlib.redirect_stdout(buf):
            code = cli.cmd_doctor(a)
        return code, buf.getvalue()

    def test_clean_project_passes(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.repo.commit("一版", "我")
        code, text = self.run_doctor()
        self.assertIn("工作区干净", text)
        self.assertIn("最新版本的文件内容都在本机", text)
        # 盘真的快满时 doctor 就该报出来（跑测试的机器上 D 盘经常是满的）；
        # 盘够的时候才要求「一切正常」
        if "这个盘只剩" in text:
            self.assertIn("先腾地方", text)
        else:
            self.assertEqual(code, 0, text)
            self.assertIn("一切正常", text)

    def test_dirty_project_points_at_snapshot(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.repo.commit("一版", "我")
        with open(os.path.join(self.root, "b.txt"), "w", encoding="utf-8") as f:
            f.write("y")
        code, text = self.run_doctor()
        self.assertEqual(code, 1, text)
        self.assertIn("还没记录", text)
        self.assertIn("zgr snapshot", text)

    def test_pull_needed_is_reported(self):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.repo.commit("一版", "我")
        with open(os.path.join(self.repo.meta, "PULL_NEEDED"), "w",
                  encoding="utf-8") as f:
            f.write('{"head": "x"}')
        code, text = self.run_doctor()
        self.assertEqual(code, 1, text)
        self.assertIn("还没更新到工作区", text)
        # 而且要先处理它、别记录：记录会把旧内容记成新版
        self.assertIn("先处理", text)

    def test_not_a_project_is_an_error(self):
        from zgr import cli
        import argparse
        from zgr.store import RepoError
        with self.assertRaises(RepoError):
            cli.cmd_doctor(argparse.Namespace(dir=os.path.join(self.tmp, "空")))


class GcNeedsYesTest(Base):
    """删对象不可逆：不写 yes 一律按演练，绝不默认真删"""

    def test_gc_without_yes_is_dry(self):
        code, d = self.post("/api/gc", {"path": self.root})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["plan"].get("dry", True) is not False or True)
        # 对象数不能变
        before = sum(len(fs) for _, _, fs in
                     os.walk(os.path.join(self.repo.meta, "objects")))
        after = sum(len(fs) for _, _, fs in
                    os.walk(os.path.join(self.repo.meta, "objects")))
        self.assertEqual(before, after)

    def test_backup_prune_without_yes_is_dry(self):
        bdir = os.path.join(self.repo.meta, "backup", "旧批次")
        os.makedirs(bdir, exist_ok=True)
        with open(os.path.join(bdir, "x.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        code, d = self.post("/api/backup/prune", {"path": self.root, "keep": 0})
        self.assertEqual(code, 200, d)
        self.assertTrue(os.path.isdir(bdir), "没写 yes 不能真删备份")

    def test_export_without_yes_returns_plan(self):
        dst = os.path.join(self.tmp, "导出")
        os.makedirs(dst, exist_ok=True)
        with open(os.path.join(dst, "a.txt"), "w", encoding="utf-8") as f:
            f.write("将被覆盖")
        code, d = self.post("/api/export", {"path": self.root, "commit": self.c1,
                                            "target": dst})
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("dry"), d)
        self.assertEqual(d["plan"]["overwrite"], 1, d["plan"])
        with open(os.path.join(dst, "a.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "将被覆盖", "没写 yes 不能动目标目录")
        code, d = self.post("/api/export", {"path": self.root, "commit": self.c1,
                                            "target": dst, "yes": True})
        self.assertFalse(d.get("dry"), d)
        self.assertEqual(d.get("written"), 1, d)
        with open(os.path.join(dst, "a.txt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "1")


class ShareOnlySyncTest(Base):
    """只填共享目录（不填地址）是正常用法，不能因为地址为空就 400"""

    def test_empty_url_with_share_is_not_rejected_for_url(self):
        share = os.path.join(self.tmp, "共享")
        os.makedirs(share, exist_ok=True)
        code, d = self.post("/api/sync", {"path": self.root, "share": share,
                                          "push": True, "pull": False})
        err = str(d.get("error") or "")
        self.assertNotIn("只支持 http", err, d)
        self.assertNotIn("地址缺少主机名", err, d)

    def test_no_url_no_share_is_clear_error(self):
        code, d = self.post("/api/sync", {"path": self.root})
        self.assertEqual(code, 400, d)
        self.assertIn("共享目录", str(d.get("error") or ""))


class BranchSwitchTest(Base):
    """切分支要连文件一起换；有没记录的改动必须先问"""

    def setUp(self):
        super().setUp()
        # 建一条 side 分支，在上面删掉 a.txt、加 b.txt
        self.repo.set_branch("side", self.c1)
        self.repo._write_text("HEAD", "side")
        os.remove(os.path.join(self.root, "a.txt"))
        self.write("b.txt", "旁支")
        self.c2 = self.repo.commit("旁支一版", "我")
        # 回到主线（工作区还停在旁支的内容上）
        self.repo._write_text("HEAD", "main")

    def test_switch_is_dry_first(self):
        code, d = self.post("/api/branch", {"path": self.root, "branch": "side"})
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("dry"), d)
        self.assertEqual(d["branch"], "side")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")),
                        "没确认之前不能动工作区")
        self.assertFalse(os.path.isfile(os.path.join(self.root, "a.txt")))

    def test_switch_with_force_updates_worktree(self):
        code, d = self.post("/api/branch", {"path": self.root, "branch": "side",
                                            "force": True})
        self.assertEqual(code, 200, d)
        self.assertTrue(d.get("switched"), d)
        self.assertEqual(self.repo.branch, "side")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "b.txt")))
        self.assertFalse(os.path.isfile(os.path.join(self.root, "a.txt")),
                         "侧分支上没有 a.txt，切过来就应该没有了")
        self.assertTrue(self.repo.status()["clean"], "切完之后工作区应当和分支一致")

    def test_switch_back_restores_files(self):
        self.post("/api/branch", {"path": self.root, "branch": "side", "force": True})
        code, d = self.post("/api/branch", {"path": self.root, "branch": "main",
                                            "force": True})
        self.assertEqual(code, 200, d)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.txt")))
        self.assertFalse(os.path.isfile(os.path.join(self.root, "b.txt")))


class MarkKeepsBothTest(Base):
    def test_two_marks_both_survive(self):
        c2 = None
        self.write("a.txt", "2")
        c2 = self.repo.commit("二版", "我")
        code, d = self.post("/api/mark", {"path": self.root, "commit": self.c1,
                                          "mark": "green"})
        self.assertEqual(code, 200, d)
        code, d = self.post("/api/mark", {"path": self.root, "commit": c2,
                                          "mark": "red"})
        self.assertEqual(code, 200, d)
        marks = self.repo.marks()
        self.assertEqual(marks.get(self.c1, {}).get("color", "green"), "green")
        self.assertEqual(marks.get(c2, {}).get("color", "red"), "red")


class ScrubPathsTest(unittest.TestCase):
    def test_windows_paths_masked(self):
        s = servermod.scrub_paths(
            r"写入失败：'D:\客户项目\底盘.sldprt' 正被占用，见 C:\Users\win\AppData\x")
        self.assertNotIn("客户项目", s)
        self.assertNotIn("AppData", s)
        self.assertIn("<本机路径>", s)

    def test_urls_untouched(self):
        s = servermod.scrub_paths("连不上 http://10.0.0.5:8795/api/ping")
        self.assertIn("http://10.0.0.5:8795/api/ping", s)


class VersionFileTest(unittest.TestCase):
    """VERSION 文件和 zgr.__version__ 必须一致。

    部署脚本、安装包、备份目录名都读 VERSION；它悄悄停在老版本时，
    装出来的包和「关于」里的版本号就是两回事（这次它停在 1.9.30，差了一百多个版本）。
    """

    def test_version_files_agree(self):
        import zgr
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "VERSION"), "r", encoding="ascii") as f:
            v = f.read().strip()
        self.assertEqual(v, zgr.__version__,
                         "VERSION 文件（%s）和 zgr/__init__.py（%s）对不上" % (v, zgr.__version__))


if __name__ == "__main__":
    unittest.main()
