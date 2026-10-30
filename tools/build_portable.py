# -*- coding: utf-8 -*-
r"""打一个免安装的便携包：应用 + 自带 Python 运行时 + 安装脚本，解压即用。

为什么需要它：追光几何只依赖 Python 标准库，但目标电脑不一定有 Python，
也没有管理员权限去装。便携包自带 python\python.exe + pythonw.exe，
双击 `安装.bat` 就能建快捷方式、装命令行、设开机自启（全部只写当前用户）。

用法（在仓库根目录）：
    python tools/build_portable.py
    python tools/build_portable.py --python-from "dist/追光几何-1.0.0-portable.zip"
    python tools/build_portable.py --download          # 从 python.org 下 3.13 embeddable
    python tools/build_portable.py --no-zip            # 只生成目录，方便调试

产物：
    dist/追光几何-<版本>-portable/        目录（可直接拷到 U 盘）
    dist/追光几何-<版本>-portable.zip     压缩包
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
PY_VER = "3.13.7"
PY_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (PY_VER, PY_VER)

# 应用本体：这些进包
APP_FILES = ["zgr.py", "VERSION", "README.md", "启动追光几何.bat", "LICENSE"]
APP_DIRS = ["zgr", "web", "assets"]
# 原生桌面客户端：编出来的 exe + WebView2 组件，放在包根目录，
# 双击「追光几何.exe」就是一个正常的 Windows 窗口，不经过浏览器
DESKTOP_DIR = os.path.join(DIST, "desktop")
DESKTOP_FILES = ["追光几何.exe",
                 "Microsoft.Web.WebView2.Core.dll",
                 "Microsoft.Web.WebView2.Wpf.dll",
                 "WebView2Loader.dll"]
# 附带：安装脚本
EXTRA_FILES = [("tools/install.ps1", "tools/install.ps1"),
               ("tools/install.bat", "安装.bat")]
EXTRA_DIRS = []

SKIP_NAMES = {"__pycache__", ".git", ".pytest_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".tmp")

USAGE = """追光几何 {ver} · 便携版
=================================

一、最简单的用法（不安装任何东西）
  双击「追光几何.exe」→ 打开独立窗口（不是浏览器，没有地址栏、没有标签页）
  后台服务会自动起来；关窗口只是收到右下角托盘，实时同步继续跑。

二、装成常驻软件（推荐）
  双击「安装.bat」（或在 PowerShell 里跑 tools\\install.ps1）
  · 桌面/开始菜单出现「追光几何」
  · 任意终端能用 zgr 命令
  · 开机自动起后台服务（没有黑窗口）
  · 需要别的电脑连过来时：用管理员重跑一次，会自动加防火墙放行

三、这个包里有什么
  python\\      自带的 Python {pyver}（不碰系统里已装的 Python）
  zgr\\ web\\   追光几何本体
  userdata\\    绿色模式的数据目录（项目列表、缓存）；删掉就恢复出厂
  portable.flag 有这个文件 = 所有数据都写在本目录，不往 C 盘放东西

四、局域网里两台机器同步
  1. 两台都装好，服务都在跑（默认 8795）
  2. 打开一方界面 → 设置 → 实时同步 → 填对方地址（如 http://192.168.1.20:8795）
  3. 同网段会自动发现同名项目；也可以直接填地址

五、卸载
  · 删除桌面快捷方式
  · 删掉 %LOCALAPPDATA%\\ZhuiguangGeometry（命令行入口）
  · 删掉注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 里的 ZhuiguangGeometry
  · 整个目录删掉即可（数据在 userdata\\，想留就单独拷出来）
"""


def read_version() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="ascii") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def copy_tree(src: str, dst: str):
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        if name in SKIP_NAMES or name.endswith(SKIP_SUFFIX):
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            copy_tree(s, d)
        else:
            shutil.copy2(s, d)


def ensure_desktop() -> bool:
    """桌面客户端的 exe 必须先编出来；没有就调用 desktop/build.ps1 编一次。"""
    exe = os.path.join(DESKTOP_DIR, "追光几何.exe")
    if os.path.isfile(exe):
        return True
    script = os.path.join(ROOT, "desktop", "build.ps1")
    if not os.path.isfile(script):
        print("缺少桌面客户端：%s 和 %s 都不存在" % (exe, script))
        return False
    print("编译桌面客户端：%s" % script)
    rc = subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", script])
    if rc != 0 or not os.path.isfile(exe):
        print("桌面客户端编译失败（退出码 %s）" % rc)
        return False
    return True


def copy_desktop(into: str):
    """把 exe + WebView2 组件铺到软件根目录，双击即用"""
    os.makedirs(into, exist_ok=True)
    for name in DESKTOP_FILES:
        src = os.path.join(DESKTOP_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(into, name))
        else:
            print("提示：桌面客户端缺少 %s（这一份将无法双击启动）" % name)


def find_runtime_zip(explicit: str) -> str:
    """找一个能提供 python/ 的 zip：显式指定 > 老便携包 > 本地嵌入版压缩包"""
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit("指定的运行时压缩包不存在：%s" % explicit)
        return explicit
    for name in sorted(os.listdir(DIST)) if os.path.isdir(DIST) else []:
        p = os.path.join(DIST, name)
        if not name.lower().endswith(".zip"):
            continue
        try:
            with zipfile.ZipFile(p) as z:
                if any(n.startswith("python/python.exe") for n in z.namelist()):
                    return p
        except (OSError, zipfile.BadZipFile):
            continue
    for name in os.listdir(ROOT):
        if name.lower().startswith("python-") and "embed" in name.lower() and name.endswith(".zip"):
            return os.path.join(ROOT, name)
    return ""


def download_runtime(into: str) -> str:
    os.makedirs(into, exist_ok=True)
    dst = os.path.join(into, "python-%s-embed-amd64.zip" % PY_VER)
    if os.path.isfile(dst):
        return dst
    print("下载 Python %s embeddable …" % PY_VER)
    with urllib.request.urlopen(PY_URL, timeout=120) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    print("已下载：%s（%.1f MB）" % (dst, os.path.getsize(dst) / 1048576.0))
    return dst


def extract_runtime(zip_path: str, out_dir: str) -> int:
    """把 zip 里的 python/ 目录（或 embeddable 包的根）铺到 out_dir/python"""
    target = os.path.join(out_dir, "python")
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        has_prefix = any(n.startswith("python/") for n in names)
        for n in names:
            if n.endswith("/"):
                continue
            rel = n[len("python/"):] if has_prefix and n.startswith("python/") else n
            if not rel or rel.startswith("__MACOSX") or "/" in rel:
                continue            # embeddable 包是平铺的，只取根下文件
            with z.open(n) as src, open(os.path.join(target, rel), "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    # 关键：Python 3.13 的 embeddable 包默认把 import 限制在 python313.zip 里，
    # 用 _pth 显式放开当前目录，这样 zgr 包能被正常 import
    for name in os.listdir(target):
        if name.endswith("._pth"):
            with open(os.path.join(target, name), "w", encoding="ascii") as f:
                f.write("python313.zip\n.\n..\\\\\nimport site\n")
            break
    if not os.path.isfile(os.path.join(target, "python.exe")):
        raise SystemExit("这个压缩包里没有 python/python.exe：%s" % zip_path)
    return count


def write_launchers(out_dir: str, ver: str):
    # 绿色版标记：数据写在本目录
    open(os.path.join(out_dir, "portable.flag"), "w", encoding="ascii").close()
    with open(os.path.join(out_dir, "使用说明.txt"), "w", encoding="utf-8-sig") as f:
        f.write(USAGE.format(ver=ver, port=8795, pyver=PY_VER))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python-from", default="", help="提供 python/ 的 zip（老便携包即可）")
    ap.add_argument("--download", action="store_true", help="从 python.org 下载 embeddable")
    ap.add_argument("--out", default="", help="输出目录（默认 dist/追光几何-<版本>-portable）")
    ap.add_argument("--no-zip", action="store_true")
    a = ap.parse_args()

    ver = read_version()
    out_dir = a.out or os.path.join(DIST, "追光几何-%s-portable" % ver)
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    missing = [f for f in APP_FILES if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        print("缺少文件（先补上再打包）：", ", ".join(missing))

    for f in APP_FILES:
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, f))
        elif f == "LICENSE":
            print("提示：还没有 LICENSE 文件，开源源码里会有；便携包先跳过")
    for d in APP_DIRS:
        copy_tree(os.path.join(ROOT, d), os.path.join(out_dir, d))
    ensure_desktop()
    copy_desktop(out_dir)
    for src, dst in EXTRA_FILES:
        s = os.path.join(ROOT, src)
        if os.path.isfile(s):
            os.makedirs(os.path.dirname(os.path.join(out_dir, dst)), exist_ok=True)
            shutil.copy2(s, os.path.join(out_dir, dst))
    for src, dst in EXTRA_DIRS:
        s = os.path.join(ROOT, src)
        if os.path.isdir(s):
            copy_tree(s, os.path.join(out_dir, dst))
    zip_path = find_runtime_zip(a.python_from)
    if not zip_path and a.download:
        zip_path = download_runtime(DIST)
    if not zip_path:
        print("")
        print("找不到 Python 运行时。两种办法：")
        print("  1. 加 --download（从 python.org 拉 %s embeddable）" % PY_VER)
        print("  2. 加 --python-from <某个自带 python/ 的老便携包.zip>")
        print("（应用本体已经铺好在 %s，只差运行时）" % out_dir)
        return 2
    n = extract_runtime(zip_path, out_dir)
    print("Python 运行时：%s → %d 个文件（来自 %s）" % (os.path.basename(out_dir) + "\\python",
                                                    n, os.path.basename(zip_path)))
    write_launchers(out_dir, ver)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fn in os.walk(out_dir) for f in fn)
    print("目录：%s（%.1f MB）" % (out_dir, total / 1048576.0))
    if a.no_zip:
        return 0

    zip_out = os.path.join(DIST, "追光几何-%s-portable.zip" % ver)
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dp, _dn, fn in os.walk(out_dir):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, out_dir)
                z.write(full, rel)
    print("压缩包：%s（%.1f MB）" % (zip_out, os.path.getsize(zip_out) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
