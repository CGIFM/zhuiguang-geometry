# -*- coding: utf-8 -*-
r"""打一个标准的 Windows Installer（.msi）。

和安装版（Inno）的区别
----------------------
Inno 的 setup.exe 是自定义安装程序；MSI 是微软的系统级安装格式，
能被组策略 / Intune / SCCM 批量推送，也能 msiexec /i xx.msi /qn 静默装。
安装内容完全一样：装到 %LOCALAPPDATA%\Programs\ZhuiguangGeometry，
桌面图标直接指向原生客户端 追光几何.exe。

用法（在仓库根目录）：
    python tools/build_msi.py
    python tools/build_msi.py --wix C:/Tools\wix314
    python tools/build_msi.py --python-from dist/追光几何-1.0.0-portable.zip

产物：
    dist/追光几何-<版本>.msi
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_portable as P  # noqa: E402
import build_installer as I  # noqa: E402

ROOT = P.ROOT
DIST = P.DIST
STAGE = os.path.join(DIST, "msi")
WXS = os.path.join(ROOT, "installer", "zgr.wxs")


def find_wix(explicit: str) -> str:
    cands = [explicit] if explicit else []
    cands += [
        r"C:/Tools\wix314",
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WiX Toolset v3.14", "bin"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WiX Toolset v3.11", "bin"),
        r"C:\Program Files (x86)\WiX Toolset v3.14\bin",
    ]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "candle.exe")):
            return d
    raise SystemExit(
        "没找到 WiX（candle.exe / light.exe）。\n"
        "下载免安装版解压即可：\n"
        "  https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip\n"
        "然后用 --wix <解压目录> 指定。")


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        print(out[-4000:])
        raise SystemExit("命令失败（%d）：%s" % (p.returncode, os.path.basename(cmd[0])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wix", default="", help="WiX bin 目录（含 candle.exe）")
    ap.add_argument("--python-from", default="", help="提供 python/ 的 zip")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--keep-stage", action="store_true")
    a = ap.parse_args()

    ver = P.read_version()
    wix = find_wix(a.wix)
    print("版本 %s，WiX %s" % (ver, wix))

    # 1) 铺 payload：和安装版同一套内容（含桌面客户端 exe）
    payload = I.stage_payload(ver, a.python_from, a.download)
    print("payload：%d 个文件" % sum(len(f) for _d, _s, f in os.walk(payload)))

    work = os.path.join(STAGE, "work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    # 2) heat 把 payload 目录收成组件组
    heat_wxs = os.path.join(work, "payload.wxs")
    run([os.path.join(wix, "heat.exe"), "dir", payload,
         "-cg", "Payload", "-dr", "INSTALLFOLDER",
         "-gg", "-g1", "-sfrag", "-srd", "-sreg", "-scom", "-sx",
         "-var", "var.PayloadDir", "-out", heat_wxs])
    print("heat：ok")

    # 3) candle 编译
    objs = []
    for src in (WXS, heat_wxs):
        run([os.path.join(wix, "candle.exe"),
             "-arch", "x64",
             "-dAppVer=%s" % ver,
             "-dRepoRoot=%s" % ROOT,
             "-dPayloadDir=%s" % payload,
             "-out", os.path.join(work, os.path.splitext(os.path.basename(src))[0] + ".wixobj"),
             src])
        objs.append(os.path.join(work, os.path.splitext(os.path.basename(src))[0] + ".wixobj"))
    print("candle：ok")

    # 4) light 链成 msi
    msi = os.path.join(DIST, "追光几何-%s.msi" % ver)
    if os.path.isfile(msi):
        os.remove(msi)
    # 按用户安装（ALLUSERS=2 + MSIINSTALLPERUSER=1）时，ICE38/ICE64/ICE91 会要求
    # 每个用户目录下的组件改用 HKCU 注册表做 KeyPath；对整目录布包不适用，按惯例抑制。
    run([os.path.join(wix, "light.exe"),
         "-sice:ICE38", "-sice:ICE64", "-sice:ICE91",
         "-out", msi] + objs)
    print("light：ok")

    if not os.path.isfile(msi):
        raise SystemExit("没有产出 msi")
    print("安装包：%s（%.1f MB）" % (msi, os.path.getsize(msi) / 1048576.0))
    if not a.keep_stage:
        shutil.rmtree(STAGE, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
