# -*- coding: utf-8 -*-
"""命令行 zgr today：今天记了哪些版本、各是谁记的

网页时间线上方那一行和命令行必须是同一件事——
站会时开浏览器和敲命令，看到的数要对得上。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from zgr.store import Repo  # noqa: E402


class TodayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-today-cli-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("今天")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_today(self, *args):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "today"] + list(args)
                           + [self.root], cwd=APP, env=env,
                           capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")

    def commit(self, msg, who):
        with open(os.path.join(self.root, "%s.txt" % len(os.listdir(self.root))), "w",
                  encoding="utf-8") as f:
            f.write("x")
        return self.repo.commit(msg, who)

    def test_counts_today_and_by_author(self):
        self.commit("上午调了侧板", "我")
        self.commit("补两个定位销", "张三")
        self.commit("下午改料盘", "我")
        code, out, err = self.run_today()
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天 3 个版本", out)
        self.assertIn("我 2", out)
        self.assertIn("张三 1", out)
        self.assertIn("下午改料盘", out)

    def test_multiline_message_shows_first_line_only(self):
        # 一行一个版本，换行会把时间线撑乱
        self.commit("改料盘\n顺手把定位销加长 2mm", "我")
        code, out, err = self.run_today()
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("改料盘", out)
        self.assertNotIn("顺手把定位销加长", out)

    def test_limit_zero_does_not_slice_from_the_end(self):
        # --limit 0 以前会让 rows[:-N] 悄悄吃掉最后几条
        self.commit("a", "我")
        self.commit("b", "我")
        code, out, err = self.run_today("--limit", "0")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天 2 个版本", out)
        self.assertIn("还有 1 个", out)
        # 新的排在前面：留下的是 b，被折叠的是更早的 a
        self.assertIn("  b", out)

    def test_negative_limit_is_not_a_dump_everything(self):
        self.commit("a", "我")
        code, out, err = self.run_today("--limit", "-5")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天 1 个版本", out)
        self.assertIn("  a", out)

    def test_truncated_scan_says_at_least(self):
        # 4 个今天的版本、只让读 3 个：那就只能报「至少 3 个」，并且说明是上限截断
        for i in range(4):
            self.commit("第 %d 版" % (i + 1), "我")
        code, out, err = self.run_today("--scan", "3")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天 至少 3 个版本", out)
        self.assertIn("--scan", out)

        # 读全了就是准数，不再说「至少」
        code2, out2, _ = self.run_today("--scan", "10")
        self.assertEqual(code2, 0)
        self.assertIn("今天 4 个版本", out2)
        self.assertNotIn("至少", out2)
        self.assertNotIn("--scan", out2)

    def test_md_is_paste_ready(self):
        # 站会前粘一段到群里：标题一行、一版一行，不要表格和色块
        self.commit("上午调了侧板", "我")
        self.commit("补两个定位销", "张三")
        code, out, err = self.run_today("--md")
        self.assertEqual(code, 0, err[-300:])
        lines = [ln for ln in out.replace("\r\n", "\n").split("\n") if ln.strip()]
        self.assertTrue(lines[0].startswith("**今天 2 个版本"))
        self.assertIn("张三 1", lines[0])
        self.assertEqual(sum(1 for ln in lines if ln.startswith("- ")), 2)
        self.assertTrue(any("补两个定位销（张三）" in ln for ln in lines))
        self.assertNotIn("  ", out, "不要做表格对齐的空白")
        # 时间那一段是 时:分，不是完整日期
        self.assertTrue(any(ln.startswith("- ") and ln[2:7].count(":") == 1 for ln in lines))

    def test_md_empty_still_explains(self):
        code, out, err = self.run_today("--md")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天还没有记录", out)

    def test_empty_says_so(self):
        # 一点还没记过：不该报错，也不该假装有内容
        code, out, err = self.run_today()
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("今天还没有记录", out)
        self.assertIn("zgr log", out)

    def test_not_a_project_exits_2(self):
        empty = os.path.join(self.tmp, "空的")
        os.makedirs(empty)
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "today", empty],
                           cwd=APP, env=env, capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 2)
        self.assertIn("不是追光几何项目", p.stdout.decode("utf-8", "replace"))

    def test_help_lists_today(self):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "--help"],
                           cwd=APP, env=env, capture_output=True, timeout=120)
        self.assertIn("today", p.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
