"""复合文档缩略图提取（SolidWorks / Office / DWG 等）

原理：这些文件都是 OLE 复合文档（Compound File Binary），预览图以独立流的形式存在
（SolidWorks 放在 `Preview` 存储下，流名通常是 Preview / PreviewPNG / PreviewJPEG）。
本模块用纯标准库解析 CFB 结构把图取出来，**不需要装 SolidWorks**。

对外：
    extract(path)        → (bytes, mime) 或 None
    extract_bytes(data)  → 同上（给历史版本的对象用）
    is_previewable(name) → 按扩展名判断
"""
from __future__ import annotations

import ctypes
import os
import struct
import zlib

MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
MAXREGSECT = 0xFFFFFFFA

STREAM_T = 2
ROOT_T = 5

# 模型/文档类扩展名（大小写不敏感）
MODEL_EXTS = {
    ".sldprt", ".sldasm", ".slddrw", ".sldblk", ".sldlfp",
    ".dwg", ".dxf", ".step", ".stp", ".igs", ".iges", ".x_t", ".x_b", ".sat",
    ".prt", ".asm", ".stl", ".obj", ".ply", ".3mf", ".ipt", ".iam", ".catpart",
}

# 服务端出不了图、只能靠浏览器端的网格引擎（occt / three）渲染的格式。
# 这些不假装能渲染：直接跳过，省得每个文件都白读一遍再失败。
CLIENT_ONLY_EXTS = {".step", ".stp", ".iges", ".igs", ".brep",
                    ".stl", ".obj", ".ply", ".3mf"}


MAX_PNG_SIDE = 8192              # 单边上限：真实缩略图不会有 8000 像素以上
MAX_PNG_PIXELS = 16_000_000       # 总像素上限（8192×1953），挡「头写很大、其实很小」
MAX_PNG_BYTES = 8 << 20


PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")


def png_gate(data: bytes):
    """回存 PNG 的结构闸门；通过返回 (宽, 高)，不通过返回 None。

    以前只查 8 字节魔数就落盘。可是服务器后面会按 IHDR 里的宽高去分配
    (宽×高×4) 字节的像素缓冲 —— 一个 5 MB 的文件，头里写 60000×60000，
    一次请求就能把服务吃成 MemoryError；或者头写 512×512、IDAT 里塞
    几百 MB 的压缩炸弹。这里不看像素，只做三件事：头字段是否自洽、
    尺寸是否可信、IDAT 解出来的长度是否超过它自己声明的。
    """
    if not data or len(data) > MAX_PNG_BYTES or len(data) < 33:
        return None
    if data[:8] != PNG_MAGIC:
        return None
    ilen = struct.unpack_from(">I", data, 8)[0]
    if ilen != 13 or data[12:16] != b"IHDR":
        return None
    w, h = struct.unpack_from(">II", data, 16)
    depth, ctype, comp, filt, inter = data[24:29]
    if not (1 <= w <= MAX_PNG_SIDE and 1 <= h <= MAX_PNG_SIDE):
        return None
    if w * h > MAX_PNG_PIXELS:
        return None
    if depth != 8 or ctype not in (2, 6) or comp or filt or inter:
        return None
    # 把 IDAT 拼起来，按它声明的尺寸限长解压：解超了就是压缩炸弹
    bpp = 4 if ctype == 6 else 3
    want = (w * bpp + 1) * h
    off, got, seen_idat = 8, bytearray(), False
    while off + 8 <= len(data) and len(got) <= MAX_PNG_BYTES:
        clen = struct.unpack_from(">I", data, off)[0]
        tag = data[off + 4:off + 8]
        if tag == b"IDAT":
            seen_idat = True
            got += data[off + 8:off + 8 + clen]
        elif tag == b"IEND":
            break
        if clen > len(data):          # 长度字段明显在胡写
            return None
        off += 12 + clen
    if not seen_idat or len(got) > MAX_PNG_BYTES:
        return None
    try:
        d = zlib.decompressobj()
        out = d.decompress(bytes(got), want + 1)
    except (zlib.error, MemoryError):
        return None
    if len(out) > want:
        return None                    # 解出来的像素比它自己声明的还多
    return int(w), int(h)


def is_client_only(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in CLIENT_ONLY_EXTS
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff"}

# 图片魔数 → MIME
_SIGS = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"RIFF", "image/webp"),        # 需再看 8..12 是否为 WEBP
]
_META_SIGS = [
    (b"\xd7\xcd\xc6\x9a", "image/wmf"),          # 可放置 WMF
    (b"\x01\x00\x09\x00", "image/wmf"),          # 标准 WMF 头
    (b"\x01\x00\x00\x00", "image/emf"),          # EMF（HEADER 记录类型 1）
]


def is_previewable(name: str) -> bool:
    ext = os.path.splitext(name or "")[1].lower()
    return ext in MODEL_EXTS or ext in IMAGE_EXTS


def is_model(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in MODEL_EXTS


def sniff(blob: bytes):
    """按魔数判断图片类型，认不出返回 None"""
    if not blob or len(blob) < 4:
        return None
    for sig, mime in _SIGS:
        if blob.startswith(sig):
            if mime == "image/webp" and blob[8:12] != b"WEBP":
                continue
            return mime
    for sig, mime in _META_SIGS:
        if blob.startswith(sig):
            # EMF 的 01 00 00 00 太容易误判，要求后面紧跟 " EMF"
            if mime == "image/emf" and blob[40:44] != b" EMF":
                continue
            return mime
    return None


class CfbError(Exception):
    pass


class Cfb:
    """只读的 Compound File Binary 解析器"""

    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != MAGIC:
            raise CfbError("不是复合文档")
        self.data = data
        self.ssz = 1 << struct.unpack_from("<H", data, 30)[0]
        # 分区表里的扇区号、链长全是文件自己写的，畸形文件可以让解析器
        # 一路 allocate 到几个 GB（`_read_chain` 以前只按 100 万个扇区封顶）。
        # 真实文件里扇区数绝不可能超过文件本身，用它当硬上限最稳妥。
        self.n_sectors = max(1, (len(data) - 512) // max(512, self.ssz))
        self.msz = 1 << struct.unpack_from("<H", data, 32)[0]
        self.n_fat = struct.unpack_from("<I", data, 44)[0]
        self.dir_start = struct.unpack_from("<I", data, 48)[0]
        self.cutoff = struct.unpack_from("<I", data, 56)[0]
        self.minifat_start = struct.unpack_from("<I", data, 60)[0]
        self.n_minifat = struct.unpack_from("<I", data, 64)[0]
        self.difat_start = struct.unpack_from("<I", data, 68)[0]
        self.n_difat = struct.unpack_from("<I", data, 72)[0]
        if self.ssz not in (512, 4096):
            raise CfbError("扇区大小异常：%d" % self.ssz)
        self.fat = self._read_fat()
        self.minifat = self._read_minifat()
        self.entries = self._read_dir()
        self.root = next((e for e in self.entries if e["type"] == ROOT_T), None)

    # ---- 底层
    def _sector(self, n: int) -> bytes:
        off = (n + 1) * self.ssz
        if n > MAXREGSECT or off >= len(self.data):
            return b"\x00" * self.ssz
        chunk = self.data[off:off + self.ssz]
        return chunk + b"\x00" * (self.ssz - len(chunk))

    def _read_fat(self):
        difat = list(struct.unpack_from("<109I", self.data, 76))
        cap = self.n_sectors + 8
        # 追加 DIFAT 扇区
        sec = self.difat_start
        guard = 0
        while sec <= MAXREGSECT and guard < 1000 and len(difat) < cap:
            guard += 1
            block = self._sector(sec)
            vals = struct.unpack_from("<%dI" % (self.ssz // 4), block, 0)
            difat.extend(vals[:-1])
            sec = vals[-1]
        fat = []
        for fs in difat[:max(0, min(int(self.n_fat), cap))]:
            if fs > MAXREGSECT:
                continue
            if len(fat) >= cap * (self.ssz // 4):
                break
            block = self._sector(fs)
            fat.extend(struct.unpack_from("<%dI" % (self.ssz // 4), block, 0))
        return fat

    def _read_minifat(self):
        if not self.n_minifat or self.minifat_start > MAXREGSECT:
            return []
        raw = self._read_chain(self.minifat_start, self.fat, self.n_minifat * self.ssz)
        return list(struct.unpack_from("<%dI" % (len(raw) // 4), raw, 0))

    def _read_chain(self, start: int, fat, limit=None) -> bytes:
        out = []
        seen = set()
        sec = start
        # 链最多只能有这么多个扇区：文件里根本没有更多数据，再走就是凭空造内存
        max_secs = self.n_sectors + 1
        while sec <= MAXREGSECT and sec not in seen:
            if len(out) >= max_secs:
                break
            seen.add(sec)
            out.append(self._sector(sec))
            if limit is not None and len(out) * self.ssz >= limit:
                break
            if sec >= len(fat):
                break
            sec = fat[sec]
        raw = b"".join(out)
        return raw[:limit] if limit is not None else raw

    def _read_dir(self):
        raw = self._read_chain(self.dir_start, self.fat)
        entries = []
        for off in range(0, len(raw) - 127, 128):
            e = raw[off:off + 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            etype = e[66]
            if etype not in (1, 2, 5) or nlen < 2 or nlen > 64:
                continue
            name = e[:nlen - 2].decode("utf-16-le", "ignore")
            start = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<Q", e, 120)[0]
            if size > len(self.data) * 8:      # v3 文档只写 4 字节，高位脏数据
                size = size & 0xFFFFFFFF
            entries.append({"name": name, "type": etype, "start": start, "size": size})
        return entries

    # ---- 对外
    def stream_bytes(self, entry) -> bytes:
        size = entry["size"]
        if size == 0:
            return b""
        if size < self.cutoff and entry["type"] == STREAM_T and self.root:
            # 小流：在 root 的 mini stream 里
            mini = self._read_chain(self.root["start"], self.fat, self.root["size"])
            out, sec, seen = [], entry["start"], set()
            while sec <= MAXREGSECT and sec not in seen and len(out) * self.msz < size:
                seen.add(sec)
                off = sec * self.msz
                out.append(mini[off:off + self.msz])
                if sec >= len(self.minifat):
                    break
                sec = self.minifat[sec]
            return b"".join(out)[:size]
        return self._read_chain(entry["start"], self.fat, size)

    def streams(self):
        for e in self.entries:
            if e["type"] == STREAM_T:
                yield e


def _pick(blobs):
    """从候选流里挑最像缩略图的：优先大图，其次要求魔数可识别"""
    best = None
    for name, blob in blobs:
        mime = sniff(blob)
        if not mime:
            continue
        score = len(blob)
        # PNG 通常最干净，JPEG 次之，BMP/元文件最后
        if mime == "image/png":
            score *= 4
        elif mime == "image/jpeg":
            score *= 3
        elif mime == "image/bmp":
            score *= 2
        if best is None or score > best[0]:
            best = (score, blob, mime, name)
    if not best:
        return None
    return best[1], best[2]


def extract_bytes(data: bytes, magics_only: bool = False):
    """从文件内容里抽缩略图 → (bytes, mime) 或 None"""
    if not data:
        return None
    mime = sniff(data)
    if mime and magics_only:
        return data, mime
    if data[:8] == MAGIC:
        try:
            cf = Cfb(data)
        except (CfbError, struct.error, IndexError):
            return None
        except MemoryError:
            # 畸形文件撑爆内存：当它没有预览，绝不让整个服务跟着崩
            return None
        cands = []
        for e in cf.streams():
            low = e["name"].lower()
            if "preview" in low or "thumbnail" in low:
                try:
                    blob = cf.stream_bytes(e)
                except (CfbError, struct.error, IndexError):
                    continue
                if blob:
                    cands.append((e["name"], blob))
        return _pick(cands)
    return None


def extract(path: str, max_bytes: int = 64 << 20):
    """读取文件并抽缩略图；带魔数的图片文件本身也可预览"""
    # 工作区里的文件路径可能超过 260 字符：这里只读，加上前缀就能读
    from .store import fs_path
    path = fs_path(path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS and size <= max_bytes:
        with open(path, "rb") as f:
            blob = f.read(max_bytes)
        mime = sniff(blob)
        return (blob, mime) if mime else None
    if size > max_bytes:
        # 复合文档的预览流通常在文件头部附近，但 CFB 是随机访问的，太大就直接放弃
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return None
    return extract_bytes(data)


# ---------------------------------------------------------------- Windows 外壳缩略图
BLANK_RATIO = 0.0015      # 非白像素少于 0.15% 视为"没有缩略图"（只剩坐标轴那种）


def _almost_blank(bgra: bytes, width: int, height: int) -> bool:
    """SolidWorks 未保存预览图的零件只会渲染出一小撮坐标轴，这种图没有信息量"""
    total = width * height
    if total <= 0 or len(bgra) < total * 4:
        return True
    nonwhite = 0
    step = 4 * max(1, total // 150000)      # 大图抽样，最多看 15 万像素
    seen = 0
    for i in range(0, total * 4, step):
        seen += 1
        if bgra[i] < 245 or bgra[i + 1] < 245 or bgra[i + 2] < 245:
            nonwhite += 1
    return nonwhite / max(seen, 1) < BLANK_RATIO


def _png_from_bgra(width: int, height: int, bgra: bytes) -> bytes:
    """把自顶向下的 BGRA 像素编码成 PNG（纯标准库）"""
    stride = width * 4
    # GDI 出来的 32 位位图经常整张 alpha=0（外壳缩略图不写 alpha），直接用会全透明
    if not any(bgra[3::4]):
        bgra = bytearray(bgra)
        bgra[3::4] = b"\xff" * (len(bgra) // 4)
        bgra = bytes(bgra)
    rows = []
    for y in range(height):
        row = bytearray(bgra[y * stride:(y + 1) * stride])
        # BGRA → RGBA
        row[0::4], row[2::4] = row[2::4], row[0::4]
        rows.append(b"\x00" + bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) +
            chunk(b"IEND", b""))


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _shell_path(path: str) -> str:
    """外壳命名空间只认反斜杠；给正斜杠会返回 E_INVALIDARG（0x80070057）"""
    import ntpath
    return ntpath.normpath(os.path.abspath(path))


def shell_thumbnail(path: str, size: int = 512, flags: int = 0x108):
    """用 Windows 外壳缩略图处理器出图（SolidWorks/Office/PDF 的处理器由各家安装时注册）

    返回 PNG bytes；没有处理器或失败返回 None。只在 Windows 上可用。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    path = _shell_path(path)

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    iid = _GUID(0xBCC18B79, 0xBA16, 0x442F,
                (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B))

    inited = False
    try:
        if ole32.CoInitializeEx(None, 0x2) in (0, 1):   # APARTMENTTHREADED / 已初始化
            inited = True
    except Exception:
        pass

    create = shell32.SHCreateItemFromParsingName
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_GUID),
                       ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long

    pv = ctypes.c_void_p()
    hbmp = wintypes.HBITMAP()
    hdc = None
    try:
        hr = create(path, None, ctypes.byref(iid), ctypes.byref(pv))
        if hr != 0 or not pv:
            return None
        vtbl = ctypes.cast(pv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HBITMAP))
        get_image = proto(vtbl[3])          # IShellItemImageFactory::GetImage
        # 8 = THUMBNAILONLY（不要退化成文件类型图标）；0x100 = SCALEUP（小图放大到请求尺寸）
        hr = get_image(pv, SIZE(size, size), flags, ctypes.byref(hbmp))
        if hr != 0 or not hbmp:
            return None

        bm = BITMAP()
        if not gdi32.GetObjectW(hbmp, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        w, h = int(bm.bmWidth), int(bm.bmHeight)
        if w <= 0 or h <= 0 or w > 8192 or h > 8192:
            return None

        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负数 = 自顶向下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        n = gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        if n != h:
            return None
        pixels = buf.raw[:w * h * 4]
        if _almost_blank(pixels, w, h):
            return None                     # 只有坐标轴、没有零件的空图，不如不给
        return _png_from_bgra(w, h, pixels)
    except Exception:
        return None
    finally:
        try:
            if hbmp:
                gdi32.DeleteObject(hbmp)
            if hdc:
                user32.ReleaseDC(None, hdc)
            if pv:
                vp = ctypes.cast(pv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                rel = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vp[2])
                rel(pv)
        except Exception:
            pass


_THUMB_HANDLER = "{e357fccd-a995-4576-b01f-234630154e96}"   # IThumbnailProvider
_capable_cache: dict[str, bool] = {}


def shell_capable(ext: str) -> bool:
    """本机是否为该扩展名注册了外壳缩略图处理器（例如是否装了 SolidWorks）"""
    if os.name != "nt":
        return False
    ext = (ext or "").lower()
    if not ext:
        return False
    if ext in _capable_cache:
        return _capable_cache[ext]
    ok = False
    try:
        import winreg
        sub = r"%s\ShellEx\%s" % (ext, _THUMB_HANDLER)
        for hive in (winreg.HKEY_CLASSES_ROOT, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, (sub if hive is winreg.HKEY_CLASSES_ROOT
                                           else r"Software\Classes" + "\\" + sub)):
                    ok = True
                    break
            except OSError:
                continue
    except Exception:
        ok = False
    _capable_cache[ext] = ok
    return ok


def render(path: str, size: int = 512):
    """优先自带预览流，其次外壳缩略图；返回 (bytes, mime) 或 None"""
    res = extract(path)
    if res:
        return res
    png = shell_thumbnail(path, size)
    if png:
        return png, "image/png"
    return None


# ---------------------------------------------------------------- 自测
if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        res = extract(p)
        if res:
            blob, mime = res
            print("%s -> %s %d bytes" % (p, mime, len(blob)))
        else:
            print("%s -> (无预览)" % p)
