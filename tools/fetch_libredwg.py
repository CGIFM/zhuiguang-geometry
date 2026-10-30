# -*- coding: utf-8 -*-
"""下载 LibreDWG 的 dwg2dxf.exe（可选组件，用于把 DWG 转成 DXF 在网页里预览）。

为什么不在仓库里：LibreDWG 是 **GPL-3.0**，和本项目的 MIT 不同。
把它的二进制塞进 MIT 仓库会造成许可混淆，所以做成「需要时自己下」。
不用 DWG 预览的完全可以跳过这一步，其余功能不受影响。

用法：
    python tools/fetch_libredwg.py          # 下载到 vendor/libredwg/
    python tools/fetch_libredwg.py --check  # 只检查有没有

下载来源：GNU LibreDWG 官方发布的 Windows 预编译包。
提醒：一旦把它放进 vendor/ 并随你的发布物分发，你的发布物整体就受 GPL-3.0 约束。
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "vendor", "libredwg")
# LibreDWG 的 Windows 构建在 GitHub releases 之外没有稳定直链，这里给出官方仓库，
# 由用户自行确认版本后下载；下面的地址是最新的 release 页。
URL = "https://github.com/LibreDWG/libredwg/releases"
NEED = ("dwg2dxf.exe", "libredwg-0.dll")


def present() -> bool:
    return all(os.path.isfile(os.path.join(DEST, n)) for n in NEED)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--zip", help="已经下好的 libredwg 压缩包路径")
    a = ap.parse_args()

    if present():
        print("已就绪：%s" % DEST)
        for n in NEED:
            print("  %s  %.1f KB" % (n, os.path.getsize(os.path.join(DEST, n)) / 1024))
        return 0
    if a.check:
        print("没有 LibreDWG。DWG 预览不可用，其它功能正常。")
        return 1

    if not a.zip:
        print("LibreDWG 是 GPL-3.0，不随仓库分发。请手动下载 Windows 版：")
        print("  %s" % URL)
        print("解压后运行：python tools/fetch_libredwg.py --zip <压缩包路径>")
        return 2

    os.makedirs(DEST, exist_ok=True)
    with zipfile.ZipFile(a.zip) as z:
        got = 0
        for info in z.infolist():
            name = os.path.basename(info.filename)
            if name.endswith((".exe", ".dll")) and not info.is_dir():
                with z.open(info) as src, open(os.path.join(DEST, name), "wb") as dst:
                    dst.write(src.read())
                got += 1
    print("解压了 %d 个文件到 %s" % (got, DEST))
    if not present():
        print("提醒：没找到 %s，请确认压缩包里包含了转换器。" % "、".join(NEED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
