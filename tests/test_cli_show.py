# -*- coding: utf-8 -*-
"""命令行 zgr show：看某一版里到底有哪些文件（脚本里用 --json）"""
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


class ShowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-show-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("清单")
        for name in ("侧板.sldprt", "底座.sldprt", "说明.txt"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
                f.write(name * 3)
        self.c1 = self.repo.commit("第一版")
        with open(os.path.join(self.root, "侧板.sldprt"), "a", encoding="utf-8") as f:
            f.write("加厚")
        self.c2 = self.repo.commit("第二版")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_show(self, *args):
        # -I 会忽略 PYTHON* 环境变量，所以用 -X utf8 让子进程输出 UTF-8
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", "-X", "utf8", "zgr.py", "show", self.root]
                           + list(args), cwd=APP, env=env, capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace"), \
            p.stderr.decode("utf-8", "replace")

    def test_json_lists_files_of_a_version(self):
        code, out, err = self.run_show(self.c1, "--json")
        self.assertEqual(code, 0, err[-400:])
        d = json.loads(out)
        self.assertEqual(d["commit"], self.c1)
        self.assertEqual({f["path"] for f in d["files"]},
                         {"侧板.sldprt", "底座.sldprt", "说明.txt"})
        self.assertTrue(all(f["oid"] for f in d["files"]))

    def test_head_alias_and_filter(self):
        code, out, err = self.run_show("HEAD", "--json", "--file", "侧板")
        self.assertEqual(code, 0, err[-400:])
        d = json.loads(out)
        self.assertEqual(d["commit"], self.c2, "HEAD 应该是当前最新那一版")
        self.assertEqual([f["path"] for f in d["files"]], ["侧板.sldprt"])

    def test_limit_trims_the_list(self):
        code, out, err = self.run_show(self.c1, "-n", "2")
        self.assertEqual(code, 0, err[-400:])
        self.assertIn("还有 1 个", out)

    def test_unknown_version_is_an_error_not_an_empty_list(self):
        code, out, err = self.run_show("不存在的版本号")
        self.assertNotEqual(code, 0, "版本不存在时不能悄悄输出空清单")


if __name__ == "__main__":
    unittest.main(verbosity=2)
