# -*- coding: utf-8 -*-
r"""第 94 轮：fs_path 的性能快路径 + 示例项目自洽性 + `zgr demo`

长路径那次改造把 `fs_path()` 铺到了「每个文件一次」的位置（扫描、状态、体积统计）。
它每次都调 `os.path.abspath()`（normpath + getcwd），2000 个文件的项目平白多出几十毫秒。
这里补两件事：

1. 快路径：已经是规矩的绝对路径就直接返回，不再规范化。
   **约定不能变**——相对路径照样变成绝对路径，超长路径照样加 `\\?\` 前缀。
2. 示例项目得自己说得通：装配体里引用的零件必须真存在于示例里，
   否则第一次点开 3D 预览就是一片红字，恰好在「第一印象」上翻车。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import cli as cli_mod              # noqa: E402
from zgr import demo as demo_mod            # noqa: E402
from zgr.registry import Registry           # noqa: E402
from zgr.store import Repo, fs_path, WIN_LONG   # noqa: E402


class FsPathFastPathTest(unittest.TestCase):
    """快路径不能改变结果，只能省掉一次 abspath"""

    def test_short_abs_path_not_normalized(self):
        p = "C:\\项目\\图纸\\底板.sldprt"
        self.assertEqual(fs_path(p), p)

    def test_fast_path_does_not_call_abspath(self):
        """回归：快路径失效（又去调 abspath）时这里会炸"""
        orig = os.path.abspath

        def boom(*a, **kw):
            raise AssertionError("短绝对路径不该再走 abspath")

        os.path.abspath = boom
        try:
            self.assertEqual(fs_path("D:\\项目\\a.sldprt"), "D:\\项目\\a.sldprt")
        finally:
            os.path.abspath = orig

    def test_relative_path_still_becomes_absolute(self):
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            got = fs_path(os.path.join("子目录", "a.txt"))
            self.assertTrue(os.path.isabs(got), got)
            self.assertTrue(got.endswith("a.txt"), got)
        finally:
            os.chdir(cwd)

    def test_dotdot_still_normalized(self):
        p = "C:\\项目\\..\\图纸\\a.sldprt"
        self.assertEqual(fs_path(p), "C:\\图纸\\a.sldprt")

    def test_forward_slash_abs_still_normalized(self):
        self.assertEqual(fs_path("C:/项目/图纸/a.sldprt"), "C:\\项目\\图纸\\a.sldprt")

    def test_already_prefixed_untouched(self):
        p = "\\\\?\\C:\\" + "很长的目录" * 40 + "\\a.txt"
        self.assertEqual(fs_path(p), p)

    def test_long_path_still_prefixed(self):
        p = "C:\\" + os.sep.join(["很长的目录名称"] * 40) + "\\a.txt"
        self.assertGreaterEqual(len(p), WIN_LONG)
        self.assertTrue(fs_path(p).startswith("\\\\?\\C:\\"), fs_path(p))


class IgnoredParityTest(unittest.TestCase):
    """忽略规则换成正则之后，答案必须和原版 fnmatch 一字不差

    漏判一条 = 恢复时把「本该忽略」的文件删掉；多判一条 = 该记录的文件被悄悄跳过。
    """

    PATS = ["*.tmp", "~$*", "Thumbs.db", ".git", "*.log", "零件01/件0001.sldprt",
            "零件0?", "*.SLDPRT", "a\\b.txt", "*", "?级/x", "[ab]*.txt"]

    PATHS = ["零件01/件0001.sldprt", "零件01/件0001.TMP", "零件01/件0001.tmp",
             "~$底板.sldprt", "缩略图/thumbs.DB", ".git/config", "调试.log",
             "零件01/件0001.sldprt.bak", "a/b.txt", "a\\b.txt", "x/a.txt",
             "一级/x", "任意/再一层/文件.txt", "根文件.sldprt"]

    @staticmethod
    def ref_ignored(rel, patterns):
        import fnmatch
        rel = rel.replace("\\", "/")
        name = rel.rsplit("/", 1)[-1]
        if name.endswith(".zgr-part"):
            return True
        parts = rel.split("/")
        for pat in patterns:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return True
            if "/" not in pat and any(fnmatch.fnmatch(p, pat) for p in parts):
                return True
        return False

    def test_same_answer_as_fnmatch(self):
        from zgr.store import ignored
        for pat in self.PATS:
            for rel in self.PATHS:
                self.assertEqual(ignored(rel, [pat]),
                                 self.ref_ignored(rel, [pat]),
                                 "规则 %r 与路径 %r" % (pat, rel))
        for rel in self.PATHS:
            self.assertEqual(ignored(rel, self.PATS),
                             self.ref_ignored(rel, self.PATS), rel)

    def test_no_fnmatch_in_hot_path(self):
        """回归：热路径里不该再有逐条的 fnmatch.fnmatch（它每次都 normcase 两边）"""
        import fnmatch
        from zgr.store import ignored
        orig = fnmatch.fnmatch

        def boom(*a, **kw):
            raise AssertionError("ignored() 又在调 fnmatch.fnmatch 了")

        fnmatch.fnmatch = boom
        try:
            self.assertTrue(ignored("a/件.tmp", ["*.tmp"]))
            self.assertFalse(ignored("a/件.sldprt", ["*.tmp"]))
        finally:
            fnmatch.fnmatch = orig

    def test_part_suffix_always_ignored(self):
        from zgr.store import ignored
        self.assertTrue(ignored("零件/底板.sldprt.zgr-part", []))


class DoctorIgnoreTest(unittest.TestCase):
    """忽略规则写过头时，体检得说出来那一声"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-doc94-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("体检")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _warns(self):
        from zgr import doctor
        return [w["msg"] for w in doctor.report(self.repo)["warn"]]

    def test_warns_when_rules_exclude_everything(self):
        with open(os.path.join(self.root, "底板.sldprt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.repo.update_config(lambda c: c.update({"ignore": ["*"]}))
        self.assertTrue(any("全排除" in m for m in self._warns()), self._warns())

    def test_quiet_for_empty_project(self):
        self.repo.update_config(lambda c: c.update({"ignore": ["*"]}))
        self.assertFalse(any("全排除" in m for m in self._warns()), self._warns())

    def test_quiet_for_normal_rules(self):
        with open(os.path.join(self.root, "底板.sldprt"), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertFalse(any("全排除" in m for m in self._warns()), self._warns())


class DemoConsistencyTest(unittest.TestCase):
    """示例项目要自洽：装配体引用的零件得在示例里都找得到"""

    def test_assembly_references_exist(self):
        files = demo_mod._files()
        asm = files["装配体/机架.sldasm"]
        refs = [ln.strip().split(" ×")[0] for ln in asm.splitlines()[1:]
                if ln.strip().startswith("零件/")]
        self.assertGreaterEqual(len(refs), 3, asm)
        for rel in refs:
            self.assertIn(rel, files, "装配体引用了不存在的零件：%s" % rel)

    def test_bom_covers_every_part(self):
        files = demo_mod._files()
        bom = files["BOM/机架.csv"]
        parts = [p for p in files if p.startswith("零件/")]
        for p in parts:
            name = os.path.splitext(os.path.basename(p))[0]
            # BOM 里用名称列（底板/侧板/立柱/内六角螺钉），按前两个字对得上即可
            self.assertTrue(any(name[:2] in line for line in bom.splitlines()),
                            "BOM 里找不到零件：%s" % name)


class DemoCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-demo-cli-")
        self._orig_root = demo_mod.demo_root
        self._orig_reg = cli_mod.Registry
        demo_mod.demo_root = lambda: os.path.join(self.tmp, demo_mod.DEMO_NAME)
        path = os.path.join(self.tmp, "projects.json")
        cli_mod.Registry = lambda *a, **kw: Registry(path=path)

    def tearDown(self):
        demo_mod.demo_root = self._orig_root
        cli_mod.Registry = self._orig_reg
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zgr_demo_runs_and_registers(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_mod.main(["demo"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("示例项目已就绪", text)
        self.assertIn("2 个", text)                      # 两个版本
        paths = [p["path"] for p in
                 Registry(path=os.path.join(self.tmp, "projects.json")).projects]
        self.assertEqual(paths, [os.path.join(self.tmp, demo_mod.DEMO_NAME)])

    def test_zgr_demo_is_idempotent(self):
        import contextlib
        import io
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_mod.main(["demo"]), 0)
        projects = Registry(path=os.path.join(self.tmp, "projects.json")).projects
        self.assertEqual(len(projects), 1)
        from zgr.store import Repo
        self.assertEqual(len(Repo(projects[0]["path"]).log()), 2)


if __name__ == "__main__":
    unittest.main()
