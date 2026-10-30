# -*- coding: utf-8 -*-
"""预览图 / 网格缓存的总量清理

这三样都能重新生成，丢了不心疼；但它们长在没人看的地方，一个月能堆到几 GB。
规则和回收站一致：只删自己命名的文件、最近 10 分钟的不动、从最旧的删起、
删不掉如实回报、`dry` 只算不删。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import cache                          # noqa: E402
from zgr.store import Repo                     # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-cache-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("缓存")
        self.pv = os.path.join(self.repo.meta, "previews", "ab")
        self.ms = os.path.join(self.repo.meta, "meshes")
        os.makedirs(self.pv, exist_ok=True)
        os.makedirs(self.ms, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, d, name, kb, age=3600):
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * (kb * 1024))
        old = time.time() - age
        os.utime(p, (old, old))
        return p


class PruneDirTest(Base):
    def test_drop_oldest_until_under_cap(self):
        for i in range(10):
            self.put(self.pv, "%02d.png" % i, 100, age=3600 - i * 60)  # 越往后越新
        res = cache.prune_dir(self.pv, 500 * 1024)
        self.assertLessEqual(cache.total_bytes(self.pv), 500 * 1024)
        self.assertEqual(len(res["removed"]), 5)
        left = sorted(os.listdir(self.pv))
        self.assertEqual(left, ["05.png", "06.png", "07.png", "08.png", "09.png"],
                         "留下的必须是最新的那几批")

    def test_fresh_files_are_protected(self):
        self.put(self.pv, "old.png", 100, age=3600)
        self.put(self.pv, "now.png", 100, age=0)      # 10 分钟内：可能正在写
        cache.prune_dir(self.pv, 1)
        self.assertEqual(os.listdir(self.pv), ["now.png"])

    def test_dry_run_deletes_nothing(self):
        for i in range(4):
            self.put(self.pv, "%d.png" % i, 100)
        before = sorted(os.listdir(self.pv))
        res = cache.prune_dir(self.pv, 1, dry=True)
        self.assertEqual(sorted(os.listdir(self.pv)), before)
        self.assertEqual(len(res["removed"]), 4)
        self.assertTrue(res["dry"])

    def test_foreign_files_untouched(self):
        self.put(self.pv, "note.txt", 100)            # 不是我们的扩展名
        self.put(self.pv, "x.png", 100)
        cache.prune_dir(self.pv, 1)
        self.assertTrue(os.path.isfile(os.path.join(self.pv, "note.txt")))
        self.assertFalse(os.path.isfile(os.path.join(self.pv, "x.png")))

    def test_zero_cap_means_unlimited(self):
        self.put(self.pv, "a.png", 100)
        res = cache.prune_dir(self.pv, 0)
        self.assertEqual(res["removed"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.pv, "a.png")))

    def test_missing_dir_is_not_an_error(self):
        res = cache.prune_dir(os.path.join(self.tmp, "没有这个目录"), 1024)
        self.assertEqual(res["removed"], [])


class PruneRepoTest(Base):
    def test_uses_config(self):
        for i in range(6):
            self.put(self.pv, "%d.png" % i, 200)
        for i in range(3):
            self.put(self.ms, "%s.zgm" % (("ab%d" % i) * 32)[:64], 400)
        cfg = self.repo.config
        cfg.update({"cache_max_mb": 1, "mesh_max_mb": 1})
        self.repo._write_json("config.json", cfg)
        res = cache.prune_repo(self.repo)
        self.assertTrue(res["previews"]["removed"])
        self.assertTrue(res["meshes"]["removed"])

    def test_auto_off(self):
        self.put(self.pv, "a.png", 500)
        cfg = self.repo.config
        cfg.update({"cache_max_mb": 1, "cache_auto": False})
        self.repo._write_json("config.json", cfg)
        res = cache.prune_repo(self.repo)
        self.assertIsNone(res["previews"])
        self.assertTrue(os.path.isfile(os.path.join(self.pv, "a.png")))

    def test_bad_config_falls_back(self):
        self.put(self.pv, "a.png", 100)
        cfg = self.repo.config
        cfg.update({"cache_max_mb": "很多"})
        self.repo._write_json("config.json", cfg)
        cache.prune_repo(self.repo)                    # 不许抛异常


class OrphanMeshTest(Base):
    def test_finds_meshes_of_vanished_objects(self):
        dead = "c" * 64
        alive = "d" * 64
        self.put(self.ms, dead + ".zgm", 10)
        self.put(self.ms, alive + ".zgm", 10)
        self.put(self.ms, alive + ".json", 1)
        found = cache.orphans(self.repo, {alive})
        self.assertEqual(len(found), 1)
        self.assertIn(dead, found[0])
        # 清理后只留下仍然被引用的那个
        for p in found:
            os.remove(p)
        self.assertEqual(sorted(os.listdir(self.ms)), [alive + ".json", alive + ".zgm"])


class SizeReportTest(Base):
    def test_meshes_counted_separately(self):
        self.put(self.ms, ("e" * 64) + ".zgm", 300)
        rep = self.repo.size_report()
        self.assertGreaterEqual(rep["meshes"]["bytes"], 300 * 1024)
        # 网格不能被重复算进「其它」里
        self.assertLess(rep["meta_other"]["bytes"],
                        self.repo._dir_bytes(self.repo.meta) - 300 * 1024 + 1)


class ApiTest(Base):
    """界面要能看到策略、能一键清到上限"""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.csv = os.path.join(self.tmp, "projects.json")
        self.app = App(Registry(path=self.csv))
        self.app.registry.add(self.root, "缓存")
        handler = type("HC", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        import json
        import urllib.parse
        import urllib.request
        url = self.base + path
        sep = "&" if "?" in url else "?"
        with urllib.request.urlopen(url + sep + urllib.parse.urlencode(
                {"path": self.root}), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path, body):
        import json
        import urllib.request
        data = json.dumps({"path": self.root, **body}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_cache_report_and_prune(self):
        for i in range(4):
            self.put(self.pv, "%d.png" % i, 400)      # 共 1.6 MB，上限 1 MB
        self.put(self.ms, ("f" * 64) + ".zgm", 400)
        cfg = self.repo.config
        cfg.update({"cache_max_mb": 1})
        self.repo._write_json("config.json", cfg)

        d = self.get("/api/cache")
        self.assertGreater(d["previews"]["bytes"], 0)
        self.assertGreater(d["meshes"]["bytes"], 0)
        self.assertEqual(d["preview_max_mb"], 1)

        # 先演练：一个也不删
        r = self.post("/api/cache/prune", {"dry": True})
        self.assertTrue(r["dry"])
        self.assertGreater(r["previews"]["removed"], 0)
        self.assertEqual(len(os.listdir(self.pv)), 4)

        r = self.post("/api/cache/prune", {})
        self.assertFalse(r["dry"])
        self.assertLessEqual(cache.total_bytes(self.pv), 1024 * 1024)

    def test_size_endpoint_carries_policy(self):
        cfg = self.repo.config
        cfg.update({"cache_max_mb": 512, "mesh_max_mb": 256})
        self.repo._write_json("config.json", cfg)
        d = self.get("/api/size")
        self.assertEqual(d["cache"]["preview_max_mb"], 512)
        self.assertEqual(d["cache"]["mesh_max_mb"], 256)
        self.assertIn("meshes", d["report"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
