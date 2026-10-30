# -*- coding: utf-8 -*-
"""完整性：对象坏一个字节也要当场发现，绝不把坏图纸写回工作区"""
import os
import shutil
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


def _obj(repo, oid):
    return repo.object_path(oid)


def _flip_byte(repo, oid):
    """把对象文件的**解压后内容**改一个字节，再压缩写回——模拟磁盘静默损坏/手改文件。
    长度完全不变，只看长度是发现不了的。"""
    p = _obj(repo, oid)
    with open(p, "rb") as f:
        raw = bytearray(zlib.decompress(f.read()))
    raw[-1] = raw[-1] ^ 0x01
    with open(p, "wb") as f:
        f.write(zlib.compress(bytes(raw), 6))


class IntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-int-")
        self.root = os.path.join(self.tmp, "p")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("体检")
        for i in range(6):
            sub = os.path.join(self.root, "part", "grp%d" % (i % 2))
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, "p%d.sldprt" % i), "wb") as f:
                f.write(("零件 %d " % i).encode("utf-8") * 50)
        self.c1 = self.repo.commit("第一版")
        with open(os.path.join(self.root, "part", "grp0", "p0.sldprt"), "wb") as f:
            f.write(b"changed")
        self.c2 = self.repo.commit("第二版")
        _, self.files = self.repo.files_of(self.c1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_repo_passes(self):
        r = self.repo.fsck()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["bad"], [])
        self.assertGreater(r["objects"], 0)
        r2 = self.repo.fsck(deep=True)
        self.assertTrue(r2["ok"], r2)

    def test_deep_fsck_catches_same_length_corruption(self):
        oid = self.files["part/grp1/p1.sldprt"]["oid"]
        _flip_byte(self.repo, oid)
        light = self.repo.fsck()
        self.assertTrue(light["ok"], "浅体检只看在不在，本来就不该发现")   # 确认行为边界
        deep = self.repo.fsck(deep=True)
        self.assertFalse(deep["ok"])
        self.assertIn(oid, [b["oid"] for b in deep["bad"]])

    def test_blob_to_file_refuses_corrupt_and_keeps_target(self):
        oid = self.files["part/grp1/p1.sldprt"]["oid"]
        _flip_byte(self.repo, oid)
        dst = os.path.join(self.tmp, "out.sldprt")
        with open(dst, "wb") as f:
            f.write(b"old")
        with self.assertRaises(RepoError):
            self.repo.blob_to_file(oid, dst)
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), b"old", "坏对象不能覆盖目标文件")
        self.assertFalse(os.path.exists(dst + ".tmp.%d" % os.getpid()), "临时文件要清掉")

    def test_restore_with_corrupt_object_rolls_back(self):
        oid = self.files["part/grp0/p0.sldprt"]["oid"]
        _flip_byte(self.repo, oid)
        self.repo.restore(self.c2)                     # 回到第二版
        before = {}
        for dirpath, _, names in os.walk(self.root):
            if ".zgr" in dirpath:
                continue
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, "rb") as f:
                    before[os.path.relpath(p, self.root)] = f.read()
        with self.assertRaises(RepoError):
            self.repo.restore(self.c1)                 # 要写坏对象
        after = {}
        for dirpath, _, names in os.walk(self.root):
            if ".zgr" in dirpath:
                continue
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, "rb") as f:
                    after[os.path.relpath(p, self.root)] = f.read()
        self.assertEqual(before, after, "恢复失败必须回滚到原样")

    def test_missing_object_reported(self):
        oid = self.files["part/grp0/p2.sldprt"]["oid"]
        os.remove(_obj(self.repo, oid))
        r = self.repo.fsck()
        self.assertFalse(r["ok"])
        self.assertIn(oid, r["missing"])

    def test_dangling_objects_are_not_an_error(self):
        oid = self.repo.put("blob", b"nobody points at me")
        r = self.repo.fsck()
        self.assertTrue(r["ok"])
        self.assertIn(oid, r["dangling"])

    def test_get_refuses_corrupt_commit(self):
        _flip_byte(self.repo, self.c1)
        with self.assertRaises(RepoError):
            self.repo.commit_info(self.c1)

    def test_empty_repo(self):
        d = os.path.join(self.tmp, "empty")
        os.makedirs(d)
        r = Repo(d).init("空")
        rep = r.fsck(deep=True)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["objects"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
