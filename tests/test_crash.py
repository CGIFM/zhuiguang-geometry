# -*- coding: utf-8 -*-
"""崩溃一致性：在提交/恢复中途把进程强杀（等价于断电、任务管理器结束进程）。

规矩只有三条：
1. 仓库必须还能打开，分支头要么是旧的、要么是新的，不能指向一个坏提交；
2. 引用的对象必须都在（fsck 通过），不能出现「记录说有、盘上没有」；
3. 工作区里每个文件要么是完整的旧内容，要么是完整的新内容，绝不能半截。
"""
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def blob_oid(p):
    """和软件一样的算法：sha256("blob\\n<长度>\\n" + 内容)。"""
    h = hashlib.sha256()
    size = os.path.getsize(p)
    h.update(b"blob\n%d\n" % size)
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


class CrashTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-crash-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("崩溃")
        # 文件够多、够大，提交/恢复都要跑一会儿，才杀得进中途
        self.bodies = {}
        for i in range(240):
            rel = "part/grp%d/p%03d.sldprt" % (i % 8, i)
            p = os.path.join(self.root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            body = (b"%d-" % i) + bytes([65 + i % 20]) * 60000
            with open(p, "wb") as f:
                f.write(body)
            self.bodies[rel] = body
        self.c1 = self.repo.commit("第一版")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, code, delay):
        """起一个子进程干活，过 delay 秒直接 TerminateProcess"""
        env = dict(os.environ, PYTHONPATH=HERE)
        p = subprocess.Popen([sys.executable, "-c", code], cwd=HERE, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(delay)
        p.kill()
        p.wait(timeout=30)
        return p.returncode

    def check_worktree_intact(self):
        """工作区里的每个文件：要么是旧版完整内容，要么是另一个已知的完整内容"""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != ".zgr"]
            for fn in filenames:
                if fn.endswith(".zgr-part"):
                    continue        # 强杀留下的半成品，已经排除在项目文件之外
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, self.root).replace("\\", "/")
                if rel not in self.bodies:
                    continue
                with open(p, "rb") as f:
                    got = f.read()
                self.assertIn(got, (self.bodies[rel], b"NEW-" + self.bodies[rel]),
                              "文件被写成了半截或来历不明：%s" % rel)

    def test_kill_during_commit(self):
        # 先造一批新内容
        for i in range(0, 240, 3):
            rel = "part/grp%d/p%03d.sldprt" % (i % 8, i)
            p = os.path.join(self.root, rel.replace("/", os.sep))
            with open(p, "wb") as f:
                f.write(b"NEW-" + self.bodies[rel])
        for delay in (0.05, 0.25, 0.6):
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from zgr.store import Repo\n"
                    "Repo(%r).commit('被强杀的一版')\n") % (HERE, self.root)
            self.spawn(code, delay)
            head = self.repo.head_oid()
            rep = self.repo.fsck()
            self.assertTrue(rep["ok"], "提交被打断后体检不通过：%s" % rep)
            self.assertTrue(head, "提交被打断后分支头没了")
            self.repo.commit_info(head)          # 头指向的提交必须读得出来
            self.check_worktree_intact()
        # 最后还能正常提交，说明仓库没被搞坏（先改一个文件，保证有东西可记）
        with open(os.path.join(self.root, "恢复后新文件.txt"), "wb") as f:
            f.write(b"after crash")
        ok = self.repo.commit("恢复后正常提交")
        self.assertTrue(ok, "强杀之后连一次正常提交都做不了")
        self.assertTrue(self.repo.fsck()["ok"])

    def test_kill_during_restore_then_finish(self):
        # 第二版：全部文件换内容
        for i in range(240):
            rel = "part/grp%d/p%03d.sldprt" % (i % 8, i)
            p = os.path.join(self.root, rel.replace("/", os.sep))
            with open(p, "wb") as f:
                f.write(b"V2-" + self.bodies[rel])
        c2 = self.repo.commit("第二版")

        code = ("import sys; sys.path.insert(0, %r)\n"
                "from zgr.store import Repo\n"
                "Repo(%r).restore(%r)\n") % (HERE, self.root, self.c1)
        self.spawn(code, 0.35)

        rep = self.repo.fsck()
        self.assertTrue(rep["ok"], "恢复被打断后体检不通过：%s" % rep)
        # 每个文件要么是 v1 的，要么是 v2 的，不能有别的
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d != ".zgr"]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, self.root).replace("\\", "/")
                if rel not in self.bodies:
                    continue
                with open(p, "rb") as f:
                    got = f.read()
                self.assertIn(got, (self.bodies[rel], b"V2-" + self.bodies[rel]),
                              "出现了既不是旧版也不是新版的半截内容：%s" % rel)

        # 不管有没有留下「中断记号」，再恢复一次都必须得到干净一致的 v1
        if self.repo.pending_restore():
            self.repo.finish_pending_restore("again")
        else:
            self.repo.restore(self.c1)
        _, files = self.repo.files_of(self.c1)
        for rel, info in files.items():
            p = os.path.join(self.root, rel.replace("/", os.sep))
            self.assertEqual(blob_oid(p), info["oid"], "收尾后内容仍然不对：%s" % rel)
        self.assertIsNone(self.repo.pending_restore())


if __name__ == "__main__":
    unittest.main(verbosity=2)
