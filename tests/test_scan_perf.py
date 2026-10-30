# -*- coding: utf-8 -*-
"""全盘扫描既要有结果正确，也不该在没人改文件的时候反复白跑

界面每几秒就问一次「有没有未记录的改动」，走的就是 scan()。三千个零件的项目里
这一步是纯等待——所以两条底线：
  ① 换实现绝不能改变「哪些文件算项目文件」这个答案（漏一个 = 恢复时被删掉）；
  ② 工作区没变的时候，不该反反复复重写 index.json（大项目里那是一份几百 KB 的 JSON）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, is_link_like   # noqa: E402


def walk_reference(root, repo):
    """os.walk + 老办法判链接：作为「正确答案」的参照实现"""
    base = os.path.abspath(root)
    pats = repo.ignore_patterns
    from zgr.store import ignored, META_DIR
    out = set()
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [d for d in dirnames
                       if d != META_DIR
                       and not ignored((rel_dir + "/" + d).lstrip("/"), pats)
                       and not is_link_like(os.path.join(dirpath, d))]
        for fn in filenames:
            rel = (rel_dir + "/" + fn).lstrip("/")
            if ignored(rel, pats) or rel.split("/")[0] == META_DIR:
                continue
            ap = os.path.join(dirpath, fn)
            if is_link_like(ap) or not os.path.isfile(ap):
                continue
            out.add(rel)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-scan-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("扫描")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ParityTest(Base):
    """换实现之后，答案必须和参照实现一字不差"""

    def make_tree(self):
        r = self.root
        for d in ("底板", "底板/子件", "装配 体", "空目录", "忽略我"):
            os.makedirs(os.path.join(r, d), exist_ok=True)
        for p in ("底板/侧板.sldprt", "底板/子件/螺钉.sldprt", "装配 体/总装.sldasm",
                  "根零件.sldprt", "说明.txt", "忽略我/不该看见.sldprt",
                  "侧面.SLDPRT", "零件.中文名.sldprt"):
            with open(os.path.join(r, p), "w", encoding="utf-8") as f:
                f.write("x")

    def test_matches_reference(self):
        self.make_tree()
        got = set(self.repo.scan())
        want = walk_reference(self.root, self.repo)
        self.assertEqual(got, want)

    def test_ignored_folder_is_skipped(self):
        self.make_tree()
        cfg = self.repo.config
        cfg["ignore"] = ["忽略我"]
        self.repo._write_json("config.json", cfg)
        got = self.repo.scan()
        self.assertNotIn("忽略我/不该看见.sldprt", got)
        self.assertIn("底板/侧板.sldprt", got, "不能把正常的子目录也一起跳过")

    def test_partial_suffix_never_counted(self):
        p = os.path.join(self.root, "半截.sldprt.zgr-part")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        self.assertNotIn("半截.sldprt.zgr-part", self.repo.scan())

    def test_link_is_not_followed(self):
        self.make_tree()
        outside = os.path.join(self.tmp, "项目外")
        os.makedirs(outside)
        with open(os.path.join(outside, "外面的.sldprt"), "w", encoding="utf-8") as f:
            f.write("x")
        link = os.path.join(self.root, "外链")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前账号建不了符号链接（Windows 需要开发者模式）")
        got = self.repo.scan()
        self.assertNotIn("外面的.sldprt", got)
        self.assertFalse(any(k.startswith("外链/") for k in got),
                         "链接目录里的东西一个都不能被卷进项目")

    def test_empty_project(self):
        self.assertEqual(self.repo.scan(), {})


class NoRewriteTest(Base):
    """没有改动的时候，状态检查不该反复写 index.json"""

    def test_status_does_not_rewrite_index(self):
        for i in range(20):
            with open(os.path.join(self.root, "件%02d.sldprt" % i), "w",
                      encoding="utf-8") as f:
                f.write("x")
        self.repo.commit("一")
        idx = os.path.join(self.repo.meta, "index.json")
        self.repo.status()                       # 第一次可能补写
        before = os.stat(idx).st_mtime_ns
        time.sleep(0.05)
        for _ in range(3):
            self.repo.status()
        self.assertEqual(os.stat(idx).st_mtime_ns, before,
                         "文件都没变，index.json 不该被反复重写")

        with open(os.path.join(self.root, "件03.sldprt"), "w", encoding="utf-8") as f:
            f.write("改了")
        self.repo.status()
        self.assertNotEqual(os.stat(idx).st_mtime_ns, before,
                            "真改了之后还得把新状态记下来")


class SpeedTest(Base):
    """只防灾难性退化：两千个文件扫一遍不该慢到秒级"""

    def test_scan_2000_files(self):
        n = 2000
        for i in range(n):
            d = os.path.join(self.root, "分%02d" % (i % 20))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "件%04d.sldprt" % i), "w", encoding="utf-8") as f:
                f.write("x")
        self.repo.scan()                          # 预热（首轮要建目录项缓存）
        t0 = time.perf_counter()
        got = self.repo.scan()
        cost = time.perf_counter() - t0
        self.assertEqual(len(got), n)
        self.assertLess(cost, 1.5, "扫 %d 个文件用了 %.2f 秒，属于灾难性退化" % (n, cost))


if __name__ == "__main__":
    unittest.main(verbosity=2)
