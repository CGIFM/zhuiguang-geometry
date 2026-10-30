# -*- coding: utf-8 -*-
r"""打一个「双击就能装」的 Windows 安装包（Inno Setup）。

和便携包的区别
--------------
便携包是一堆文件，用户得自己找 bat；安装包是一个 exe：
双击 → 下一步 → 桌面出现图标 → 能在「应用和功能」里正常卸载。
装在当前用户目录（%LOCALAPPDATA%\Programs），**全程不需要管理员、不弹 UAC**，
自带 Python 运行时，目标电脑不需要装任何东西。

用法：
    python tools/build_installer.py
    python tools/build_installer.py --python-from dist/追光几何-1.0.0-portable.zip
    python tools/build_installer.py --iscc "C:\path\to\ISCC.exe"

产物：
    dist/追光几何-<版本>-安装版.exe
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

import build_portable as P

ROOT = P.ROOT
DIST = P.DIST
ISS = os.path.join(ROOT, "installer", "setup.iss")
STAGE = os.path.join(DIST, "installer")

# Inno Setup 常见安装位置（官方便携/用户级安装都在后者）
ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def find_iscc(explicit: str) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit("指定的 ISCC 不存在：%s" % explicit)
        return explicit
    for p in ISCC_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    raise SystemExit(
        "没找到 Inno Setup 的编译器 ISCC.exe。\n"
        "装一个（免管理员）：winget install JRSoftware.InnoSetup\n"
        "或用 --iscc 指定路径。")


def stage_payload(ver: str, python_from: str, download: bool) -> str:
    """铺出安装包的内容：应用 + 自带 Python + 使用说明"""
    payload = os.path.join(STAGE, "payload")
    shutil.rmtree(payload, ignore_errors=True)
    os.makedirs(payload)

    for f in P.APP_FILES:
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(payload, f))
    for d in P.APP_DIRS:
        src = os.path.join(ROOT, d)
        if os.path.isdir(src):
            P.copy_tree(src, os.path.join(payload, d))

    # 原生桌面客户端（exe + WebView2 组件）：安装后的桌面图标就是它
    P.ensure_desktop()
    P.copy_desktop(payload)

    zip_path = P.find_runtime_zip(python_from)
    if not zip_path and download:
        zip_path = P.download_runtime(DIST)
    if not zip_path:
        raise SystemExit("找不到 Python 运行时：加 --download，或 --python-from <自带 python/ 的 zip>")
    n = P.extract_runtime(zip_path, payload)
    print("Python 运行时：%d 个文件（来自 %s）" % (n, os.path.basename(zip_path)))

    # 安装版不要 portable.flag（数据该进 %APPDATA%，别写进 Program Files）
    flag = os.path.join(payload, "portable.flag")
    if os.path.isfile(flag):
        os.remove(flag)

    with open(os.path.join(payload, "使用说明.txt"), "w", encoding="utf-8-sig") as f:
        f.write(P.USAGE.format(ver=ver, port=8795, pyver=P.PY_VER))
    return payload


def write_license_txt():
    """Inno 的 LicenseFile 只认 .txt/.rtf"""
    src = os.path.join(ROOT, "LICENSE")
    dst = os.path.join(ROOT, "installer", "LICENSE.txt")
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            text = f.read()
        with open(dst, "w", encoding="utf-8-sig") as f:
            f.write(text)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-from", default="", help="提供 python/ 的 zip")
    ap.add_argument("--download", action="store_true", help="从 python.org 下 embeddable")
    ap.add_argument("--iscc", default="", help="ISCC.exe 路径")
    ap.add_argument("--keep-stage", action="store_true", help="保留中间文件")
    a = ap.parse_args()

    ver = P.read_version()
    iscc = find_iscc(a.iscc)
    print("版本 %s，编译器 %s" % (ver, iscc))

    stage_payload(ver, a.python_from, a.download)
    write_license_txt()

    os.makedirs(DIST, exist_ok=True)
    cmd = [iscc,
           "/DAppVer=%s" % ver,
           "/DPayloadDir=%s" % os.path.join(STAGE, "payload"),
           "/DOutputDir=%s" % DIST,
           "/Qp", ISS]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        print(tail[-3000:])
        raise SystemExit("Inno Setup 编译失败（退出码 %d）" % p.returncode)
    for line in tail.splitlines():
        if "Warning" in line or "warning" in line or "Error" in line:
            print("  " + line)

    exe = os.path.join(DIST, "追光几何-%s-安装版.exe" % ver)
    if not os.path.isfile(exe):
        raise SystemExit("编译似乎成功但没找到产物：%s" % exe)
    print("安装包：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))
    if not a.keep_stage:
        shutil.rmtree(STAGE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
