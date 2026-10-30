# -*- coding: utf-8 -*-
r"""第 95 轮：对象读缓存（\api\log、搜索、状态都在反复读同一批目录树）

打开一个项目时，同一个目录树对象会被读十几遍——时间线算每版改了哪些文件、
文件清单展开整棵树、搜索翻提交、状态和工作区比对，每次都是解压 + 重算 sha256。
对象是内容寻址的、写完不会再变，读进内存的副本永远是对的，所以要缓存；
但缓存必须**有上限**（别把几十兆的图纸塞进来），并且**体检不能吃缓存**
（盘上坏了，缓存替它瞒过去，体检就失去意义了）。
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import store as store_mod                       # noqa: E402
from zgr.store import (Repo, RepoError, obj_cache_clear,  # noqa: E402
                       obj_cache_stats, _obj_cache_put, OBJ_CACHE_ITEM)


class Base(unittest.TestCase):
    def setUp(self):
        obj_cache_clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-cache95-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("缓存")
        with open(os.path.join(self.root, "底板.sldprt"), "w", encoding="utf-8") as f:
            f.write("厚度: 12 mm\n")
        os.makedirs(os.path.join(self.root, "零件"), exist_ok=True)
        with open(os.path.join(self.root, "零件", "x.sldprt"), "w",
                  encoding="utf-8") as f:
            f.write("x\n")

    def tearDown(self):
        obj_cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self):
        return self.repo.commit("一版", "测试")

    def any_tree(self):
        return self.repo.commit_info(self.repo.head_oid())["tree"]


class CacheHitTest(Base):
    def setUp(self):
        super().setUp()
        self.commit()

    def test_hit_does_not_decompress_again(self):
        """命中之后不再解压：第二次读同一对象不该再走一遍 zlib + sha256"""
        tree = self.any_tree()
        kind1, p1 = self.repo.get(tree)
        self.assertEqual(kind1, "tree")
        self.assertTrue(obj_cache_stats()["items"] >= 1)

        import zlib
        orig = zlib.decompress

        def boom(*a, **kw):
            raise AssertionError("命中缓存还在解压")

        zlib.decompress = boom
        try:
            kind2, p2 = self.repo.get(tree)
        finally:
            zlib.decompress = orig
        self.assertEqual((kind2, p2), (kind1, p1))

    def test_deleted_object_not_served_from_cache(self):
        """盘上对象被删掉（比如命令行跑过 zgr gc），缓存不能再往外供"""
        tree = self.any_tree()
        self.repo.get(tree)
        os.remove(self.repo.object_path(tree))
        with self.assertRaises(RepoError):
            self.repo.get(tree)

    def test_corrupt_after_cache_is_still_caught(self):
        """先读进缓存、再从盘上改一个字节：下一次读必须报错，不能拿旧副本糊弄"""
        import zlib
        tree = self.any_tree()
        p = self.repo.object_path(tree)
        self.repo.get(tree)
        with open(p, "rb") as f:
            raw = bytearray(zlib.decompress(f.read()))
        raw[-2] = raw[-2] ^ 0xFF
        with open(p, "wb") as f:
            f.write(zlib.compress(bytes(raw)))
        with self.assertRaises(RepoError):
            self.repo.get(tree)

    def test_use_cache_false_always_reads_disk(self):
        tree = self.any_tree()
        self.repo.get(tree)                     # 先让它进缓存
        os.remove(self.repo.object_path(tree))
        with self.assertRaises(RepoError):
            self.repo.get(tree, use_cache=False)

    def test_fsck_does_not_trust_cache(self):
        """盘上对象被同长度改坏：体检必须报出来，不能拿缓存糊弄过去"""
        tree = self.any_tree()
        p = self.repo.object_path(tree)
        self.repo.get(tree)                     # 缓存里是好的那一份
        import zlib
        with open(p, "rb") as f:
            raw = bytearray(zlib.decompress(f.read()))
        raw[-2] = raw[-2] ^ 0xFF                # 同长度改一个字节
        with open(p, "wb") as f:
            f.write(zlib.compress(bytes(raw)))
        rep = self.repo.fsck(deep=False)
        oids = [b["oid"] for b in rep["bad"]]
        self.assertIn(tree, oids)

    def test_shallow_fsck_ok_when_healthy(self):
        rep = self.repo.fsck(deep=False)
        self.assertTrue(rep["ok"], rep["bad"])


class CacheBoundTest(unittest.TestCase):
    def setUp(self):
        obj_cache_clear()
        self.tmp = tempfile.mkdtemp(prefix="zgr-cache-b-")

    def tearDown(self):
        obj_cache_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, name, size=2):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def test_big_object_not_cached(self):
        p = self._file("big")
        _obj_cache_put(p, "blob", b"a" * (OBJ_CACHE_ITEM + 1))
        self.assertEqual(obj_cache_stats()["items"], 0)

    def test_total_bytes_capped(self):
        cap = store_mod.OBJ_CACHE_MAX
        n = (cap // (1 << 20)) + 4          # 每块 1 MB，塞到超过上限
        for i in range(n):
            _obj_cache_put(self._file("o%d" % i), "blob", b"a" * (1 << 20))
        st = obj_cache_stats()
        self.assertLessEqual(st["bytes"], cap)
        self.assertLess(st["items"], n)     # 最旧的被淘汰了

    def test_missing_file_never_cached(self):
        _obj_cache_put(os.path.join(self.tmp, "没这个文件"), "tree", b"[]")
        self.assertEqual(obj_cache_stats()["items"], 0)

    def test_clear_resets(self):
        _obj_cache_put(self._file("a"), "tree", b"[]")
        self.assertEqual(obj_cache_stats()["items"], 1)
        obj_cache_clear()
        st = obj_cache_stats()
        self.assertEqual((st["items"], st["bytes"], st["max_bytes"]),
                         (0, 0, store_mod.OBJ_CACHE_MAX))


class CacheConcurrencyTest(Base):
    def test_concurrent_reads(self):
        """多个线程同时读同一批对象：不能重复计数、不能崩"""
        self.commit()
        oids = [self.repo.head_oid(), self.any_tree()]
        errs = []

        def work():
            try:
                for _ in range(40):
                    for o in oids:
                        self.repo.get(o)
            except Exception as e:           # noqa: BLE001 —— 有异常就记下来
                errs.append(e)

        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        st = obj_cache_stats()
        self.assertGreaterEqual(st["items"], 2)
        self.assertLessEqual(st["bytes"], store_mod.OBJ_CACHE_MAX)


if __name__ == "__main__":
    unittest.main()
