# -*- coding: utf-8 -*-
"""文件系统通知：有改动一定叫醒，没改动一定别叫，我们自己写 .zgr 也别叫

这三条任何一条错了，界面就会「永远显示旧的」或者「永远在扫」。
（通知本身只是加速，用不了的时候会自动退回轮询——那条路径见 state() 返回 None。）
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import watchfs                       # noqa: E402


def wait_change(w, old, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        st = w.state()
        if st and st[0] != old:
            return st
        time.sleep(0.05)
    return None


class Base(unittest.TestCase):
    def setUp(self):
        if not sys.platform.startswith("win"):
            self.skipTest("文件通知只有 Windows 有")
        self.tmp = tempfile.mkdtemp(prefix="zgr-watch-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class WatcherTest(Base):
    def test_missing_dir_is_not_fatal(self):
        w = watchfs.Watcher(os.path.join(self.tmp, "没有这个目录"))
        self.assertFalse(w.start())
        self.assertIsNone(w.state(), "用不了就明确说用不了，让调用方退回轮询")

    def test_sees_new_file(self):
        w = watchfs.Watcher(self.root)
        try:
            self.assertTrue(w.start())
            old = w.state()[0]
            with open(os.path.join(self.root, "新零件.sldprt"), "w", encoding="utf-8") as f:
                f.write("1")
            self.assertIsNotNone(wait_change(w, old), "新建文件必须叫醒")
        finally:
            w.stop()

    def test_sees_edit_in_subdir(self):
        sub = os.path.join(self.root, "底板")
        os.makedirs(sub)
        p = os.path.join(sub, "侧板.sldprt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("1")
        w = watchfs.Watcher(self.root)
        try:
            self.assertTrue(w.start())
            old = w.state()[0]
            with open(p, "a", encoding="utf-8") as f:
                f.write("2")
            self.assertIsNotNone(wait_change(w, old), "子目录里的改动也要叫醒")
        finally:
            w.stop()

    def test_ignores_own_meta_writes(self):
        meta = os.path.join(self.root, ".zgr")
        os.makedirs(meta)
        w = watchfs.Watcher(self.root)
        try:
            self.assertTrue(w.start())
            old = w.state()[0]
            for i in range(5):
                with open(os.path.join(meta, "index%d.json" % i), "w",
                          encoding="utf-8") as f:
                    f.write("{}")
                time.sleep(0.05)
            time.sleep(1.0)
            self.assertEqual(w.state()[0], old,
                             "我们自己写 .zgr 不能把自己叫醒，否则恢复一版就白扫一遍")
        finally:
            w.stop()

    def test_stop_ends_thread(self):
        w = watchfs.Watcher(self.root)
        self.assertTrue(w.start())
        w.stop()
        self.assertFalse(w._thread.is_alive())
        w.stop()                                  # 重复停不许抛
        self.assertIsNone(w.state(), "停掉之后就不该再自称可用")

    def test_version_is_globally_unique(self):
        """每个监视器的版本号从**全局**序列取，不是各自从 0 数起。

        淘汰重建之后新监视器如果也从 0 开始，状态缓存会以为「版本没变」，
        把没人监视那段时间发生的改动当成没变化。"""
        a = watchfs.Watcher(self.root)
        b = watchfs.Watcher(self.root)
        self.assertNotEqual(a.state()[0] if a.state() else a._version, b._version)

    def test_concurrent_start_makes_one_thread(self):
        """两个请求同时第一次问同一个项目：只能起一条监听线程。

        起两条的话后写的那条会覆盖 self._handle，stop() 只取消得掉一个，
        另一条永远卡在 ReadDirectoryChangesW 里，句柄跟着泄漏。"""
        import threading
        w = watchfs.Watcher(self.root)
        try:
            # 别的用例可能还留着监视器线程，只能比「这次多了几条」
            before = len([t for t in threading.enumerate() if t.name == "zgr-watch"])
            results = []
            ts = [threading.Thread(target=lambda: results.append(w.start()))
                  for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
            self.assertEqual(len(results), 6)
            self.assertTrue(all(results), "并发启动后应当是「能用」")
            after = len([t for t in threading.enumerate() if t.name == "zgr-watch"])
            self.assertLessEqual(after - before, 1,
                                 "同一个目录只能多出一条 zgr-watch 线程，实际 %d 条"
                                 % (after - before))
        finally:
            w.stop()

    def test_restart_after_stop(self):
        """停了以后又拿来用（被淘汰之后同一个对象又被取到）：还能重新工作"""
        w = watchfs.Watcher(self.root)
        self.assertTrue(w.start())
        w.stop()
        self.assertTrue(w.start(), "重新启动应当成功")
        try:
            old = w.state()[0]
            with open(os.path.join(self.root, "又改了.txt"), "w", encoding="utf-8") as f:
                f.write("x")
            self.assertIsNotNone(wait_change(w, old), "重启之后照样能叫醒")
        finally:
            w.stop()


class ManagerTest(Base):
    def test_same_project_same_watcher(self):
        m = watchfs.Manager()
        try:
            a = m.get(self.root)
            b = m.get(self.root)
            self.assertIsNotNone(a)
            self.assertIs(a, b)
            other = os.path.join(self.tmp, "另一个")
            os.makedirs(other)
            c = m.get(other)
            self.assertIsNot(a, c, "不同项目要各盯各的")
        finally:
            m.stop_all()

    def test_not_a_dir_gives_none(self):
        m = watchfs.Manager()
        try:
            self.assertIsNone(m.get(os.path.join(self.tmp, "没有")))
        finally:
            m.stop_all()

    def test_many_projects_do_not_thrash(self):
        """项目比名额多时，不能每问一次就拆一个、建一个。

        以前名额是 8，而界面会挨个问每个项目的状态：第 9 个项目一进来就淘汰
        「最久没被问的」，下一轮轮到的正好又是它——监视器被反复建了拆，
        每个请求还白白背上最长 3 秒的 join。名额提到 32 之后，常见规模（十几个
        项目）根本不会触发淘汰。"""
        m = watchfs.Manager()
        try:
            made = []
            for i in range(12):
                d = os.path.join(self.tmp, "项目%02d" % i)
                os.makedirs(d)
                made.append(d)
            for d in made:
                self.assertIsNotNone(m.get(d))
            self.assertEqual(len(m._map), 12, "12 个项目应当各留一个监视器，不该互相挤掉")
            again = m.get(made[0])
            self.assertIs(again, m._map[os.path.normcase(os.path.abspath(made[0]))],
                          "再问一次还是同一个对象，没有重建")
        finally:
            m.stop_all()

    def test_eviction_does_not_block_the_request(self):
        """真要淘汰时，停线程（最长 join 3 秒）不能压在请求线程上"""
        m = watchfs.Manager()
        old_max = m.MAX_WATCHERS
        m.MAX_WATCHERS = 2
        try:
            dirs = []
            for i in range(4):
                d = os.path.join(self.tmp, "少量%02d" % i)
                os.makedirs(d)
                dirs.append(d)
            for d in dirs:
                t0 = time.time()
                m.get(d)
                self.assertLess(time.time() - t0, 1.5,
                                "取监视器不该卡在等别人线程退出上")
        finally:
            m.MAX_WATCHERS = old_max
            m.stop_all()


if __name__ == "__main__":
    unittest.main(verbosity=2)
