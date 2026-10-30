# -*- coding: utf-8 -*-
"""第 98 轮：3D 预览别再拿用户的 SolidWorks 当沙包

现场（167）：追光每 3 分钟起一次 SolidWorks 导 3D 预览，2024 每次都在 slduiu.dll
里崩掉，45 天 558 条崩溃记录；服务被重启时还留下没有窗口的 SolidWorks 孤儿，
把后面所有导出卡成「SolidWorks 正开着」。

这组测试钉住四件事：
1. 连着崩两次就先停手（熔断），不再无限重试；
2. 崩过的版本先让路，上次成功的版本排最前；
3. 只收拾「自己记过、启动时刻对得上、没有窗口」的孤儿；
4. 判断「崩了」还是「模型打不开」不能错——错一次就会把用户能用的版本拉黑。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import mesh as M   # noqa: E402
from zgr import swstep as S  # noqa: E402


class StateFileTest(unittest.TestCase):
    """所有用例都用自己的状态文件，别碰真实机器上的那份"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zgr-sw-state-")
        self.path = os.path.join(self.dir, "zgr-sw-state.json")
        self._orig = M._state_path
        M._state_path = lambda: self.path

    def tearDown(self):
        M._state_path = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)


class ExportOrderTest(StateFileTest):
    def setUp(self):
        super().setUp()
        self._orig_ids = M.sldworks_progids
        M.sldworks_progids = lambda: ["SldWorks.Application.32",
                                      "SldWorks.Application.29",
                                      "SldWorks.Application"]

    def tearDown(self):
        M.sldworks_progids = self._orig_ids
        super().tearDown()

    def test_default_order_is_newest_first(self):
        self.assertEqual(M.export_order()[0], "SldWorks.Application.32")

    def test_last_good_goes_first(self):
        M._note_result("SldWorks.Application.29", True, False)
        self.assertEqual(M.export_order()[0], "SldWorks.Application.29")

    def test_crashed_twice_is_skipped(self):
        M._note_result("SldWorks.Application.32", False, True)
        self.assertIn("SldWorks.Application.32", M.export_order())
        M._note_result("SldWorks.Application.32", False, True)
        self.assertNotIn("SldWorks.Application.32", M.export_order())
        self.assertIn("SldWorks.Application.29", M.export_order())

    def test_all_blacklisted_still_returns_something(self):
        for p in ("SldWorks.Application.32", "SldWorks.Application.29", "SldWorks.Application"):
            M._note_result(p, False, True)
            M._note_result(p, False, True)
        self.assertTrue(M.export_order(), "全被拉黑也不能让功能彻底不可用")

    def test_cooldown_expires(self):
        M._note_result("SldWorks.Application.32", False, True)
        M._note_result("SldWorks.Application.32", False, True)
        st = M._state_read()
        st["bad"]["SldWorks.Application.32"]["until"] = 0
        M._state_write(st)
        self.assertIn("SldWorks.Application.32", M.export_order())


class CrashGuardTest(StateFileTest):
    def setUp(self):
        super().setUp()
        self._orig_ids = M.sldworks_progids
        M.sldworks_progids = lambda: ["SldWorks.Application.32", "SldWorks.Application.29"]

    def tearDown(self):
        M.sldworks_progids = self._orig_ids
        super().tearDown()

    def test_no_hold_before_any_crash(self):
        M._crash_guard()          # 不抛就是通过

    def test_hold_after_two_failures(self):
        M._note_result("SldWorks.Application.32", False, True)
        M._note_result("SldWorks.Application.32", False, True)
        with self.assertRaises(M.MeshError) as ctx:
            M._crash_guard()
        msg = str(ctx.exception)
        self.assertIn("连续崩", msg)
        self.assertIn("分钟", msg)

    def test_success_clears_hold(self):
        M._note_result("SldWorks.Application.32", False, True)
        M._note_result("SldWorks.Application.32", False, True)
        M._note_result("SldWorks.Application.29", True, False)
        M._crash_guard()          # 成功了就该立刻恢复

    def test_model_error_does_not_hold(self):
        """模型打不开不是崩溃：连报五次也不许把功能停掉"""
        for _ in range(5):
            M._note_result("SldWorks.Application.32", False, False)
        M._crash_guard()
        self.assertIn("SldWorks.Application.32", M.export_order())


class LooksCrashedTest(unittest.TestCase):
    def test_empty_output_is_crash(self):
        self.assertTrue(M._looks_crashed(""))
        self.assertTrue(M._looks_crashed("   \r\n"))

    def test_only_try_lines_is_crash(self):
        self.assertTrue(M._looks_crashed("PROGID SldWorks.Application.32\nTRY kind=1 nothing"))

    def test_ok_is_not_crash(self):
        self.assertFalse(M._looks_crashed("PROGID x\nOPEN kind=1\nOK"))

    def test_err_is_not_crash(self):
        self.assertFalse(M._looks_crashed("ERR open 5"))


class ProgidUsedTest(unittest.TestCase):
    def test_reads_echoed_line(self):
        self.assertEqual(M._progid_used("PROGID SldWorks.Application.29\nOPEN kind=1\nOK",
                                        ["a", "b"]), "SldWorks.Application.29")

    def test_falls_back_to_first(self):
        self.assertEqual(M._progid_used("", ["SldWorks.Application.32"]),
                         "SldWorks.Application.32")


class ReapOrphanTest(StateFileTest):
    """只收拾自己留下的、没窗口的孤儿；有窗口（人在用）的一律不碰"""

    def setUp(self):
        super().setUp()
        self.killed = []
        self.alive = set()
        self.times = {}
        self.hwnds = {}
        self._orig = (M._sldworks_pids, M._proc_start_times,
                      M._proc_window_handles, M._kill)
        M._sldworks_pids = lambda: set(self.alive)
        M._proc_start_times = lambda pids: {p: self.times.get(p, 0) for p in pids}
        M._proc_window_handles = lambda pids: {p: self.hwnds.get(p, 0) for p in pids}
        M._kill = lambda pids: (self.killed.extend(sorted(pids)), self.alive.difference_update(pids))
        M.SW_ORPHAN_MIN_AGE = 0

    def tearDown(self):
        (M._sldworks_pids, M._proc_start_times,
         M._proc_window_handles, M._kill) = self._orig
        M.SW_ORPHAN_MIN_AGE = 90
        super().tearDown()

    def _record(self, pid, started):
        M._state_write({"own": {str(pid): int(started)}})
        self.alive.add(pid)

    def test_kills_recorded_headless_orphan(self):
        self.times[15160] = 1000
        self.hwnds[15160] = 0
        self._record(15160, 1000)
        self.assertEqual(M._reap_orphans(), 1)
        self.assertEqual(self.killed, [15160])

    def test_keeps_user_instance_with_window(self):
        self.times[4242] = 1000
        self.hwnds[4242] = 131234         # 有窗口 = 人在编辑
        self._record(4242, 1000)
        self.assertEqual(M._reap_orphans(), 0)
        self.assertEqual(self.killed, [])

    def test_pid_reuse_is_not_ours(self):
        """pid 被系统回收给别的进程：启动时刻对不上就不许杀"""
        self.times[777] = 9999            # 现在是另一个进程了
        self.hwnds[777] = 0
        self._record(777, 1000)
        self.assertEqual(M._reap_orphans(), 0)
        self.assertEqual(self.killed, [])

    def test_forgets_dead_pids(self):
        M._state_write({"own": {"123456": 1}})   # 早就不存在了
        M._reap_orphans()
        self.assertEqual(M._state_read().get("own"), {})


class VbsReportsProgidTest(unittest.TestCase):
    def test_vbs_echoes_the_progid(self):
        code = [ln for ln in M._VBS.splitlines() if not ln.strip().startswith("'")]
        self.assertTrue(any('"PROGID "' in ln for ln in code),
                        "VBS 要告诉我们哪版 SolidWorks 接的活")

    def test_vbs_still_exits_the_instance(self):
        self.assertIn("sw.ExitApp", M._VBS)

    def test_vbs_refuses_to_touch_a_running_instance(self):
        """VBS 结尾会 ExitApp：绝不能接上用户已经开着的那个 SolidWorks"""
        for mod_name, vbs in (("mesh", M._VBS), ("swstep", S._VBS)):
            code = [ln for ln in vbs.splitlines() if not ln.strip().startswith("'")]
            self.assertTrue(any('"BUSY "' in ln for ln in code),
                            mod_name + " 的 VBS 要先问「是不是已经开着」")
            self.assertTrue(any("GetObject(, one)" in ln for ln in code),
                            mod_name + " 的 VBS 用 GetObject 探活")

    def test_busy_is_not_a_crash(self):
        self.assertFalse(M._looks_crashed("BUSY SldWorks.Application.32"))
        self.assertIn("3D 预览先不生成", M._export_hint("BUSY SldWorks.Application.32"))


if __name__ == "__main__":
    unittest.main()
