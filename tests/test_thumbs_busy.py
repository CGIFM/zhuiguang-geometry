# -*- coding: utf-8 -*-
"""缩略图「这次没轮到」不能写成「没有图」。

回归点：1.7.1 及以前，任何取图失败（包括 SolidWorks 排队排不上）
都会写一个 6 小时的 miss 标记，于是这台机器在半天内都不会再试，
用户看到的就是「明明有零件，图一直是空的」。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import mesh as M          # noqa: E402
from zgr import thumbs as T        # noqa: E402
from zgr.store import Repo         # noqa: E402


class ThumbsBusyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-thumb-busy-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("测试项目")
        with open(os.path.join(self.root, "零件.sldprt"), "wb") as f:
            f.write(b"not-a-real-sldprt")
        self.repo.commit("第一次")
        _, files = self.repo.files_of(self.repo.head_oid())
        self.oid = files["零件.sldprt"]["oid"]
        self.svc = T.Thumbs()
        self._orig = None

    def tearDown(self):
        if self._orig is not None:
            M.ensure_mesh = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_busy(self):
        self._orig = M.ensure_mesh

        def busy(repo, rel, oid, commit=None, force=False, peer="", peer_token="",
                 budget=None):
            raise M.MeshBusy("排不上")

        M.ensure_mesh = busy

    def test_busy_returns_none_without_miss_marker(self):
        self._patch_busy()
        self.assertIsNone(self.svc.get(self.repo, self.oid, "零件.sldprt", 128,
                                       budget=T.FOREGROUND_WAIT))
        self.assertFalse(os.path.isfile(self.svc._miss_path(self.repo, self.oid, 128)),
                         "排队没排上不能记成「没有图」")

    def test_busy_does_not_cache_empty(self):
        self._patch_busy()
        self.svc.get(self.repo, self.oid, "零件.sldprt", 128, budget=T.FOREGROUND_WAIT)
        self.assertFalse(self.svc.empty(self.repo, self.oid),
                         "也不能记成「这个模型没有实体」")

    def test_saturated_render_gate_no_miss_marker(self):
        """光栅化闸门排满时也要立刻返回，而且同样不能记成「没有图」。"""
        for _ in range(T.MAX_RENDERERS):
            self.svc._render_gate.acquire()
        try:
            self.assertIsNone(self.svc.get(self.repo, self.oid, "零件.sldprt", 128,
                                           budget=1.0))
            self.assertFalse(
                os.path.isfile(self.svc._miss_path(self.repo, self.oid, 128)),
                "闸门排满不是「没有图」")
        finally:
            for _ in range(T.MAX_RENDERERS):
                self.svc._render_gate.release()

    def test_render_slot_raises_when_full(self):
        import time
        for _ in range(T.MAX_RENDERERS):
            self.svc._render_gate.acquire()
        try:
            t0 = time.time()
            with self.assertRaises(M.MeshBusy):
                with self.svc._render_slot(1.0):
                    pass
            self.assertLess(time.time() - t0, 5)
        finally:
            for _ in range(T.MAX_RENDERERS):
                self.svc._render_gate.release()

    def test_mesh_busy_is_mesh_error(self):
        self.assertTrue(issubclass(M.MeshBusy, M.MeshError))
        self.assertFalse(issubclass(M.MeshBusy, M.EmptyMesh),
                         "排不上和空模型是两回事，不能混用")


if __name__ == "__main__":
    unittest.main()
