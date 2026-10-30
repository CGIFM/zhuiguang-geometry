# -*- coding: utf-8 -*-
"""网格渲缩略图：能出图、尺寸对、不是全空白"""
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import thumb3d   # noqa: E402


def _cube_zgm():
    """一个 10mm 立方体；顶点 8 个、面 12 个，够验证投影和 z-buffer"""
    v = [(x, y, z) for x in (0, 10) for y in (0, 10) for z in (0, 10)]
    # 索引顺序：x*4 + y*2 + z
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, c, d in quads:
        tris += [a, b, c, a, c, d]
    head = b"ZGM1" + struct.pack("<II", len(v), len(tris) // 3)
    body = b"".join(struct.pack("<3f", *p) for p in v)
    body += b"".join(struct.pack("<3I", *tris[i:i + 3]) for i in range(0, len(tris), 3))
    return head + body


def _png_size(png):
    return struct.unpack_from(">II", png, 16)


def _non_white(png):
    pos, idat = 8, []
    while pos < len(png):
        ln = struct.unpack_from(">I", png, pos)[0]
        tag = png[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat.append(png[pos + 8:pos + 8 + ln])
        pos += 12 + ln
    raw = zlib.decompress(b"".join(idat))
    return sum(1 for b in raw if b != 255 and b != 0)


class Thumb3DTest(unittest.TestCase):
    def test_render_cube(self):
        png = thumb3d.render_zgm(_cube_zgm(), 128)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(_png_size(png), (128, 128))
        self.assertGreater(_non_white(png), 2000, "立方体应该有明显的非白像素")

    def test_bad_input(self):
        with self.assertRaises(thumb3d.RenderError):
            thumb3d.render_zgm(b"XXXX" + struct.pack("<II", 0, 0), 128)
        with self.assertRaises(thumb3d.RenderError):
            thumb3d.render_zgm(b"ZGM1" + struct.pack("<II", 3, 1), 128)

    def test_shrink(self):
        big = thumb3d.render_zgm(_cube_zgm(), 128)
        small = thumb3d.shrink(big, 64)
        self.assertEqual(_png_size(small), (64, 64))
        self.assertGreater(_non_white(small), 200)


if __name__ == "__main__":
    unittest.main()
