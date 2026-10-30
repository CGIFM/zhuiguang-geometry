"""把 ZGM 网格直接渲成缩略图（纯标准库，不依赖浏览器、不依赖 OpenGL）。

为什么要有这个：
    有些 SolidWorks 零件文件里根本没存预览图，Windows 外壳处理器也只能给空白。
    唯一能拿到几何形状的办法是让 SolidWorks 把模型导出成网格（见 mesh.py）。
    网格到手以后，服务端要能自己出一张图 —— 不然就得指望用户开着网页去渲，
    那「每个零件都有缩略图」就永远补不齐。

做法：
    正交投影 + 等轴测视角 + 平光着色 + z-buffer。零件缩略图不需要逼真，
    能一眼认出形状就够，所以宁可简单可靠，也不引三方渲染库。
"""

from __future__ import annotations

import math
import struct
import zlib

MAGIC = b"ZGM1"
MAX_TRIS = 400_000          # 再大就别硬渲了，纯 Python 会卡住
MAX_VERTS = 3_000_000       # 面数达标、顶点数离谱的文件同样不能进渲染
MAX_ZGM_BYTES = 128 << 20
BG = (255, 255, 255)        # 跟外壳缩略图的底色一致
BASE = (150, 163, 185)      # 钢灰蓝，跟网页里 STL/STEP 缩略图同色系


class RenderError(Exception):
    pass


def _decode_zgm(buf: bytes):
    if len(buf) < 12 or buf[:4] != MAGIC:
        raise RenderError("网格格式不认识")
    nv, nt = struct.unpack_from("<II", buf, 4)
    if not nv or not nt:
        raise RenderError("网格是空的")
    if nt > MAX_TRIS:
        raise RenderError("模型面数太多（%d 面），不做缩略图" % nt)
    if nv > MAX_VERTS or len(buf) > MAX_ZGM_BYTES:
        raise RenderError("模型太大（%d 个顶点），不做缩略图" % nv)
    need = 12 + nv * 12 + nt * 12
    if len(buf) < need:
        raise RenderError("网格数据被截断")
    verts = struct.unpack_from("<%df" % (nv * 3), buf, 12)
    tris = struct.unpack_from("<%dI" % (nt * 3), buf, 12 + nv * 12)
    return verts, tris, nv, nt


def _norm(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if n <= 1e-9:
        return (0.0, 0.0, 1.0)
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _png_rgb(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    rows = [b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height)]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) +
            chunk(b"IEND", b""))


def render_zgm(buf: bytes, size: int = 512) -> bytes:
    """ZGM 网格 → PNG；视角跟网页里 STEP/STL 缩略图一致（等轴测）"""
    verts, tris, nv, nt = _decode_zgm(buf)
    size = max(32, min(int(size or 512), 1024))
    # 2 倍超采样让边缘不毛糙，但像素数是 4 倍：512 再超采样等于渲 1024×1024，
    # 大零件（十几万面）实测要 8 秒，而悬停框只有 304px 宽，512 已经够细。
    # 所以只给小尺寸超采样，512 直接渲；面数不多的小零件照样保留超采样。
    ss = 2 if (size <= 256 or nt <= 60000) else 1
    W = H = size * ss

    # 包围盒 → 居中
    xs = verts[0::3]
    ys = verts[1::3]
    zs = verts[2::3]
    cx = (min(xs) + max(xs)) * 0.5
    cy = (min(ys) + max(ys)) * 0.5
    cz = (min(zs) + max(zs)) * 0.5
    span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-6)

    d = _norm((0.62, 0.46, 1.0))          # 相机方向（从模型指向相机，越大越靠前）
    up0 = (0.0, 0.0, 1.0)
    r = _norm(_cross(up0, d))
    u = _cross(d, r)

    scale = (min(W, H) * 0.92) / span
    ox, oy = W * 0.5, H * 0.5

    # 顶点一次性投影
    px = [0.0] * nv
    py = [0.0] * nv
    pz = [0.0] * nv
    for i in range(nv):
        j = i * 3
        x = verts[j] - cx
        y = verts[j + 1] - cy
        z = verts[j + 2] - cz
        px[i] = ox + _dot((x, y, z), r) * scale
        py[i] = oy - _dot((x, y, z), u) * scale
        pz[i] = _dot((x, y, z), d)

    zbuf = [-1e30] * (W * H)      # 存深度：越大越靠近相机
    color = bytearray(bytes(BG) * (W * H))

    key = _norm((0.55, 0.2, 0.80))        # 主光：右前上（跟相机同一侧，正面才亮）
    fill = _norm((-0.55, -0.45, 0.25))    # 补光：左后下，把背面提一点
    amb = 0.30

    ntri = nt
    for t in range(ntri):
        k = t * 3
        i0 = tris[k]
        i1 = tris[k + 1]
        i2 = tris[k + 2]
        x0, y0, z0 = px[i0], py[i0], pz[i0]
        x1, y1, z1 = px[i1], py[i1], pz[i1]
        x2, y2, z2 = px[i2], py[i2], pz[i2]

        area2 = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(area2) < 1e-9:
            continue

        # 法线（世界坐标，用投影前的向量算更稳）
        ax, ay, az = verts[i0 * 3], verts[i0 * 3 + 1], verts[i0 * 3 + 2]
        bx, by, bz = verts[i1 * 3], verts[i1 * 3 + 1], verts[i1 * 3 + 2]
        cxv, cyv, czv = verts[i2 * 3], verts[i2 * 3 + 1], verts[i2 * 3 + 2]
        n = _cross((bx - ax, by - ay, bz - az), (cxv - ax, cyv - ay, czv - az))
        ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        if ln <= 1e-12:
            continue
        n = (n[0] / ln, n[1] / ln, n[2] / ln)
        # 让法线朝着相机，背面才不会被当成阴影里的暗面
        if n[0] * d[0] + n[1] * d[1] + n[2] * d[2] < 0:
            n = (-n[0], -n[1], -n[2])
        lam = amb + 0.66 * max(0.0, _dot(n, key)) + 0.14 * max(0.0, _dot(n, fill))
        if lam > 1.0:
            lam = 1.0
        cr = int(BASE[0] * lam)
        cg = int(BASE[1] * lam)
        cb = int(BASE[2] * lam)

        minx = max(0, int(math.floor(min(x0, x1, x2))))
        maxx = min(W - 1, int(math.ceil(max(x0, x1, x2))))
        miny = max(0, int(math.floor(min(y0, y1, y2))))
        maxy = min(H - 1, int(math.ceil(max(y0, y1, y2))))
        if minx > maxx or miny > maxy:
            continue

        inv = 1.0 / area2
        for yy in range(miny, maxy + 1):
            sy = yy + 0.5
            row = yy * W
            for xx in range(minx, maxx + 1):
                sx = xx + 0.5
                w0 = ((x1 - sx) * (y2 - sy) - (x2 - sx) * (y1 - sy)) * inv
                if w0 < 0:
                    continue
                w1 = ((x2 - sx) * (y0 - sy) - (x0 - sx) * (y2 - sy)) * inv
                if w1 < 0:
                    continue
                w2 = 1.0 - w0 - w1
                if w2 < 0:
                    continue
                zz = w0 * z0 + w1 * z1 + w2 * z2
                idx = row + xx
                if zz <= zbuf[idx]:
                    continue
                zbuf[idx] = zz
                o = idx * 3
                color[o] = cr
                color[o + 1] = cg
                color[o + 2] = cb

    if ss == 1:
        return _png_rgb(W, H, bytes(color))

    # 缩采样
    out = bytearray(size * size * 3)
    for y in range(size):
        sy0 = y * ss
        for x in range(size):
            sx0 = x * ss
            tr = tg = tb = 0
            for dy in range(ss):
                base = ((sy0 + dy) * W + sx0) * 3
                for dx in range(ss):
                    o = base + dx * 3
                    tr += color[o]
                    tg += color[o + 1]
                    tb += color[o + 2]
            n = ss * ss
            o2 = (y * size + x) * 3
            out[o2] = tr // n
            out[o2 + 1] = tg // n
            out[o2 + 2] = tb // n
    return _png_rgb(size, size, bytes(out))


def shrink(png: bytes, size: int) -> bytes:
    """把已经渲好的方图缩到指定边长（只为少存几份 512 的大图）"""
    if not png or png[:8] != b"\x89PNG\r\n\x1a\n":
        return png
    # 只处理我们自己产出的 8 位 RGB PNG
    pos = 8
    w = h = 0
    idat = []
    try:
        while pos < len(png):
            ln = struct.unpack_from(">I", png, pos)[0]
            tag = png[pos + 4:pos + 8]
            data = png[pos + 8:pos + 8 + ln]
            if tag == b"IHDR":
                w, h = struct.unpack_from(">II", data, 0)
                if data[8] != 8 or data[9] != 2:
                    return png
            elif tag == b"IDAT":
                idat.append(data)
            pos += 12 + ln
        raw = zlib.decompress(b"".join(idat))
    except (struct.error, zlib.error):
        return png
    if w != h or w <= 0 or not raw:
        return png
    stride = w * 3 + 1
    if len(raw) < stride * h:
        return png
    size = max(16, min(int(size), w))
    out = bytearray(size * size * 3)
    step = w / size
    for y in range(size):
        sy = min(h - 1, int(y * step))
        base = sy * stride + 1
        for x in range(size):
            sx = min(w - 1, int(x * step))
            o = base + sx * 3
            o2 = (y * size + x) * 3
            out[o2] = raw[o]
            out[o2 + 1] = raw[o + 1]
            out[o2 + 2] = raw[o + 2]
    return _png_rgb(size, size, bytes(out))
