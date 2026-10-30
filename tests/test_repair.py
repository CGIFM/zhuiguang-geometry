# -*- coding: utf-8 -*-
"""两台机器互为备份：本机对象丢了/坏了，从对方补齐"""
import os
import shutil
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo  # noqa: E402
from zgr.sync import repair, sync  # noqa: E402


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-repair-")
        self.a = Repo(os.path.join(self.tmp, "A")).init("本机")
        self.share = os.path.join(self.tmp, "共享")
        for i in range(8):
            p = os.path.join(self.a.root, "part", "p%d.sldprt" % i)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(("零件 %d " % i).encode("utf-8") * 200)
        self.head = self.a.commit("初始")
        sync(self.a, share=self.share, push=True, pull=False, log=lambda m: None)
        self.files = self.a.files_of(self.head)[1]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corrupt(self, oid):
        p = self.a.object_path(oid)
        with open(p, "rb") as f:
            raw = bytearray(zlib.decompress(f.read()))
        raw[-2] ^= 0x01
        with open(p, "wb") as f:
            f.write(zlib.compress(bytes(raw), 6))

    def test_repair_missing_object(self):
        oid = self.files["part/p0.sldprt"]["oid"]
        os.remove(self.a.object_path(oid))
        self.assertFalse(self.a.fsck()["ok"])
        from zgr.sync import make_backend
        be = make_backend(share=self.share, project=self.a.name)
        res = repair(self.a, backend=be, log=lambda m: None)
        self.assertTrue(res["ok"], res)
        self.assertGreaterEqual(res["fetched"], 1)
        self.assertTrue(self.a.fsck()["ok"])

    def test_repair_corrupt_object(self):
        oid = self.files["part/p3.sldprt"]["oid"]
        self._corrupt(oid)
        deep = self.a.fsck(deep=True)
        self.assertFalse(deep["ok"])
        from zgr.sync import make_backend
        be = make_backend(share=self.share, project=self.a.name)
        res = repair(self.a, backend=be, log=lambda m: None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["removed_bad"], 1)
        self.assertTrue(self.a.fsck(deep=True)["ok"])
        # 补回来的对象内容要和原来一字不差
        out = os.path.join(self.tmp, "x.bin")
        self.a.blob_to_file(oid, out)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), ("零件 3 ").encode("utf-8") * 200)

    def test_repair_never_touches_branch_or_worktree(self):
        before = {}
        for dirpath, _dn, fns in os.walk(self.a.root):
            if ".zgr" in dirpath:
                continue
            for f in fns:
                p = os.path.join(dirpath, f)
                with open(p, "rb") as fh:
                    before[os.path.relpath(p, self.a.root)] = fh.read()
        oid = self.files["part/p5.sldprt"]["oid"]
        os.remove(self.a.object_path(oid))
        from zgr.sync import make_backend
        repair(self.a, backend=make_backend(share=self.share, project=self.a.name),
               log=lambda m: None)
        self.assertEqual(self.a.head_oid(), self.head)
        after = {}
        for dirpath, _dn, fns in os.walk(self.a.root):
            if ".zgr" in dirpath:
                continue
            for f in fns:
                p = os.path.join(dirpath, f)
                with open(p, "rb") as fh:
                    after[os.path.relpath(p, self.a.root)] = fh.read()
        self.assertEqual(before, after, "修复只动物件库，不该碰工作区")

    def test_repair_reports_when_peer_is_also_incomplete(self):
        share_repo = Repo(os.path.join(self.share, self.a.name))
        oid = self.files["part/p2.sldprt"]["oid"]
        os.remove(self.a.object_path(oid))
        os.remove(share_repo.object_path(oid))       # 备份那边也没有
        from zgr.sync import make_backend
        res = repair(self.a, backend=make_backend(share=self.share, project=self.a.name),
                     log=lambda m: None)
        self.assertFalse(res["ok"])
        self.assertTrue(res["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
