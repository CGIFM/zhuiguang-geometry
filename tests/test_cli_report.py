# -*- coding: utf-8 -*-
"""命令行 zgr report：近 N 天每天几版、谁记的

站会和周报要的是「这几天我们改了些什么」，不是某一个项目的最新一屏。
日期的分组必须按本地日历日算：按 24 小时窗口切，会把昨天 23 点的那版算到今天。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import zgr.store as store                        # noqa: E402
from zgr.store import Repo                       # noqa: E402

DAY = 86400 * 1000


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-report-cli-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("周报")
        self._real = store.now_ms

    def tearDown(self):
        store.now_ms = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, msg, who="我", back_hours=0, back_days=0):
        n = len(os.listdir(self.root))
        with open(os.path.join(self.root, "f%d.txt" % n), "w", encoding="utf-8") as f:
            f.write("x")
        back = back_days * DAY + back_hours * 3600 * 1000
        store.now_ms = (lambda b=back, r=self._real: (lambda: r() - b))()
        try:
            return self.repo.commit(msg, who)
        finally:
            store.now_ms = self._real

    def run_report(self, *args):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "report"] + list(args)
                           + [self.root], cwd=APP, env=env, capture_output=True, timeout=180)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")

    def test_groups_by_calendar_day(self):
        self.commit("今天一版", "我")
        self.commit("昨天一版", "张三", back_days=1)
        self.commit("前天两版 A", "我", back_days=2)
        self.commit("前天两版 B", "我", back_days=2)
        code, out, err = self.run_report("--days", "7")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("近 7 天", out)
        self.assertIn("4 个版本", out)
        self.assertIn("我 3", out)
        self.assertIn("张三 1", out)
        today = time.strftime("%m-%d")
        yday = time.strftime("%m-%d", time.localtime(time.time() - 86400))
        day3 = time.strftime("%m-%d", time.localtime(time.time() - 2 * 86400))
        lines = {m.group(1): m.group(2) for m in
                 re.finditer(r"^\s+(\d\d-\d\d)\s+(\d+) 版.*$", out, re.M)}
        self.assertEqual(lines[today], "1")
        self.assertEqual(lines[yday], "1")
        self.assertEqual(lines[day3], "2", "同一日历日的两版要算在一起")
        self.assertIn("张三 1", out.split(yday)[1].split("\n")[0])

    def test_days_window_excludes_older(self):
        self.commit("很久以前", "我", back_days=30)
        self.commit("今天", "我")
        code, out, err = self.run_report("--days", "3")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("1 个版本", out)
        self.assertNotIn("很久以前", out)

    def test_empty_window_says_so(self):
        self.commit("很久以前", "我", back_days=30)
        code, out, err = self.run_report("--days", "3")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("没有记录", out)

    def test_md_is_paste_ready(self):
        self.commit("今天一版", "我")
        code, out, err = self.run_report("--days", "7", "--md")
        self.assertEqual(code, 0, err[-300:])
        lines = [ln for ln in out.replace("\r\n", "\n").split("\n") if ln.strip()]
        self.assertTrue(lines[0].startswith("**近 7 天"), lines[0])
        self.assertTrue(any(ln.startswith("- ") and "1 版" in ln for ln in lines[1:]))

    def test_days_is_clamped(self):
        self.commit("今天", "我")
        code, out, err = self.run_report("--days", "0")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("近 1 天", out)
        code2, out2, _ = self.run_report("--days", "99999")
        self.assertEqual(code2, 0)
        self.assertIn("近 366 天", out2)

    def test_not_a_project_exits_2(self):
        empty = os.path.join(self.tmp, "空的")
        os.makedirs(empty)
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "report", empty],
                           cwd=APP, env=env, capture_output=True, timeout=180)
        self.assertEqual(p.returncode, 2)

    def test_help_lists_report(self):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "--help"],
                           cwd=APP, env=env, capture_output=True, timeout=180)
        self.assertIn("report", p.stdout.decode("utf-8", "replace"))

    def test_day_lines_are_sorted_newest_first(self):
        self.commit("今天", "我")
        self.commit("昨天", "我", back_days=1)
        self.commit("前天", "我", back_days=2)
        code, out, err = self.run_report("--days", "7")
        self.assertEqual(code, 0, err[-300:])
        dates = re.findall(r"^\s+(\d\d-\d\d)", out, re.M)
        self.assertEqual(dates, sorted(dates, reverse=True), out)


if __name__ == "__main__":
    unittest.main()
