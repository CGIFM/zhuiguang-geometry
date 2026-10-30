"""生成应用图标（纯标准库）：蓝紫渐变圆角方块 + 白色光点拖尾，写入 assets/zgr.ico"""
import os
import struct
import zlib

SIZES = (16, 24, 32, 48, 64, 128, 256)


def _png(size, pixels):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size):
    px = bytearray(size * size * 4)
    r = size * 0.22                      # 圆角半径
    c1, c2 = (47, 107, 255), (122, 92, 255)
    head = (size * 0.30, size * 0.30)    # 光点
    tail = (size * 0.74, size * 0.74)    # 拖尾末端
    for y in range(size):
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            # 圆角方块遮罩
            dx = max(r - cx, 0) + max(cx - (size - r), 0)
            dy = max(r - cy, 0) + max(cy - (size - r), 0)
            if (dx * dx + dy * dy) ** 0.5 > r + 0.5:
                continue
            t = (cx + cy) / (2 * size)
            col = _mix(c1, c2, t)
            # 拖尾：沿对角线由亮到暗的一串光点
            vx, vy = tail[0] - head[0], tail[1] - head[1]
            L2 = vx * vx + vy * vy
            s = ((cx - head[0]) * vx + (cy - head[1]) * vy) / L2
            s = max(0.0, min(1.0, s))
            px_, py_ = head[0] + vx * s, head[1] + vy * s
            dist = ((cx - px_) ** 2 + (cy - py_) ** 2) ** 0.5
            radius = size * (0.085 - 0.045 * s)
            glow = max(0.0, 1.0 - dist / max(radius * 2.2, 1e-6)) ** 2
            a = min(1.0, glow * (1.15 - 0.55 * s))
            if a > 0:
                col = _mix(col, (255, 255, 255), a)
            i = (y * size + x) * 4
            px[i:i + 4] = bytes((col[0], col[1], col[2], 255))   # RGBA
    return _png(size, bytes(px))


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    frames = [(s, render(s)) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, blob = b"", b""
    for s, data in frames:
        entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(data), offset)
        blob += data
        offset += len(data)
    path = os.path.join(out_dir, "zgr.ico")
    with open(path, "wb") as f:
        f.write(header + entries + blob)
    print("已生成", path, os.path.getsize(path), "字节")


if __name__ == "__main__":
    main()
