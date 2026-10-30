# -*- coding: utf-8 -*-
"""数据安全：恢复前的体检、失败回滚、特殊路径、空项目状态、接口健壮性

每一条都对应一种「可能把用户图纸搞丢或搞坏」的真实场景。原则只有一句：
宁可一步都不做，也不能做到一半把工作区变成半新半旧。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


def read(p):
    with open(p, "rb") as f:
        return f.read()


class SafetyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-safe-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("安全测试")

    def tearDown(self):
        for dp, dns, fns in os.walk(self.tmp):
            for fn in fns:
                try:
                    os.chmod(os.path.join(dp, fn), 0o666)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def snap(self):
        """工作区快照：相对路径 -> 内容"""
        out = {}
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d != ".zgr"]
            for fn in fns:
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, self.root).replace("\\", "/")
                out[rel] = read(p)
        return out


class RestoreBackupTest(SafetyBase):
    def test_overwritten_and_removed_files_are_backed_up(self):
        self.write("a.txt", "第一版")
        self.write("b.txt", "会被删掉")
        v1 = self.repo.commit("一")
        self.write("a.txt", "第二版")
        os.remove(os.path.join(self.root, "b.txt"))
        self.write("c.txt", "后加的")
        self.repo.commit("二")

        res = self.repo.restore(v1)
        # 回到第一版：a 是旧内容，b 回来了，c 被删掉
        self.assertEqual(read(os.path.join(self.root, "a.txt")).decode(), "第一版")
        self.assertEqual(read(os.path.join(self.root, "b.txt")).decode(), "会被删掉")
        self.assertFalse(os.path.exists(os.path.join(self.root, "c.txt")))

        # 被覆盖和被删除的都要在备份里留一份
        bak = res["backup"]
        self.assertTrue(os.path.isdir(bak))
        self.assertEqual(read(os.path.join(bak, "a.txt")).decode(), "第二版")
        self.assertEqual(read(os.path.join(bak, "c.txt")).decode(), "后加的")

    def test_ignored_temp_files_never_deleted(self):
        """SolidWorks 的 ~$ 锁文件、.zgr 目录绝不能被镜像恢复删掉"""
        self.write("a.txt", "一")
        v1 = self.repo.commit("一")
        self.write("~$a.sldprt", "锁文件")
        self.write(".zgr/自定义.txt", "仓库自己的东西")
        self.write("b.txt", "新增的")
        self.repo.restore(v1)
        self.assertTrue(os.path.exists(os.path.join(self.root, "~$a.sldprt")))
        self.assertTrue(os.path.exists(os.path.join(self.root, ".zgr", "自定义.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "b.txt")))


class RestorePreflightTest(SafetyBase):
    def test_readonly_file_aborts_before_touching_anything(self):
        """有文件只读/被占用时，一个字节都不许先写出去"""
        self.write("a.txt", "旧")
        self.write("z.txt", "最后一个")
        v1 = self.repo.commit("一")
        self.write("a.txt", "新")
        self.write("z.txt", "新的最后一个")
        self.repo.commit("二")

        ro = os.path.join(self.root, "z.txt")
        os.chmod(ro, 0o444)
        before = self.snap()
        with self.assertRaises(RepoError) as e:
            self.repo.restore(v1)
        msg = str(e.exception)
        self.assertTrue("只读" in msg or "占用" in msg, msg)
        self.assertEqual(self.snap(), before, "体检没过就不能改动工作区")

    def test_not_enough_disk_refuses(self):
        self.write("a.txt", "一点内容" * 1000)
        v1 = self.repo.commit("一")
        self.write("a.txt", "另一版" * 1000)
        self.repo.commit("二")
        before = self.snap()
        fake = shutil.disk_usage(self.root).__class__(
            total=10 ** 9, used=10 ** 9 - 1024, free=1024)
        with mock.patch.object(shutil, "disk_usage", return_value=fake):
            with self.assertRaises(RepoError) as e:
                self.repo.restore(v1)
        self.assertIn("空间", str(e.exception))
        self.assertEqual(self.snap(), before)


class RestoreRollbackTest(SafetyBase):
    def test_failure_midway_rolls_back(self):
        """写到一半出错必须自己退回原样，不能留半新半旧的工作区"""
        self.write("a.txt", "旧A")
        self.write("b.txt", "旧B")
        self.write("c.txt", "旧C")
        v1 = self.repo.commit("一")
        for n in ("a", "b", "c"):
            self.write(n + ".txt", "新" + n.upper())
        self.repo.commit("二")

        before = self.snap()
        real = Repo.blob_to_file
        calls = {"n": 0}

        def flaky(self_, oid, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "模拟磁盘错误")
            return real(self_, oid, dst)

        with mock.patch.object(Repo, "blob_to_file", flaky):
            with self.assertRaises(RepoError) as e:
                self.repo.restore(v1)
        self.assertIn("回滚", str(e.exception))
        self.assertEqual(self.snap(), before, "出错后工作区必须回到原样")


class PathSafetyTest(SafetyBase):
    def test_unicode_deep_and_spaces(self):
        names = [
            "模具3D/9-16（2024版）/拆板/YHD-XC0004-总装配/WS4D-YHD-XC001拆板/NID01.SLDPRT",
            "有 空格/带#号/a&b.txt",
            "很深的目录/一/二/三/四/五/六/七/八/文件.txt",
        ]
        for i, n in enumerate(names):
            self.write(n, "内容%d" % i)
        v1 = self.repo.commit("中文长路径")
        for i, n in enumerate(names):
            self.write(n, "改过%d" % i)
        self.repo.commit("二")
        self.repo.restore(v1)
        for i, n in enumerate(names):
            p = os.path.join(self.root, n.replace("/", os.sep))
            self.assertEqual(read(p).decode("utf-8"), "内容%d" % i)

    def test_big_directory_commit_and_restore(self):
        """1000 个小文件：提交+恢复要能在可接受时间内完成，且内容一字不差"""
        for i in range(1000):
            self.write("d%02d/f%04d.sldprt" % (i % 20, i), "零件%d" % i)
        t0 = time.time()
        v1 = self.repo.commit("一千个")
        for i in range(0, 1000, 7):
            self.write("d%02d/f%04d.sldprt" % (i % 20, i), "改过%d" % i)
        self.repo.commit("改一批")
        self.repo.restore(v1)
        took = time.time() - t0
        self.assertEqual(read(os.path.join(self.root, "d00", "f0000.sldprt")).decode(),
                         "零件0")
        self.assertLess(took, 60, "1000 个文件的提交+恢复用了 %.1f 秒，太慢了" % took)


class _ApiBase(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-safe-api-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        Repo(self.root).init("接口测试")
        self.reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(self.reg)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, route):
        with urllib.request.urlopen(self.base + route, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_raw(self, route, data, ctype="application/json"):
        req = urllib.request.Request(self.base + route, data=data,
                                     headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")


class EmptyStateTest(_ApiBase):
    def test_starts_with_zero_projects(self):
        d = self.get("/api/projects")
        self.assertEqual(d["projects"], [])

    def test_add_then_remove_leaves_nothing(self):
        body = json.dumps({"path": self.root, "name": "新项目"}).encode("utf-8")
        self.post_raw("/api/add", body)
        self.assertEqual(len(self.get("/api/projects")["projects"]), 1)
        self.post_raw("/api/remove", json.dumps({"path": self.root}).encode("utf-8"))
        self.assertEqual(self.get("/api/projects")["projects"], [])
        # 移除项目绝不删工作区文件
        self.assertTrue(os.path.isdir(self.root))
        self.assertTrue(os.path.isdir(os.path.join(self.root, ".zgr")))


class ApiRobustnessTest(_ApiBase):
    def test_bad_json_is_400(self):
        code, body = self.post_raw("/api/add", "{这不是json".encode("utf-8"))
        self.assertEqual(code, 400, body)

    def test_oversized_body_rejected(self):
        import zgr.server as srv
        old = srv.MAX_BODY
        srv.MAX_BODY = 1024
        try:
            code, body = self.post_raw("/api/add", b"x" * 4096)
        finally:
            srv.MAX_BODY = old
        self.assertIn(code, (400, 413), body)

    def test_form_post_rejected(self):
        code, body = self.post_raw("/api/add", b"path=x", ctype="text/plain")
        self.assertEqual(code, 415, body)

    def test_unknown_route_404(self):
        code, _ = self.post_raw("/api/no_such_route", b"{}")
        self.assertEqual(code, 404)

    def test_traversal_rejected(self):
        for bad in ("../../windows/system32/evil.txt", "C:/Windows/evil.txt",
                    "a/../../b.txt", ".zgr/config.json"):
            code, body = self.post_raw("/api/diff", json.dumps(
                {"path": self.root, "file": bad}).encode("utf-8"))
            self.assertIn(code, (400, 404), "%s 应该被拒绝：%s" % (bad, body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
