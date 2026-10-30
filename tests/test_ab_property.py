# -*- coding: utf-8 -*-
"""A/B 差分测试：拿「当时的磁盘」当预言机，核对恢复出来的字节

思路（和属性测试/对拍一个道理）：不信任软件自己算出来的东西。
每个版本记下来那一刻，测试自己把整个目录读一遍存成真相；之后随机跳回任意
版本，恢复出来的目录必须和当时那份真相**逐字节**一致。任何「少一个文件、
多一个文件、内容差一个字节」都会被抓出来。
"""
import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402

NAMES = ["A.sldprt", "b.SLDPRT", "零件 一.sldprt", "x y.sldasm", "位图.png",
         "说明.txt", "a.b.c.dwg", " 前导空格.txt"]
DIRS = ["", "part", "part/grp1", "组件/子装配", "deep/a/b/c"]


def snapshot(root):
    """独立预言机：不走软件任何代码，直接读盘"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".zgr"]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            with open(p, "rb") as f:
                out[rel] = f.read()
    return out


class ABPropertyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-ab-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("对拍")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_random_workload_restores_exactly(self):
        rnd = random.Random(20261029)
        truth = []            # [(commit, snapshot)]
        live = {}             # 测试自己维护的文件内容（另一份真相）

        for step in range(40):
            for _ in range(rnd.randint(1, 6)):
                rel = (rnd.choice(DIRS) + "/" if rnd.choice(DIRS) else "") + rnd.choice(NAMES)
                rel = rel.lstrip("/")
                act = rnd.choices(["write", "delete", "rewrite"], [5, 2, 3])[0]
                dst = os.path.join(self.root, rel.replace("/", os.sep))
                if act == "delete" and rel in live:
                    live.pop(rel)
                    if os.path.exists(dst):
                        os.remove(dst)
                    continue
                body = ("%s #%d %s" % (rel, step, "零件" * rnd.randint(1, 40))).encode("utf-8")
                if act == "write" and rel in live:
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                with open(dst, "wb") as f:
                    f.write(body)
                live[rel] = body
            c = self.repo.commit("第 %d 轮" % step)
            if not c:
                continue
            # 记录那一刻的真实内容：工作区（含未记录?不，刚提交完，二者一致）
            truth.append((c, snapshot(self.root)))
            self.assertEqual(snapshot(self.root), live, "提交后磁盘内容和测试自己的账本应当一致")

        self.assertGreater(len(truth), 5, "随机负载至少要产生几个版本")

        for _ in range(15):
            c, expect = rnd.choice(truth)
            self.repo.restore(c)
            got = snapshot(self.root)
            if got != expect:
                missing = sorted(set(expect) - set(got))
                extra = sorted(set(got) - set(expect))
                diff = [k for k in set(got) & set(expect) if got[k] != expect[k]]
                self.fail("恢复到 %s 后和当时的磁盘对不上：缺 %s 多 %s 内容不同 %s"
                          % (c[:8], missing[:4], extra[:4], diff[:4]))

    def test_restore_is_idempotent(self):
        """连续恢复同一个版本两次，第二次不该再写任何东西（也不该报错）"""
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"one")
        c1 = self.repo.commit("一")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"two")
        self.repo.commit("二")
        r1 = self.repo.restore(c1)
        r2 = self.repo.restore(c1)
        self.assertEqual(r2["written"], 0, "同样的目标版本，第二次不该重复写盘")
        self.assertEqual(r1["written"], 1)

    def test_branch_switch_roundtrip(self):
        """在两条分支之间来回切，每次内容都要精确回到那条分支的版本"""
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"main-1")
        m = self.repo.commit("主线")
        self.repo.set_branch("dev", m)
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"dev-1")
        d = self.repo.commit("开发", branch="dev")
        for name, oid, body in (("main", m, b"main-1"), ("dev", d, b"dev-1"),
                                ("main", m, b"main-1"), ("dev", d, b"dev-1")):
            self.repo.set_branch(name, oid)
            self.repo.restore(oid)
            with open(os.path.join(self.root, "a.txt"), "rb") as f:
                self.assertEqual(f.read(), body, "切到 %s 后内容不对" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
