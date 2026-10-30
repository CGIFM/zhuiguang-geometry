# -*- coding: utf-8 -*-
"""示例项目：一台新机器上「装着但不知道从哪儿开始」时，一键给一个能点开的项目。

为什么需要它：两台上线后项目列表一直是空的——不是功能不够，是**第一次打开见到的是
一片空白加一个按钮**，谁也不会拿自己正在做的图纸去试第一个按钮。示例项目把
「记录一版 → 改了东西 → 有第二版 → 能对比、能恢复」这一整套先演一遍，
用户拿它试手，试完删掉也不心疼。

内容全是用标准库现造的纯文本文件（含一个最小 DXF），不涉及任何真实图纸。
"""
from __future__ import annotations

import os
import tempfile

from .store import Repo, RepoError

DEMO_NAME = "追光几何-示例项目"


def _dxf_rect(w: float, h: float) -> str:
    """最小可用的 DXF（四条 LINE 围成闭合矩形），预览引擎能直接画出来"""
    pts = [(0, 0), (w, 0), (w, h), (0, h), (0, 0)]
    out = ["0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"]
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        out.append("0\nLINE\n8\n轮廓\n10\n%.3f\n20\n%.3f\n11\n%.3f\n21\n%.3f\n"
                   % (x1, y1, x2, y2))
    out.append("0\nENDSEC\n0\nEOF\n")
    return "".join(out)


def _files() -> dict:
    """示例项目里的文件：{相对路径: 内容}"""
    return {
        "说明.md": (
            "# 示例项目\n\n"
            "这是追光几何自动建的示例，用来试手，改坏了也无所谓。\n\n"
            "试这三件事：\n\n"
            "1. 点右上角「立即记录」——把现在的状态记成一版；\n"
            "2. 改一下 `零件/侧板.sldprt` 里的数字，再看左边时间线；\n"
            "3. 右键任意一版「恢复这一版」——工作区会变回去，恢复前自动留备份。\n\n"
            "试完在项目上右键「从列表移除」即可，文件本身不会被删。\n"),
        "零件/底板.sldprt": "底板\n材质: 45#钢\n厚度: 12 mm\n长: 320 mm\n宽: 240 mm\n",
        "零件/侧板.sldprt": "侧板\n材质: 6061-T6\n厚度: 8 mm\n孔距: 160 mm\n",
        "零件/立柱.sldprt": "立柱\n材质: Q235\n长度: 450 mm\n",
        "零件/内六角螺钉M6x20.sldprt": "内六角圆柱头螺钉\n规格: M6×20\n等级: 8.8\n",
        "装配体/机架.sldasm": (
            "机架装配\n"
            "  零件/底板.sldprt ×1\n"
            "  零件/侧板.sldprt ×2\n"
            "  零件/立柱.sldprt ×4\n"
            "  零件/内六角螺钉M6x20.sldprt ×16\n"
            "备注: 侧板先装，立柱后上\n"),
        "图纸/底板轮廓.dxf": _dxf_rect(320.0, 240.0),
        "BOM/机架.csv": (
            "编码,名称,规格,数量,材质\n"
            "P-001,底板,320×240×12,1,45#钢\n"
            "P-002,侧板,240×160×8,2,6061-T6\n"
            "P-003,立柱,Φ30×450,4,Q235\n"
            "P-004,内六角螺钉,M6×20,16,不锈钢\n"),
    }


def demo_root() -> str:
    """示例项目放在哪：临时目录下（不是用户自己的图纸目录）"""
    return os.path.join(tempfile.gettempdir(), DEMO_NAME)


def make_demo(reuse: bool = True) -> Repo:
    """建（或复用）示例项目，返回已初始化并有两个版本的仓库。

    只在这个固定目录里动手；目录已经是项目就原样返回，绝不重复提交、
    也绝不碰用户自己选的位置。
    """
    root = demo_root()
    repo = Repo(root)
    if repo.exists:
        return repo

    if os.path.isdir(root) and os.listdir(root):
        # 同名目录里已经有别的东西（用户自己建的）：不覆盖，换一个名字
        root = os.path.join(tempfile.gettempdir(), DEMO_NAME + "-2")
        repo = Repo(root)
        if repo.exists:
            return repo
        if os.path.isdir(root) and os.listdir(root):
            raise RepoError("示例项目目录已被占用：%s" % root)

    repo.init(DEMO_NAME)
    files = _files()
    for rel, text in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    repo.commit("示例项目：初始版本", "示例")

    # 再来一版，时间线上能看出「改了哪 2 个文件」，对比/恢复才有东西可看
    for rel, text in (("零件/侧板.sldprt",
                       "侧板\n材质: 6061-T6\n厚度: 10 mm\n孔距: 160 mm\n"),
                      ("BOM/机架.csv", files["BOM/机架.csv"].replace(
                          "240×160×8,2", "240×160×10,2"))):
        p = os.path.join(root, *rel.split("/"))
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    repo.commit("侧板加厚到 10 mm", "示例")
    return repo
