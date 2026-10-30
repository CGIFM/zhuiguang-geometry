# -*- coding: utf-8 -*-
"""跨进程锁：网页服务和命令行同时记录，谁的版本都不能丢"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 子进程脚本单独写成文件，避免在测试里做字符串格式化把自己绕进去
CHILD = '''# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, sys.argv[1])
from zgr.store import Repo

root, mode = sys.argv[2], sys.argv[3]
r = Repo(root)

if mode == "once":
    name = sys.argv[4]
    with open(os.path.join(root, name), "w", encoding="utf-8") as f:
        f.write(name)
    c = r.commit("记录 " + name)
    print(c or "-", flush=True)

elif mode == "rounds":
    tag, n = sys.argv[4], int(sys.argv[5])
    for i in range(n):
        with open(os.path.join(root, "%s-%d.txt" % (tag, i)), "w", encoding="utf-8") as f:
            f.write(tag)
        time.sleep(0.02)
        c = r.commit("%s 第 %d 次" % (tag, i))
        print(c or "-", flush=True)

elif mode == "timed":
    t0 = time.time()
    with open(os.path.join(root, "late-%d.txt" % os.getpid()), "w", encoding="utf-8") as f:
        f.write("x")
    r.commit("等锁之后")
    print("{:.2f}".format(time.time() - t0), flush=True)
'''


class LockingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-lock-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("并行")
        with open(os.path.join(self.root, "base.txt"), "w", encoding="utf-8") as f:
            f.write("底稿")
        self.repo.commit("底稿")
        self.child = os.path.join(self.tmp, "child.py")
        with open(self.child, "w", encoding="utf-8") as f:
            f.write(CHILD)
        self.env = dict(os.environ, PYTHONPATH=HERE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, *args):
        return subprocess.Popen([sys.executable, self.child, HERE, self.root] + list(args),
                                cwd=HERE, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def test_parallel_processes_keep_every_commit(self):
        """4 个进程各记 3 轮：每个报出来的提交号都必须还在历史里"""
        procs = [self.spawn("rounds", "P%d" % i, "3") for i in range(4)]
        oids = []
        for p in procs:
            out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0,
                             "子进程失败：%s" % err.decode("utf-8", "replace")[-600:])
            oids += [x for x in out.decode("utf-8").split() if x != "-"]
        self.assertGreaterEqual(len(oids), 6, "几乎没提交成功，测试没测到东西")

        head = self.repo.head_oid()
        reachable = self.repo.commit_reachable(head)
        lost = [o for o in oids if o not in reachable]
        self.assertEqual(lost, [], "这些提交被并发的另一个提交挤掉了：%s" % lost)
        self.assertTrue(self.repo.fsck()["ok"])

    def test_lock_is_exclusive_across_processes(self):
        """父进程占着锁，子进程必须等着，而不是踩进去"""
        with self.repo.lock("测试占锁"):
            p = self.spawn("timed")
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "子进程没被锁挡住，直接冲进去了")
        out, err = p.communicate(timeout=300)
        self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace")[-400:])
        waited = float(out.decode("utf-8").split()[0])
        self.assertGreater(waited, 1.0, "子进程几乎没等（%.2fs），锁没生效" % waited)

    def test_nested_lock_in_same_thread_is_fine(self):
        with self.repo.lock("外层"):
            with self.repo.lock("内层"):
                with open(os.path.join(self.root, "nested.txt"), "w") as f:
                    f.write("x")
            # 内层退出后外层还在，依然能提交（嵌套不能把自己锁死）
            self.repo.commit("嵌套提交")


if __name__ == "__main__":
    unittest.main(verbosity=2)
