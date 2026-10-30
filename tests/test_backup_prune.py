# -*- coding: utf-8 -*-
"""回收站自动瘦身：按体积上限删最旧的，硬底线是「最近 min_keep 批永远不动」

为什么要有这条：备份是给人反悔用的，不是历史存档。167/174 上真实情况是
几天就堆了 25 / 38 批、合计 1.8 GB —— 一次整项目恢复的备份本身就可能几 GB。
盘满的时候连一次记录都做不成，那才是真危险。

但删备份是**会让人丢掉反悔机会**的操作，所以规则要保守：
- 最近 min_keep 批无论多大都不动；
- 只有「超过 max_bytes」时才动手，从最旧的往前删，删到不超为止；
- 删不掉的（被占用）跳过，不谎报。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-prune-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("清理备份")
        with open(os.path.join(self.root, "件.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.repo.commit("一")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk(self, idx, kb):
        """造一批备份：名字按我们的规则，大小可控；名字越早的时间越早"""
        name = "2026100%d-1200%02d-1" % (0, idx)
        p = self.repo._p("backup", name)
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "旧文件.txt"), "wb") as f:
            f.write(b"x" * (kb * 1024))
        old = time.time() - (1000 - idx) * 60      # idx 越大越新
        os.utime(p, (old, old))
        return name

    def names(self):
        return self.repo.backup_dirs()


class PruneBySizeTest(Base):
    def test_keep_recent_even_if_oversize(self):
        for i in range(12):
            self.mk(i, 100)                        # 12 批 × 100KB = 1.2MB
        before = self.repo.backup_dirs()
        res = self.repo.prune_backups(keep=40, max_bytes=100 * 1024, min_keep=10)
        left = self.repo.backup_dirs()
        self.assertEqual(len(left), 10, "min_keep=10：再超也留 10 批（%d）" % len(left))
        self.assertEqual(len(res["removed"]), 2)
        self.assertEqual(left, before[:10], "留下的是最近 10 批")

    def test_drop_oldest_until_under_cap(self):
        for i in range(12):
            self.mk(i, 100)
        cap = 500 * 1024
        res = self.repo.prune_backups(keep=40, max_bytes=cap, min_keep=1)
        total = self.repo.backup_total_bytes()
        self.assertLessEqual(total, cap, "删到不超上限")
        self.assertTrue(res["removed"], "确实删了")
        self.assertEqual(len(self.repo.backup_dirs()), 5)
        # 留下的必须是最新的那几批
        self.assertEqual(self.repo.backup_dirs()[0], self.names()[0])

    def test_no_cap_only_counts(self):
        for i in range(6):
            self.mk(i, 10)
        res = self.repo.prune_backups(keep=3, max_bytes=None)
        self.assertEqual(len(self.repo.backup_dirs()), 3)
        self.assertEqual(res["kept"], 3)

    def test_dry_run_deletes_nothing(self):
        for i in range(6):
            self.mk(i, 10)
        before = self.repo.backup_dirs()
        res = self.repo.prune_backups(keep=2, dry=True)
        self.assertEqual(self.repo.backup_dirs(), before)
        self.assertEqual(len(res["removed"]), 4)
        self.assertTrue(res["dry"])

    def test_never_touches_foreign_dirs(self):
        self.mk(0, 10)
        os.makedirs(os.path.join(self.repo._p("backup"), "我自己的文件夹"))
        self.mk(1, 10)
        self.repo.prune_backups(keep=1)
        self.assertTrue(os.path.isdir(os.path.join(self.repo._p("backup"),
                                                   "我自己的文件夹")),
                        "不认识名字的目录一律不碰")


class AutoPruneTest(Base):
    def test_auto_uses_config(self):
        for i in range(15):
            self.mk(i, 100)                        # 1.5MB
        cfg = self.repo.config
        cfg.update({"backup_max_mb": 1, "backup_min_keep": 5, "backup_keep": 40})
        self.repo._write_json("config.json", cfg)
        res = self.repo.auto_prune_backups()
        self.assertIsNotNone(res, "超上限时应该真的清理了一次")
        self.assertLessEqual(self.repo.backup_total_bytes(), 1024 * 1024)
        self.assertGreaterEqual(len(self.repo.backup_dirs()), 5)

    def test_auto_off(self):
        for i in range(15):
            self.mk(i, 100)
        cfg = self.repo.config
        cfg.update({"backup_auto": False})
        self.repo._write_json("config.json", cfg)
        self.assertIsNone(self.repo.auto_prune_backups())
        self.assertEqual(len(self.repo.backup_dirs()), 15)

    def test_auto_quiet_when_small(self):
        for i in range(3):
            self.mk(i, 10)
        self.assertIsNone(self.repo.auto_prune_backups())

    def test_auto_survives_bad_config(self):
        for i in range(12):
            self.mk(i, 100)
        cfg = self.repo.config
        cfg.update({"backup_max_mb": "很多", "backup_min_keep": None})
        self.repo._write_json("config.json", cfg)
        self.repo.auto_prune_backups()          # 不许抛异常

    def test_size_report_sees_backups(self):
        self.mk(0, 100)
        rep = self.repo.size_report()
        self.assertGreaterEqual(rep["backup"]["bytes"], 100 * 1024)


class ApiFieldTest(Base):
    """界面要能看到「共多少、按什么规则清」"""

    def test_backups_endpoint_reports_policy(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg)
        app.registry.add(self.root, "清理备份")
        self.mk(0, 10)
        h = type("H", (Handler,), {"app": app})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), h)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            import urllib.parse
            url = ("http://127.0.0.1:%d/api/backups?path=%s"
                   % (httpd.server_address[1], urllib.parse.quote(self.root)))
            with urllib.request.urlopen(url, timeout=20) as resp:
                d = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(d["auto_prune"])
            self.assertEqual(d["max_mb"], 2048)
            self.assertEqual(d["min_keep"], 10)
            self.assertGreater(d["total_bytes"], 0)
            self.assertEqual(len(d["backups"]), 1)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
