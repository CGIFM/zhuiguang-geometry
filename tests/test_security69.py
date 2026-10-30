# -*- coding: utf-8 -*-
"""第 69 轮：畸形文件与回存接口的「内存天花板」

这一轮的共同点是——**输入的大小是别人写的**：
- 复合文档头里写「我有 40 亿个 FAT 扇区」，解析器就照着分配；
- 回存的 PNG 头里写 60000×60000，光栅化就按这个尺寸申请像素缓冲；
- ZGM 头里写 5 亿个顶点，渲染前先 unpack 一亿个 float；
- 工作区里放一个 3 GB 的文件，预览接口整个读进内存；
- 预览接口把 JPEG 当 PNG 发，浏览器解不出来，用户以为「图丢了」。

所以测试里所有的断言都是「不许把内存吃光 / 不许发错类型」，
而不是「性能有没有变好」。
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import preview                      # noqa: E402
from zgr import thumbs as thumbs_mod         # noqa: E402
from zgr.store import Repo                   # noqa: E402


def _png(w, h, ctype=6, depth=8, pixels=None):
    """造一张结构合法的 PNG（默认全黑）"""
    bpp = 4 if ctype == 6 else 3
    raw = (pixels if pixels is not None
           else bytes((w * bpp + 1) * h))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 1)) +
            chunk(b"IEND", b""))


class PngGateTest(unittest.TestCase):
    def test_accepts_normal_png(self):
        png = preview._png_from_bgra(64, 64, b"\x10\x20\x30\xff" * (64 * 64))
        self.assertEqual(preview.png_gate(png), (64, 64))
        self.assertEqual(preview.png_gate(_png(256, 128)), (256, 128))

    def test_rejects_huge_declared_size(self):
        """头里写 60000×60000：合法 PNG 结构，但后面会按它分配 14 GB"""
        bad = _png(8, 8)
        bad = bad[:16] + struct.pack(">II", 60000, 60000) + bad[24:]
        self.assertIsNone(preview.png_gate(bad))

    def test_rejects_too_many_pixels(self):
        bad = _png(8, 8)
        bad = bad[:16] + struct.pack(">II", 8000, 8000) + bad[24:]   # 6400 万像素
        self.assertIsNone(preview.png_gate(bad))

    def test_rejects_decompress_bomb(self):
        """头写 512×512（约 1 MB 像素），IDAT 里塞 40 MB 的纯色"""
        want = (512 * 4 + 1) * 512
        bomb = _png(512, 512, pixels=b"\x00" * (want * 40))
        self.assertIsNone(preview.png_gate(bomb), "解出来比声明的多 40 倍，必须拒")

    def test_rejects_non_rgba_weird_headers(self):
        self.assertIsNone(preview.png_gate(_png(32, 32, ctype=3)), "调色板图不收")
        self.assertIsNone(preview.png_gate(_png(32, 32, depth=16)), "16 位不收")
        self.assertIsNone(preview.png_gate(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20))
        self.assertIsNone(preview.png_gate(b"not a png at all, just text"))


class CfbCapTest(unittest.TestCase):
    """复合文档：所有长度字段都由文件自己填，解析时必须有天花板"""

    def fake(self, nbytes=4096, n_fat=0xFFFFFF00, n_minifat=0xFFFFFF00):
        d = bytearray(nbytes)
        d[:8] = preview.MAGIC
        struct.pack_into("<H", d, 30, 9)          # 扇区 512
        struct.pack_into("<H", d, 32, 6)          # mini 扇区 64
        struct.pack_into("<I", d, 44, n_fat)
        struct.pack_into("<I", d, 48, 0)
        struct.pack_into("<I", d, 56, 4096)
        struct.pack_into("<I", d, 60, 1)
        struct.pack_into("<I", d, 64, n_minifat)
        struct.pack_into("<I", d, 68, 0)
        struct.pack_into("<I", d, 72, 0xFFFFFF00)
        return bytes(d)

    def test_fat_is_capped_by_file_size(self):
        t0 = time.time()
        cf = preview.Cfb(self.fake())
        self.assertLess(time.time() - t0, 3.0, "解析不能因为头里的数字变慢")
        # 4096 字节的文件最多 7 个扇区，FAT 不该超过这个规模
        self.assertLessEqual(len(cf.fat), (cf.n_sectors + 8) * 128)

    def test_chain_is_capped(self):
        cf = preview.Cfb(self.fake())
        fat = [0] * 100000                       # 一条自己指自己的长链
        raw = cf._read_chain(0, fat)
        self.assertLessEqual(len(raw), cf.ssz * (cf.n_sectors + 1))

    def test_extract_on_garbage_returns_none(self):
        self.assertIsNone(preview.extract_bytes(self.fake()))
        self.assertIsNone(preview.extract_bytes(bytes(512)))


class ZgmCapTest(unittest.TestCase):
    def test_rejects_absurd_vert_count(self):
        from zgr import mesh
        hdr = mesh.MAGIC + struct.pack("<II", mesh.MAX_VERTS + 1, 10)
        payload = hdr + bytes(12 * 12)           # 长度对不上也会被拒
        with self.assertRaises(mesh.MeshError):
            mesh.put_zgm(self._root(), "0" * 64, payload)

    def _root(self):
        if not getattr(self, "_r", None):
            self._t = tempfile.mkdtemp(prefix="zgr-69-")
            self._p = os.path.join(self._t, "项目")
            os.makedirs(self._p)
            self._r = Repo(self._p).init("顶点上限")
        return self._r

    def tearDown(self):
        t = getattr(self, "_t", None)
        if t:
            shutil.rmtree(t, ignore_errors=True)
            self._t = self._r = None

    def test_thumb3d_refuses_giant_vertex_count(self):
        from zgr import thumb3d
        buf = thumb3d.MAGIC + struct.pack("<II", thumb3d.MAX_VERTS + 1, 1)
        with self.assertRaises(thumb3d.RenderError):
            thumb3d.render_zgm(buf, 128)


class EmptyMarkTtlTest(unittest.TestCase):
    """「空实体」标记不再是一辈子的判决"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-69-empty-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("空标记")
        self.svc = thumbs_mod.Thumbs()
        self.oid = "a" * 64

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_mark_counts(self):
        self.svc._write_empty_mark(self.repo, self.oid)
        self.assertTrue(self.svc.empty(self.repo, self.oid))
        self.assertIn("空装配", self.svc.empty_reason(self.repo, self.oid))

    def test_expired_mark_is_ignored(self):
        self.svc._write_empty_mark(self.repo, self.oid)
        p = self.svc._empty_path(self.repo, self.oid)
        old = time.time() - thumbs_mod.EMPTY_TTL - 60
        os.utime(p, (old, old))
        self.assertFalse(self.svc.empty(self.repo, self.oid), "过期后应该重新试一次")

    def test_missing_mark(self):
        self.assertFalse(self.svc.empty(self.repo, "b" * 64))


class ForceFlagTest(unittest.TestCase):
    """?force=0 不该被当成「强制重新生成」"""

    def test_truthy_table(self):
        from zgr.server import truthy
        for v in ("1", "true", "TRUE", "yes", "on", " 1 "):
            self.assertTrue(truthy(v), v)
        for v in ("", "0", "false", "no", "off", None, "2"):
            self.assertFalse(truthy(v), repr(v))


class Api69Test(unittest.TestCase):
    """接口层：MIME 按魔数、工作区文件有大小闸门"""

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.srv = sys.modules["zgr.server"]
        self.tmp = tempfile.mkdtemp(prefix="zgr-69-api-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("接口69")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.head = self.repo.commit("一")

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg)
        self.app.registry.add(self.root, "接口69")
        handler = type("H69", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preview_sends_real_mime(self):
        """预览流里其实是 JPEG：以前一律回 image/png"""
        _, files = self.repo.files_of(self.head)
        oid = files["a.txt"]["oid"]
        jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01"
                b"\x00\x00" + b"\xff\xd9")
        svc = thumbs_mod.get_service()
        svc._write_cache(svc.cache_path(self.repo, oid, 512), jpeg)
        url = "%s/api/preview?%s" % (self.base, urllib.parse.urlencode(
            {"path": self.root, "oid": oid, "size": 512}))
        with urllib.request.urlopen(url, timeout=20) as r:
            self.assertEqual(r.headers.get("Content-Type"), "image/jpeg")
            self.assertEqual(r.read(), jpeg)

    def test_workfile_size_cap(self):
        old = self.srv.MAX_WORKFILE
        self.srv.MAX_WORKFILE = 1024
        try:
            big = os.path.join(self.root, "大.sldprt")
            with open(big, "wb") as f:
                f.write(b"x" * 4096)
            url = "%s/api/workfile?%s" % (self.base, urllib.parse.urlencode(
                {"path": self.root, "file": "大.sldprt"}))
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url, timeout=20).read()
            self.assertEqual(cm.exception.code, 413)
            self.assertIn("太大", cm.exception.read().decode("utf-8"))
        finally:
            self.srv.MAX_WORKFILE = old

    def test_svg_gets_locked_down_csp(self):
        svg = os.path.join(self.root, "图.svg")
        with open(svg, "w", encoding="utf-8") as f:
            f.write("<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
        url = "%s/api/workfile?%s" % (self.base, urllib.parse.urlencode(
            {"path": self.root, "file": "图.svg"}))
        with urllib.request.urlopen(url, timeout=20) as r:
            csp = r.headers.get("Content-Security-Policy") or ""
            self.assertIn("default-src 'none'", csp)
            self.assertEqual(r.headers.get("Content-Type"), "image/svg+xml")


class RenderRateLimitTest(unittest.TestCase):
    """跨机匿名刷预览：先限速，别让一台机器被几十个请求烧掉"""

    def setUp(self):
        self.srv = sys.modules["zgr.server"]
        self.srv._RENDER_BUDGET.clear()

    def tearDown(self):
        self.srv._RENDER_BUDGET.clear()

    def test_budget_per_window(self):
        ip = "10.1.2.3"
        n = self.srv.RENDER_PER_MIN
        for i in range(n):
            self.assertTrue(self.srv.render_allowed(ip), "第 %d 次应该放行" % i)
        self.assertFalse(self.srv.render_allowed(ip), "超出上限要拒绝")
        self.assertTrue(self.srv.render_allowed("10.9.9.9"), "别的来源不受影响")

    def test_window_resets(self):
        ip = "10.1.2.4"
        for _ in range(self.srv.RENDER_PER_MIN):
            self.srv.render_allowed(ip)
        self.srv._RENDER_BUDGET[ip][0] -= 61          # 假装一分钟过去了
        self.assertTrue(self.srv.render_allowed(ip))


class ThumbPutGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-69-put-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("回存")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        self.repo.commit("一")
        self.svc = thumbs_mod.Thumbs()
        _, files = self.repo.files_of(self.repo.head_oid())
        self.oid = files["a.txt"]["oid"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_dimension_bomb(self):
        bad = _png(8, 8)
        bad = bad[:16] + struct.pack(">II", 60000, 60000) + bad[24:]
        self.assertFalse(self.svc.put(self.repo, self.oid, bad, 512))

    def test_accepts_real_png(self):
        ok = _png(512, 512)
        self.assertTrue(self.svc.put(self.repo, self.oid, ok, 512))
        self.assertTrue(self.svc.cached(self.repo, self.oid, 512))


if __name__ == "__main__":
    unittest.main(verbosity=2)
