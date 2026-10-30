# -*- coding: utf-8 -*-
"""断电/强杀之后：恢复中断能发现、能收尾；备份不会无限长"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-rec-")
        self.root = os.path.join(self.tmp, "p")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("恢复")
        for i in range(4):
            with open(os.path.join(self.root, "p%d.txt" % i), "w", encoding="utf-8") as f:
                f.write("版本一 %d" % i)
        self.c1 = self.repo.commit("v1")
        for i in range(4):
            with open(os.path.join(self.root, "p%d.txt" % i), "w", encoding="utf-8") as f:
                f.write("版本二 %d" % i)
        self.c2 = self.repo.commit("v2")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bak_names(self):
        return self.repo.backup_dirs()

    def test_successful_restore_leaves_no_journal(self):
        self.repo.restore(self.c1)
        self.assertIsNone(self.repo.pending_restore(),
                          "恢复做完了还留记号，下次启动会吓人一跳")

    def test_failed_restore_clears_journal_after_rollback(self):
        p = self.repo.object_path(self.repo.files_of(self.c1)[1]["p0.txt"]["oid"])
        with open(p, "rb") as f:
            raw = bytearray(f.read())
        raw[-3] ^= 0x01                       # 破坏压缩流，解压就炸，跟真实坏盘一样
        with open(p, "wb") as f:
            f.write(raw)
        with self.assertRaises(RepoError):
            self.repo.restore(self.c1)
        self.assertIsNone(self.repo.pending_restore(), "回滚干净了就该把记号清掉")

    def test_interrupted_restore_can_be_finished(self):
        """模拟断电：工作区被改了一半，.zgr 里留着记号，文件其实没恢复完"""
        with open(os.path.join(self.root, "p0.txt"), "w", encoding="utf-8") as f:
            f.write("写了一半")
        with open(os.path.join(self.root, "half.txt"), "w", encoding="utf-8") as f:
            f.write("半截文件")
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            json.dump({"commit": self.c1, "target": self.root, "backup": None,
                       "time": 1, "files": 4}, f)
        j = self.repo.pending_restore()
        self.assertIsNotNone(j)
        self.assertEqual(j["commit"], self.c1)
        res = self.repo.finish_pending_restore("again")
        self.assertEqual(res["mode"], "again")
        with open(os.path.join(self.root, "p0.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "版本一 0")
        self.assertFalse(os.path.exists(os.path.join(self.root, "half.txt")),
                         "重新恢复应当把多出来的半截文件清掉")
        self.assertIsNone(self.repo.pending_restore())

    def test_interrupted_restore_can_fall_back_to_backup(self):
        bak = os.path.join(self.repo.meta, "backup", "20260101-000000-1")
        os.makedirs(bak)
        with open(os.path.join(bak, "p0.txt"), "w", encoding="utf-8") as f:
            f.write("备份里的原文")
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            json.dump({"commit": self.c1, "target": self.root, "backup": bak,
                       "time": 1, "files": 4}, f)
        res = self.repo.finish_pending_restore("backup")
        self.assertEqual(res["restored"], 1)
        with open(os.path.join(self.root, "p0.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "备份里的原文")
        self.assertIsNone(self.repo.pending_restore())

    def test_journal_with_bom_is_still_read(self):
        """记事本手改留下的 BOM 不能让记号「凭空消失」"""
        with open(self.repo._journal_path(), "w", encoding="utf-8-sig") as f:
            json.dump({"commit": self.c1, "target": self.root, "backup": None}, f)
        j = self.repo.pending_restore()
        self.assertIsNotNone(j)
        self.assertEqual(j["commit"], self.c1)
        self.assertFalse(j.get("corrupt"))

    def test_corrupt_journal_is_reported_not_ignored(self):
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        j = self.repo.pending_restore()
        self.assertIsNotNone(j, "记号坏了也不能当没发生过")
        self.assertTrue(j["corrupt"])
        with self.assertRaises(RepoError):
            self.repo.finish_pending_restore("again")
        self.repo.clear_pending_restore()
        self.assertIsNone(self.repo.pending_restore())

    def test_no_journal_is_not_pending(self):
        self.assertIsNone(self.repo.pending_restore())
        with self.assertRaises(RepoError):
            self.repo.finish_pending_restore()

    def test_prune_keeps_newest_and_never_touches_foreign_dirs(self):
        foreign = os.path.join(self.repo.meta, "backup", "手工留档-别删我")
        os.makedirs(foreign, exist_ok=True)
        with open(os.path.join(foreign, "keep.txt"), "w") as f:
            f.write("keep")
        for i in range(5):
            bak = os.path.join(self.repo.meta, "backup", "2026010%d-000000-1" % (i + 1))
            os.makedirs(bak)
            with open(os.path.join(bak, "f.txt"), "w") as f:
                f.write("x" * 100)
            time.sleep(0.01)
        names = self._bak_names()
        self.assertEqual(len(names), 5)
        res = self.repo.prune_backups(keep=2)
        self.assertEqual(res["kept"], 2)
        self.assertEqual(len(res["removed"]), 3)
        self.assertGreater(res["bytes"], 0)
        left = self._bak_names()
        self.assertEqual(left, sorted(left, reverse=True))
        self.assertEqual(len(left), 2)
        self.assertTrue(all(n.startswith("2026010") for n in left))
        self.assertTrue(os.path.isfile(os.path.join(foreign, "keep.txt")),
                        "不是我们建的批次目录，一个字节都不许碰")

    def test_prune_dry_run_deletes_nothing(self):
        for i in range(4):
            os.makedirs(os.path.join(self.repo.meta, "backup", "2026010%d-000000-1" % (i + 1)))
        res = self.repo.prune_backups(keep=1, dry=True)
        self.assertEqual(len(res["removed"]), 3)
        self.assertEqual(len(self._bak_names()), 4)

    def test_disk_full_aborts_before_writing_anything(self):
        """盘不够时：一个字节都不许写，而不是写一半再报错"""
        import zgr.store as st

        before = {}
        for dirpath, _dn, fns in os.walk(self.root):
            if ".zgr" in dirpath:
                continue
            for f in fns:
                p = os.path.join(dirpath, f)
                with open(p, "rb") as fh:
                    before[os.path.relpath(p, self.root)] = fh.read()

        real = st.shutil.disk_usage
        st.shutil.disk_usage = lambda _p: type("D", (), {"free": 1 << 20})()
        try:
            with self.assertRaises(RepoError) as ctx:
                self.repo.restore(self.c1)
        finally:
            st.shutil.disk_usage = real
        self.assertIn("磁盘", str(ctx.exception))
        after = {}
        for dirpath, _dn, fns in os.walk(self.root):
            if ".zgr" in dirpath:
                continue
            for f in fns:
                p = os.path.join(dirpath, f)
                with open(p, "rb") as fh:
                    after[os.path.relpath(p, self.root)] = fh.read()
        self.assertEqual(before, after)

    def test_prune_keeps_at_least_one(self):
        os.makedirs(os.path.join(self.repo.meta, "backup", "20260101-000000-1"))
        res = self.repo.prune_backups(keep=0)
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["removed"], [])
        self.assertEqual(len(self._bak_names()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
