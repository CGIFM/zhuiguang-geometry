# -*- coding: utf-8 -*-
"""第 96 轮：先量「打开项目」这一整段，再看缓存到底有没有吃到

前两轮一直在抠单个函数。可用户感受到的是**打开一个项目**那一下：
项目列表 → 时间线（分支树 + 改动数）→ 状态 → 缩略图清单，四步串起来。
所以这轮给 `tools/bench.py` 加了首屏段，并把对象缓存的命中率报出来
（`/api/ping` 只给本机看、体检里也有一行）——两台真机上不用猜「是不是缓存没吃到」。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import store as store_mod                 # noqa: E402
from zgr.store import Repo, obj_cache_clear       # noqa: E402


class CacheStatTest(unittest.TestCase):
    def setUp(self):
        obj_cache_clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-stat96-")
        self.repo = Repo(self.tmp).init("统计")
        with open(os.path.join(self.tmp, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hi")
        self.repo.commit("v1", "测试")
        self.oid = self.repo.head_oid()

    def tearDown(self):
        obj_cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hits_and_misses_counted(self):
        self.repo.get(self.oid)                 # 第一次：未命中
        self.repo.get(self.oid)                 # 第二次：命中
        self.repo.get(self.oid)
        st = store_mod.obj_cache_stats()
        self.assertEqual(st["misses"], 1)
        self.assertEqual(st["hits"], 2)

    def test_disk_change_counts_as_miss(self):
        """盘上那份被换掉：这一次既不算命中，也要把旧副本扔掉"""
        self.repo.get(self.oid)
        p = self.repo.object_path(self.oid)
        st0 = os.stat(p)
        os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns + 1_000_000))
        self.repo.get(self.oid)
        st = store_mod.obj_cache_stats()
        self.assertEqual(st["hits"], 0)
        self.assertGreaterEqual(st["misses"], 2)

    def test_clear_resets_counters(self):
        self.repo.get(self.oid)
        self.repo.get(self.oid)
        obj_cache_clear()
        st = store_mod.obj_cache_stats()
        self.assertEqual((st["hits"], st["misses"], st["evicted"], st["items"]),
                         (0, 0, 0, 0))


class TreeNameGuardTest(unittest.TestCase):
    """树里出现的名字必须挡在门口（这轮把逐字符检查换成正则，答案不能变）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-tree96-")
        self.repo = Repo(self.tmp).init("校验")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_with(self, entries):
        import json
        tree = self.repo.put("tree", json.dumps(entries).encode("utf-8"))
        commit = self.repo.put("commit", json.dumps(
            {"tree": tree, "parents": [], "time": 0, "message": "坏树",
             "author": "测试"}).encode("utf-8"))
        return commit

    def test_rejects_traversal_and_control_chars(self):
        from zgr.store import RepoError
        for bad in ("../外面.txt", "a\\b.txt", "a:b.txt", "a*b.txt", "a\x01b.txt",
                    "a|b.txt", "", "..", "."):
            c = self._commit_with([{"n": bad, "t": "b", "h": "0" * 64, "s": 1}])
            with self.assertRaises(RepoError, msg=repr(bad)):
                self.repo.files_of(c)

    def test_accepts_normal_names(self):
        c = self._commit_with([{"n": "底板.SLDPRT", "t": "b", "h": "0" * 64, "s": 1},
                               {"n": "零件 01", "t": "b", "h": "1" * 64, "s": 2}])
        _info, files = self.repo.files_of(c)
        self.assertEqual(len(files), 2)
        self.assertIn("零件 01", files)


class PingCacheTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        obj_cache_clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-ping96-")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        for w in list(self.app.watchers.values()):
            w.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        obj_cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ping(self):
        with urllib.request.urlopen(self.base + "/api/ping", timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_ping_reports_cache_for_local(self):
        d = self.ping()
        self.assertIn("cache", d)
        self.assertEqual(set(["hits", "misses", "evicted", "items", "bytes"])
                         - set(d["cache"]), set())
        self.assertIn("max_bytes", d["cache"])

    def test_ping_still_has_basics(self):
        d = self.ping()
        for k in ("ok", "app", "version", "host", "urls"):
            self.assertIn(k, d)


class DoctorCacheLineTest(unittest.TestCase):
    def setUp(self):
        obj_cache_clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-doc96-")
        self.repo = Repo(self.tmp).init("体检")
        with open(os.path.join(self.tmp, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hi")
        self.repo.commit("v1", "测试")

    def tearDown(self):
        obj_cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_line_appears_after_enough_reads(self):
        from zgr import doctor
        oid = self.repo.head_oid()
        for _ in range(60):
            self.repo.get(oid)
        msgs = [g["msg"] for g in doctor.report(self.repo)["good"]]
        self.assertTrue(any("对象缓存命中" in m for m in msgs), msgs)

    def test_line_hidden_when_too_few_reads(self):
        """刚起的服务（几乎没读过对象）不该在体检里刷一行没意义的东西"""
        from zgr import doctor
        msgs = [g["msg"] for g in doctor.report(self.repo)["good"]]
        self.assertFalse(any("对象缓存命中" in m for m in msgs), msgs)


if __name__ == "__main__":
    unittest.main()
