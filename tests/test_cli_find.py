# -*- coding: utf-8 -*-
"""命令行 zgr find：在全部历史里按记录说明找版本

网页搜索框和命令行必须同一口径——网页搜得到、命令行搜不到，只会让人以为数据丢了。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from zgr.store import Repo  # noqa: E402


class FindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-find-cli-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("翻旧账")
        self.ids = {}
        for i, m in enumerate(["最早的一版", "客户确认的第三版", "改料盘", "客户又要改"]):
            with open(os.path.join(self.root, "f%d.txt" % i), "w", encoding="utf-8") as f:
                f.write("x")
            self.ids[m] = self.repo.commit(m, "我")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_find(self, *args):
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "find"] + list(args)
                           + ["--dir", self.root], cwd=APP, env=env,
                           capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")

    def test_finds_by_message(self):
        code, out, err = self.run_find("客户")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("找到 2 个", out)
        self.assertIn("客户确认的第三版", out)
        self.assertIn("客户又要改", out)

    def test_json_for_scripts(self):
        code, out, err = self.run_find("客户", "--json")
        self.assertEqual(code, 0, err[-300:])
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertEqual(d["scanned"], 4)
        self.assertEqual(len(d["hits"]), 2)
        self.assertTrue(all(h["message"] for h in d["hits"]))

    def test_by_version_prefix(self):
        code, out, err = self.run_find(self.ids["改料盘"][:8])
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("改料盘", out)
        self.assertIn("找到 1 个", out)

    def test_missing_says_how_many_scanned(self):
        code, out, err = self.run_find("压根没有")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("全部 4 个版本里都没有提到", out)
        self.assertIn("压根没有", out)

    def test_empty_keyword_is_refused(self):
        code, out, err = self.run_find("  ")
        self.assertEqual(code, 2)
        self.assertIn("请给一个关键词", out)

    def test_not_a_project_says_so(self):
        """不是项目就说不是项目，别回一句「全部 0 个版本里都没有」"""
        empty = os.path.join(self.tmp, "空目录")
        os.makedirs(empty)
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "find", "客户",
                            "--dir", empty], cwd=APP, env=env,
                           capture_output=True, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 2, out)
        self.assertIn("不是追光几何项目", out)

    def test_limit_caps_rows(self):
        code, out, err = self.run_find("客户", "--limit", "1")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("还有 1 个没列出来", out)

    def test_zero_or_negative_limit_is_clamped(self):
        """`--limit 0` 以前会一行不列、`--limit -2` 会变成 hits[:-2]（少列两条）"""
        for bad in ("0", "-2"):
            code, out, err = self.run_find("客户", "--limit", bad)
            self.assertEqual(code, 0, err[-300:])
            self.assertIn("找到 2 个", out)
            self.assertIn("客户又要改", out, "--limit %s 少列了" % bad)
            self.assertNotIn("列出 0 个", out)
            self.assertNotIn("列出 -2 个", out)

    def test_scan_equal_to_count_is_not_reported_as_truncated(self):
        """正好扫完全部版本时不该说「只翻了最近的一部分」"""
        code, out, err = self.run_find("客户", "--scan", "4")
        self.assertEqual(code, 0, err[-300:])
        self.assertNotIn("只翻了最近的一部分", out)

    def test_scan_smaller_than_count_says_truncated(self):
        code, out, err = self.run_find("客户", "--scan", "2")
        self.assertEqual(code, 0, err[-300:])
        self.assertIn("只翻了最近的一部分", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
