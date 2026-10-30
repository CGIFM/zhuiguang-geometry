# -*- coding: utf-8 -*-
"""压力与一致性：并发请求、并发提交、双机大批量同步、大文件、三千文件扫描

判定标准只有一条：**任何情况下都不许丢文件、不许把仓库弄坏**。
慢一点可以接受，坏了就是事故。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import sync as sync_mod  # noqa: E402
from zgr.store import Repo  # noqa: E402


def _Handler():
    from zgr.server import Handler
    return Handler


class _Server:
    def __init__(self, tmp, name):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tmp
        self.name = name
        reg = Registry(path=os.path.join(tmp, "projects-%s.json" % name))
        self.app = App(reg)
        handler = type("H", (_Handler(),), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def write_file(root, rel, data):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(p, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)
    return p


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HttpLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-load-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        Repo(self.root).init("压测")
        self.srv = _Server(self.tmp, "A")

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parallel_reads_never_500(self):
        bad = []
        lock = threading.Lock()

        def worker(n):
            for _ in range(10):
                for route in ("/api/ping", "/api/projects"):
                    try:
                        with urllib.request.urlopen(self.srv.url + route, timeout=15) as r:
                            if r.status != 200:
                                with lock:
                                    bad.append((route, r.status))
                            r.read()
                    except Exception as e:  # noqa: BLE001
                        with lock:
                            bad.append((route, repr(e)))

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(120)
        self.assertEqual(bad, [], "并发读请求出错：%s" % bad[:5])

    def test_parallel_commits_keep_repo_valid(self):
        """8 个线程同时提交：允许互相覆盖 head，但仓库必须始终可读、可恢复"""
        for i in range(8):
            write_file(self.root, "f%d.txt" % i, "内容%d" % i)
        repo = Repo(self.root)
        errs = []

        def worker():
            try:
                repo.commit("并发提交")
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

        ths = [threading.Thread(target=worker) for _ in range(8)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(120)
        self.assertEqual(errs, [], "并发提交报错：%s" % errs[:3])

        head = Repo(self.root).head_oid()
        self.assertTrue(head, "提交之后必须有 head")
        _info, files = Repo(self.root).files_of(head)
        self.assertEqual(len(files), 8, "所有文件都要在这个版本里")
        # head 必须能恢复出可用工作区
        res = Repo(self.root).restore(head, backup=False)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "f0.txt")))
        self.assertGreaterEqual(res["written"], 0)


class TwoMachineStressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-stress2-")
        self.a = _Server(self.tmp, "A")
        self.b = _Server(self.tmp, "B")
        self.ra = Repo(os.path.join(self.tmp, "机A", "项目")).init("跨机")
        self.rb = Repo(os.path.join(self.tmp, "机B", "项目")).init("跨机")
        self.a.app.registry.add(self.ra.root, "跨机")
        self.b.app.registry.add(self.rb.root, "跨机")

    def tearDown(self):
        self.a.close()
        self.b.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sync(self, repo, url, **kw):
        return sync_mod.sync(repo, base=url, log=lambda *a: None, **kw)

    def test_300_files_pull_matches_byte_for_byte(self):
        """A 有 300 个零件，B 拉过来：每个文件都要逐字节一致"""
        names = []
        for i in range(300):
            rel = "零件/组%02d/件%03d.sldprt" % (i % 12, i)
            write_file(self.ra.root, rel, ("零件内容 %d " % i) * 20)
            names.append(rel)
        head = self.ra.commit("三百个文件")

        res = self.sync(self.rb, self.a.url, push=False, pull=True, checkout=True)
        self.assertTrue(res["checked_out"])
        self.assertEqual(self.rb.head_oid(), head)

        for rel in names:
            pa = os.path.join(self.ra.root, rel.replace("/", os.sep))
            pb = os.path.join(self.rb.root, rel.replace("/", os.sep))
            self.assertTrue(os.path.isfile(pb), "少了文件：" + rel)
            self.assertEqual(sha(pa), sha(pb), "内容不一致：" + rel)

        # 反向：B 改一个文件推回 A，再让 A 拉回来对比
        write_file(self.rb.root, names[0], "B 改过的内容")
        self.rb.commit("B 改一件")
        self.sync(self.rb, self.a.url, push=True, pull=False)
        self.sync(self.ra, self.b.url, push=False, pull=True, checkout=True)
        # 用 with 读：句柄不关，Windows 上后面 tearDown 删目录会静默失败
        with open(os.path.join(self.ra.root, names[0].replace("/", os.sep)),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "B 改过的内容")

    def test_interrupted_fetch_does_not_move_branch(self):
        """下载到一半断线：分支指针绝不能动，工作区也不能被改"""
        write_file(self.ra.root, "a.txt", "一")
        self.ra.commit("一")
        self.sync(self.rb, self.a.url, push=False, pull=True, checkout=True)
        before_head = self.rb.head_oid()
        before_files = sorted(os.listdir(self.rb.root))

        for i in range(20):
            write_file(self.ra.root, "b%02d.txt" % i, "新内容%d" % i)
        self.ra.commit("二")

        calls = {"n": 0}
        real = sync_mod.HttpBackend.fetch

        def flaky(self_, repo, oids, log=print, progress=None):
            calls["n"] += 1
            raise sync_mod.SyncError("模拟断网")

        with mock.patch.object(sync_mod.HttpBackend, "fetch", flaky):
            with self.assertRaises(sync_mod.SyncError):
                self.sync(self.rb, self.a.url, push=False, pull=True, checkout=True)
        self.assertGreater(calls["n"], 0, "这条路应该真的去下载了")

        self.assertEqual(self.rb.head_oid(), before_head, "断线后分支指针不许动")
        self.assertEqual(sorted(os.listdir(self.rb.root)), before_files,
                         "断线后工作区不许改动")


class BigFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-big-")
        self.repo = Repo(os.path.join(self.tmp, "项目")).init("大文件")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_20mb_roundtrip(self):
        blob = os.urandom(20 << 20)
        p = write_file(self.repo.root, "大件/底板.sldprt", blob)
        want = sha(p)
        v1 = self.repo.commit("大文件")
        write_file(self.repo.root, "大件/底板.sldprt", "换个内容".encode("utf-8"))
        self.repo.commit("改了")
        self.repo.restore(v1, backup=False)
        self.assertEqual(sha(p), want, "20MB 文件恢复后内容必须一模一样")


class ScanPerfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-scan-")
        self.repo = Repo(os.path.join(self.tmp, "项目")).init("三千件")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_3000_files_scan_commit_restore(self):
        for i in range(3000):
            write_file(self.repo.root, "层%02d/文件%04d.sldprt" % (i % 40, i),
                       "零件 %d" % i)
        t0 = time.time()
        self.repo.status()
        t_status = time.time() - t0
        t0 = time.time()
        head = self.repo.commit("三千件")
        t_commit = time.time() - t0
        write_file(self.repo.root, "层00/文件0000.sldprt", "改过")
        t0 = time.time()
        self.repo.restore(head)
        t_restore = time.time() - t0
        with open(os.path.join(self.repo.root, "层00", "文件0000.sldprt"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "零件 0")
        # 阈值给得很宽松：只用来抓「退化成 O(n²) 或者卡死」这种病
        self.assertLess(t_status, 30, "扫描 3000 文件用了 %.1fs" % t_status)
        self.assertLess(t_commit, 60, "提交 3000 文件用了 %.1fs" % t_commit)
        self.assertLess(t_restore, 60, "恢复 3000 文件用了 %.1fs" % t_restore)


if __name__ == "__main__":
    unittest.main(verbosity=2)
