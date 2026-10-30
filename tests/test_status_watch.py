# -*- coding: utf-8 -*-
"""没人动文件时，状态轮询不该一遍遍全盘扫描

界面每 1.2～3 秒问一次「有没有改动」。以前每次都扫目录：三千个零件的项目两百多毫秒，
而答案几乎永远都是「没变」。现在有文件系统通知：没动静就直接回上次的结果。

但「快」绝不能换来「永远显示旧状态」——所以这几条必须成立：
- 有人改了文件，下一次询问**必须**重新扫（而不是接着用缓存）；
- 通知用不了（非 Windows / 开不出句柄），退回原来的轮询，一切照旧；
- 就算一直没收到通知，超过 MAX_IDLE 也强制重扫一次。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import watchfs                      # noqa: E402
from zgr.registry import Registry            # noqa: E402
from zgr.server import App                   # noqa: E402
from zgr.store import Repo                   # noqa: E402

WINDOWS = sys.platform.startswith("win")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-stwatch-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("状态")
        with open(os.path.join(self.root, "侧板.sldprt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.repo.commit("一")
        self.app = App(Registry(path=os.path.join(self.tmp, "projects.json")))
        self.app.registry.add(self.root, "状态")
        self.scans = []
        real = Repo.status
        outer = self

        def counting(repo_self, *a, **k):
            outer.scans.append(1)
            return real(repo_self, *a, **k)

        self._patch = mock.patch.object(Repo, "status", counting)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.app.fs_watch.stop_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def touch(self):
        with open(os.path.join(self.root, "侧板.sldprt"), "w", encoding="utf-8") as f:
            f.write("改了 %f" % time.time())


class PollFallbackTest(Base):
    """没有文件通知时，行为必须和以前一模一样"""

    def test_without_watcher_it_keeps_polling(self):
        with mock.patch.object(App, "_watch_version", lambda self, repo: None):
            self.app.status_cached(self.repo, ttl=0.01)
            time.sleep(0.05)
            self.app.status_cached(self.repo, ttl=0.01)
            time.sleep(0.05)
            self.app.status_cached(self.repo, ttl=0.01)
        self.assertEqual(len(self.scans), 3, "通知用不了就必须每次都真的去看一眼")


@unittest.skipUnless(WINDOWS, "文件通知只有 Windows 有")
class NoChangeNoScanTest(Base):
    def test_idle_project_is_not_rescanned(self):
        self.app.status_cached(self.repo, ttl=0.01)
        self.assertEqual(len(self.scans), 1)
        for _ in range(6):
            time.sleep(0.05)                      # 远超过 ttl，但一直在 MAX_IDLE 内
            self.app.status_cached(self.repo, ttl=0.01)
        self.assertEqual(len(self.scans), 1,
                         "一个文件都没动，不该反复全盘扫描（这是这一轮的全部意义）")

    def test_change_triggers_rescan(self):
        self.app.status_cached(self.repo, ttl=0.01)
        base = len(self.scans)
        self.touch()
        end = time.time() + 8
        while time.time() < end:
            time.sleep(0.1)
            coin, st = self.app.status_cached(self.repo, ttl=0.01)
            if st["counts"]["modified"]:
                break
        self.assertGreater(len(self.scans), base, "改了文件必须重新扫")
        self.assertIn("侧板.sldprt", st.get("modified") or [],
                      "重扫之后要能看到这个改动")

    def test_forced_rescan_after_max_idle(self):
        with mock.patch.object(watchfs, "MAX_IDLE", 0.3):
            self.app.status_cached(self.repo, ttl=0.01)
            base = len(self.scans)
            time.sleep(0.45)                      # 超过 MAX_IDLE，什么都没改
            self.app.status_cached(self.repo, ttl=0.01)
        self.assertEqual(len(self.scans), base + 1,
                         "通知可能会漏，超过 MAX_IDLE 必须强制重扫一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
