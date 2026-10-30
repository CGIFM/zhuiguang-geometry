"""DWG → DXF 转换（LibreDWG 的 dwg2dxf）

为什么要转：浏览器里没有能直接读 DWG 的成熟渲染库；LibreDWG 是 GNU 官方的开源实现，
把 DWG 转成 DXF 后交给前端的 dxf-viewer 渲染，效果稳定且离线可用。

转换结果按源文件内容哈希缓存，同一版本只转一次；同时缓存 gzip 版本，46MB 的图纸
压缩后通常只有几 MB，内网传输快很多。
"""
from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time

from .store import RepoError

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(APP_ROOT, "vendor", "libredwg", "dwg2dxf.exe")
TIMEOUT = 180

# 同一张图同时被两个标签页打开时，只让一个线程真去转
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            if len(_LOCKS) > 64:
                _LOCKS.clear()
            lk = _LOCKS[key] = threading.Lock()
        return lk


def _unique_tmp(path: str) -> str:
    """临时文件名必须带线程号和纳秒：只带 pid 时同一进程两个线程会撞同一个名字"""
    return "%s.%d-%d-%d.tmp" % (path, os.getpid(), threading.get_ident(), time.time_ns())


def available() -> bool:
    return os.name == "nt" and os.path.isfile(EXE)


def convert(src: str, data: bytes, cache_dir: str) -> tuple[bytes, bytes]:
    """把 DWG 字节转成 DXF，返回 (dxf_bytes, dxf_gz_bytes)。缓存命中时不调外部程序。"""
    if not available():
        raise RepoError("这台机器上没有 LibreDWG 转换器")
    key = hashlib.sha256(data).hexdigest()
    os.makedirs(cache_dir, exist_ok=True)
    plain = os.path.join(cache_dir, key + ".dxf")
    zipped = plain + ".gz"
    with _key_lock(key):
        return _convert_locked(src, data, plain, zipped)


def _convert_locked(src: str, data: bytes, plain: str, zipped: str):
    if os.path.isfile(plain):
        if not os.path.isfile(zipped):
            # 原子地补上 .gz：以前直接写目标名，另一个线程可能正好读到半个 gzip，
            # 浏览器拿到「gzip 解压失败」的坏响应
            tmp_gz = _unique_tmp(zipped)
            with open(plain, "rb") as f, gzip.open(tmp_gz, "wb", 6) as g:
                shutil.copyfileobj(f, g, 1 << 20)
            os.replace(tmp_gz, zipped)
        # 注意用普通 open 读 .gz：拿到的必须是压缩后的原始字节，
        # gzip.open(...).read() 会解压，和响应头 Content-Encoding: gzip 冲突
        with open(plain, "rb") as f, open(zipped, "rb") as g:
            return f.read(), g.read()

    tmpdir = tempfile.mkdtemp(prefix="zgr-dwg-")
    try:
        tmp_dwg = os.path.join(tmpdir, "in.dwg")
        with open(tmp_dwg, "wb") as f:
            f.write(data)
        tmp_dxf = os.path.join(tmpdir, "out.dxf")
        try:
            p = subprocess.run([EXE, "-o", tmp_dxf, tmp_dwg],
                               capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RepoError("图纸太大，转换超过 %d 秒" % TIMEOUT) from None
        if not os.path.isfile(tmp_dxf) or os.path.getsize(tmp_dxf) == 0:
            msg = (p.stderr or p.stdout or b"").decode("utf-8", "replace").strip()
            raise RepoError("DWG 转换失败%s" % (("：" + msg[:200]) if msg else ""))
        with open(tmp_dxf, "rb") as f:
            dxf = f.read()
        gz = gzip.compress(dxf, 6)
        # 先写临时名再换，避免并发读到半个文件
        for path, payload in ((plain, dxf), (zipped, gz)):
            tmp_out = _unique_tmp(path)
            with open(tmp_out, "wb") as f:
                f.write(payload)
            os.replace(tmp_out, path)
        return dxf, gz
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
