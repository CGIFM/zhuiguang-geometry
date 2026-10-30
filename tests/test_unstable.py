# -*- coding: utf-8 -*-
"""正在写盘的文件不许记进版本

真实场景：SolidWorks 保存一个大装配要几秒，用户点完保存立刻点「记录」。
这时读到的文件是写了一半的——它读得出来、不报错，于是被安安静静地记成
一个版本，从这一刻起每一条分支都带着这个残件。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


class UnstableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-unstable-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("写盘")
        self.path = os.path.join(self.root, "大装配.sldasm")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("初始")
        self.repo.commit("第一版")
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def churn(self, interval=0.02):
        """模拟「SolidWorks 正在往文件里写」"""
        def run():
            while not self.stop.is_set():
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write("x" * 40)
                except OSError:
                    pass
                time.sleep(interval)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def test_busy_file_blocks_commit(self):
        self.churn()
        time.sleep(0.1)
        head_before = self.repo.head_oid()
        with self.assertRaises(RepoError) as e:
            self.repo.commit("手快点了记录")
        self.assertIn("还在被写入", str(e.exception))
        self.assertIn("大装配.sldasm", str(e.exception))
        self.assertEqual(self.repo.head_oid(), head_before, "不该产生半截版本")

    def test_save_pausing_midway_is_still_caught(self):
        """保存中间会停顿（写一阵、算一阵）：0.12 秒那一眼可能正好落在停顿里"""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("a")                       # 保证 mtime 是「刚刚」

        def late_write():
            time.sleep(0.35)                   # 第一次复核（0.12s）之后、第二次（0.8s）之前
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("y" * 50)
        threading.Thread(target=late_write, daemon=True).start()
        t0 = time.time()
        with self.assertRaises(RepoError) as e:
            self.repo.commit("保存中间点了一下记录")
        self.assertIn("还在被写入", str(e.exception))
        self.assertGreaterEqual(time.time() - t0, 0.8, "根本没做第二次复核")

    def test_quiet_file_commits_normally(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("改完了，没人在写")
        self.repo.commit("正常记录")          # 刚写完但已经稳定：照常记
        self.assertIsNotNone(self.repo.head_oid())

    def test_old_file_is_not_double_checked(self):
        """几十分钟没动过的文件不该被复查拖慢（只复查最近 3 秒动过的）"""
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        t0 = time.time()
        self.repo.commit("没改也要能记")
        self.assertLess(time.time() - t0, 2.0)

    def test_status_never_blocked_by_busy_file(self):
        """只有「记录」要挡；看状态不能因为文件在写就报错"""
        self.churn()
        time.sleep(0.1)
        st = self.repo.status()
        self.assertIn("clean", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
