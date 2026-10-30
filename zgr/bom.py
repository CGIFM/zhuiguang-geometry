# -*- coding: utf-8 -*-
"""追光几何 · 装配体物料清单（BOM）

做一件事：拿一个 SolidWorks 装配体，把里面所有零件按「图号 + 名称 + 数量 +
材料 + 重量」列成一张表，直接存成 Excel（.xlsx）和 CSV。

为什么不用第三方库
------------------
本项目一直只用标准库。xlsx 本质是个 zip 里放几段 XML，这里自己写——不装
openpyxl，也就不会因为依赖版本把软件弄坏。

怎么拿到数据
------------
和批量导 STEP 一样，靠 VBScript 驱动本机 SolidWorks（`cscript`）：
打开装配体 → 逐层遍历零件与子装配 → 把每个零件的自定义属性读出来。
**只读，不修改、不保存、不关闭用户正在编辑的文档**：打开的是我们自己的
会话，每读完一个就关掉。

数量怎么算
----------
SolidWorks 的 GetComponents 是按「每一处实例」给的，子装配里的零件会被
重复数到。所以遍历时把父级数量乘下来：一个子装配装了 4 份，它里面的
螺丝在总表里就是 4 × 内部数量。按图号合并时再把同图号的加起来。

列可以改
--------
`columns` 是一串 (表头, 字段名) 的对应。默认照**嘉立创 Ican 工具箱**的 BOM 模板：
序号 / 图号 / 零件名称 / 材质 / 表面处理 / 品牌 / 零件类型 / 数量 / 单位，
表头上方还能带它那两行「项目名称 / 下单时间、客户 / 交期」信息块。
要更细的工程表（层级、单重、总重）就用 `FULL_COLUMNS`，
或者项目根目录放 `bom-template.json` 自己定——只改表，不动代码。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import zipfile

#: 同一台机器同一时刻只让一个任务驱动 SolidWorks（和导 STEP 共用那把锁）
_BOM_LOCK = threading.Lock()

ASSEMBLY_EXT = (".sldasm",)

#: 嘉立创 Ican 工具箱的 BOM 表头（取自它的模板 Ican工具箱BOM模板.xlsx 第 4 行）
#: 序号 / 图号 / 零件名称 / 材质 / 表面处理 / 品牌 / 零件类型 / 数量 / 单位
ICAN_COLUMNS = [
    ("序号", "no"),
    ("图号", "code"),
    ("零件名称", "name"),
    ("材质", "material"),
    ("表面处理", "surface"),
    ("品牌", "brand"),
    ("零件类型", "ptype"),
    ("数量", "qty"),
    ("单位", "unit"),
]

#: 默认用 Ican 的格式（用户要的就是照它的表来）；要更细的自己写模板
DEFAULT_COLUMNS = ICAN_COLUMNS

#: 工程用的一套：多了层级、单重、总重、备注
FULL_COLUMNS = [
    ("序号", "no"),
    ("层级", "level"),
    ("图号", "code"),
    ("名称", "name"),
    ("数量", "qty"),
    ("材料", "material"),
    ("表面处理", "surface"),
    ("品牌", "brand"),
    ("零件类型", "ptype"),
    ("单位", "unit"),
    ("单重(kg)", "unit_mass"),
    ("总重(kg)", "total_mass"),
    ("备注", "note"),
]

#: 照抄 Ican 模板的前三行（标题 + 项目信息块），放模板文件里用
ICAN_TITLE = "嘉立创Ican工具箱"
ICAN_HEADER_ROWS = [
    ["项目名称", "", "", "", "", "", "下单时间", "", ""],
    ["客户", "", "", "", "", "", "交期", "", ""],
]

#: 模板文件放在项目里就自动用它的列（不改代码就能对齐别的工具格式）
TEMPLATE_NAME = "bom-template.json"


class BomError(Exception):
    pass


# --------------------------------------------------------------- SolidWorks
_VBS = r"""' 追光几何 BOM 采集：只读遍历装配体，按行输出
Option Explicit
Dim fso, sw, jf, asmPath, model, maxDepth, progIds
Set fso = CreateObject("Scripting.FileSystemObject")

asmPath = ""
maxDepth = 8
progIds = "SldWorks.Application"
If WScript.Arguments.Count >= 1 Then
  Dim f : Set f = fso.OpenTextFile(WScript.Arguments(0), 1, False, -1)
  If Not f.AtEndOfStream Then asmPath = f.ReadLine
  If Not f.AtEndOfStream Then
    Dim d : d = f.ReadLine
    If IsNumeric(d) Then maxDepth = CInt(d)
  End If
  If Not f.AtEndOfStream Then progIds = f.ReadLine   ' 从新到旧，逗号分隔
  f.Close
End If
If Len(asmPath) = 0 Then
  WScript.Echo "FATAL no-input"
  WScript.Quit 2
End If

' 先看这台机器是不是已经开着 SolidWorks：开着就**只借不接管**
' （不改它的可见性、不关它别的文档、不退出它——用户可能正画着图）
'
' ProgID 从新到旧逐个试：默认的 SldWorks.Application 指向安装顺序里**最早**那版，
' 装了两版的机器（174 就是 2021 + 2024）拿旧版去开新版存的零件，SolidWorks 只会
' 说「打不开这个模型」。旧版排最后当兜底。
Dim hadInst, oldCIP, ids, k, prog
hadInst = False
oldCIP = False
ids = Split(progIds, ",")
For k = 0 To UBound(ids)
  prog = Trim(ids(k))
  If Len(prog) = 0 Then
    prog = "SldWorks.Application"
  End If
  Set sw = Nothing
  On Error Resume Next
  Set sw = GetObject(, prog)
  If Err.Number = 0 And Not (sw Is Nothing) Then
    hadInst = True
    WScript.Echo "SW-INSTANCE " & prog
  Else
    Set sw = Nothing        ' GetObject 失败时 sw 是 Empty，Is 比较会报 424
  End If
  Err.Clear
  On Error GoTo 0
  If Not (sw Is Nothing) Then Exit For
Next
If sw Is Nothing Then
  For k = 0 To UBound(ids)
    prog = Trim(ids(k))
    If Len(prog) = 0 Then
      prog = "SldWorks.Application"
    End If
    Set sw = Nothing
    On Error Resume Next
    Set sw = CreateObject(prog)
    If Err.Number <> 0 Then Set sw = Nothing
    Err.Clear
    On Error GoTo 0
    If Not (sw Is Nothing) Then
      WScript.Echo "SW-CREATED " & prog
      Exit For
    End If
  Next
End If
If sw Is Nothing Then
  WScript.Echo "FATAL create none"
  WScript.Quit 2
End If
If hadInst Then oldCIP = sw.CommandInProgress
If Not hadInst Then
  sw.Visible = False
  sw.UserControl = False
End If
sw.CommandInProgress = True
On Error GoTo 0

' 这份装配体在打开之前是不是**本来就在**（用户自己开的）：
' OpenDoc 对已经开着的文档会直接返回手上这一份，所以收尾时如果照旧 CloseDoc，
' 关掉的就是用户正在编辑的图——有未保存改动时更糟。只关我们自己打开的那一份。
Dim wasOpen
wasOpen = False
On Error Resume Next
Dim already
Set already = sw.GetOpenDocumentByName(asmPath)
If Err.Number = 0 And Not (already Is Nothing) Then wasOpen = True
Err.Clear
Set already = Nothing
On Error GoTo 0

On Error Resume Next
Set model = sw.OpenDoc(asmPath, 2)
If model Is Nothing Then Set model = sw.OpenDoc(asmPath, 1)
If model Is Nothing Then Set model = sw.LoadFile2(asmPath, "r")
On Error GoTo 0
If model Is Nothing Then
  WScript.Echo "FATAL open " & Err.Number & " " & Err.Description
  sw.CommandInProgress = oldCIP
  If Not hadInst Then sw.ExitApp
  WScript.Quit 3
End If

WScript.Echo "ROOT|" & model.GetTitle() & "|" & asmPath

Call Walk(model, 0, 1.0, "0")

' 收尾：只关我们自己开的这一个文档。**绝不** CloseAllDocuments / 退出用户的实例
' 本来就在开着的（wasOpen）一律不关：那是用户的图，不是我们的。
On Error Resume Next
If Not wasOpen Then sw.CloseDoc model.GetTitle()
sw.CommandInProgress = oldCIP
If Not hadInst Then sw.ExitApp
WScript.Echo "DONE"
WScript.Quit 0

' 遍历装配体：走特征树的链表（FirstFeature / GetNextFeature）
'
' 为什么不用 GetComponents：它返回的是 SAFEARRAY(Object)，VBScript / JScript 都
' 取不出元素（Set c = comps(i) 直接「类型不匹配」），实测 SolidWorks 2024 上整表为空。
' 特征树是「取一个、再取下一个」的链表，每个组件是一类 Reference 特征，
' GetSpecificFeature2() 拿到的就是 Component2——这条路在 VBScript 里是通的。
Sub Walk(asmDoc, depth, mult, prefix)
  If depth > maxDepth Then Exit Sub
  Dim feat, comp, tn, idx, path
  idx = 0
  Set feat = asmDoc.FirstFeature
  Do While Not (feat Is Nothing)
    tn = ""
    On Error Resume Next
    tn = feat.GetTypeName2()
    On Error GoTo 0
    If tn = "Reference" Then                    ' 装配体里的一个零件/子装配实例
      idx = idx + 1
      path = prefix & "." & CStr(idx)
      Set comp = Nothing
      On Error Resume Next
      Set comp = feat.GetSpecificFeature2()
      On Error GoTo 0
      If comp Is Nothing Then
        ' 引用丢了也尽量把名字带出来（Reference 特征的 Name 就是组件名，
        ' 比如「底板-1」）。用户要的是「哪几个零件没了」，不是一个数字。
        Dim fname
        fname = ""
        On Error Resume Next
        fname = feat.Name
        On Error GoTo 0
        WScript.Echo "MISSING|" & depth & "|" & path & "|" & fname
      Else
        Dim sup
        sup = False
        On Error Resume Next
        sup = comp.IsSuppressed()
        On Error GoTo 0
        If Not sup Then Call Emit(comp, depth, mult, path)
      End If
    End If
    Set feat = feat.GetNextFeature()
  Loop
End Sub

Sub Emit(comp, depth, mult, path)
  Dim nm, pth, isAsm, refDoc, qty, docType
  On Error Resume Next
  nm = comp.Name2
  pth = comp.GetPathName()
  On Error GoTo 0
  If Len(pth) = 0 Then Exit Sub
  qty = 1

  Set refDoc = Nothing
  On Error Resume Next
  Set refDoc = comp.GetModelDoc2()
  On Error GoTo 0
  If refDoc Is Nothing Then
    On Error Resume Next
    Set refDoc = comp.IGetModelDoc()
    On Error GoTo 0
  End If

  If Not refDoc Is Nothing Then
    On Error Resume Next
    docType = refDoc.GetType()
    On Error GoTo 0
    If docType = 2 Then                     ' 2 = 装配体
      WScript.Echo "ASM|" & depth & "|" & mult & "|" & path & "|" & nm & "|" & pth
      Call Walk(refDoc, depth + 1, mult, path)
      Exit Sub
    End If
  End If

  Dim code, title, material, surface, note, mass, brand, ptype, unit
  code = "" : title = "" : material = "" : surface = "" : note = "" : mass = ""
  brand = "" : ptype = "" : unit = ""
  If Not refDoc Is Nothing Then
    code = Prop(refDoc, "图号")
    If Len(code) = 0 Then code = Prop(refDoc, "零件号")
    If Len(code) = 0 Then code = Prop(refDoc, "PartNo")
    If Len(code) = 0 Then code = Prop(refDoc, "Number")
    title = Prop(refDoc, "名称")
    If Len(title) = 0 Then title = Prop(refDoc, "Description")
    material = Prop(refDoc, "材料")
    If Len(material) = 0 Then
      On Error Resume Next
      Dim db, cfg
      cfg = refDoc.ConfigurationManager.ActiveConfiguration.Name
      material = refDoc.GetMaterialPropertyName2(cfg, db)
      On Error GoTo 0
    End If
    surface = Prop(refDoc, "表面处理")
    note = Prop(refDoc, "备注")
    mass = Prop(refDoc, "重量")
    If Len(mass) = 0 Then mass = Prop(refDoc, "质量")
    If Len(mass) = 0 Then mass = Prop(refDoc, "Weight")
    brand = Prop(refDoc, "品牌")
    If Len(brand) = 0 Then brand = Prop(refDoc, "Brand")
    If Len(brand) = 0 Then brand = Prop(refDoc, "厂家")
    ptype = Prop(refDoc, "零件类型")
    If Len(ptype) = 0 Then ptype = Prop(refDoc, "类型")
    If Len(ptype) = 0 Then ptype = Prop(refDoc, "PartType")
    unit = Prop(refDoc, "单位")
    If Len(unit) = 0 Then unit = Prop(refDoc, "Unit")
  End If
  If Len(title) = 0 Then
    Dim base
    base = fso.GetBaseName(pth)
    title = base
  End If

  WScript.Echo "PART|" & depth & "|" & mult & "|" & path & "|" & nm & "|" & pth & _
      "|" & code & "|" & title & "|" & material & "|" & surface & "|" & mass & "|" & note & _
      "|" & brand & "|" & ptype & "|" & unit
End Sub

' 读一个自定义属性：先按当前配置，再按全局
Function Prop(doc, keyName)
  Dim v
  v = ""
  On Error Resume Next
  Dim cpm
  Set cpm = doc.Extension.CustomPropertyManager("")
  If Not cpm Is Nothing Then
    Dim a, b, c
    a = "" : b = "" : c = ""
    cpm.Get4 keyName, False, a, b
    v = b
    If Len(v) = 0 Then
      cpm.Get4 keyName, True, a, c
      v = c
    End If
  End If
  Err.Clear
  On Error GoTo 0
  Prop = Replace(Replace(v, vbCr, " "), vbLf, " ")
End Function
"""


def _cscript() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "cscript.exe")


def _run_vbs(vbs: str, job: str, timeout: float):
    """跑一次 VBS，逐行收输出；超时就杀掉（宁可这一版报错，也不能挂着）"""
    cs = _cscript()
    if not os.path.isfile(cs):
        raise BomError("找不到 cscript.exe，无法驱动 SolidWorks")
    proc = subprocess.Popen([cs, "//nologo", "//T:600", vbs, job],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="mbcs", errors="replace")
    lines, deadline = [], time.time() + timeout
    try:
        for line in proc.stdout:
            lines.append((line or "").rstrip("\r\n"))
            if time.time() > deadline:
                raise BomError("读装配体超时（%d 秒），SolidWorks 可能弹了对话框"
                               % int(timeout))
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        except Exception:                                          # noqa: BLE001
            pass
    return lines


# --------------------------------------------------------------- 解析
def _num(x, default=""):
    try:
        v = float(str(x).replace(",", "").strip())
        return v
    except (TypeError, ValueError):
        return default


def _fmt_mass(v):
    if v == "":
        return ""
    n = _num(v)
    if n == "":
        return str(v)
    return round(n, 4)


def parse_lines(lines):
    """把 VBS 的 PART| / ASM| 行收成记录表（带父级数量的乘法）"""
    rows, root = [], ""
    stack = {}            # 层级路径 -> 该子装配的累计数量
    for line in lines:
        if not line:
            continue
        if line.startswith("ROOT|"):
            parts = line.split("|", 2)
            root = parts[1] if len(parts) > 1 else ""
            continue
        if line.startswith("FATAL"):
            raise BomError("读装配体失败：" + line[5:].strip())
        if line.startswith("MISSING|"):
            parts = line.split("|")
            rows.append({"kind": "missing",
                         "depth": int(_num(parts[1], 0) or 0),
                         "path": parts[2] if len(parts) > 2 else "",
                         "name": parts[3] if len(parts) > 3 else ""})
            continue
        if not (line.startswith("PART|") or line.startswith("ASM|")):
            continue
        parts = line.split("|")
        kind = parts[0]
        if len(parts) < 7:
            continue
        depth = int(_num(parts[1], 0) or 0)
        mult = _num(parts[2], 1.0) or 1.0
        path = parts[3]
        name = parts[4]
        full = parts[5]
        parent = ".".join(path.split(".")[:-1])
        # 父级数量：顶层是 1；子装配里再乘上父级装了几份
        base_qty = 1.0
        if parent:
            base_qty = stack.get(parent, 1.0)
        qty = base_qty * mult
        if kind == "ASM":
            stack[path] = qty
            rows.append({"kind": "asm", "depth": depth, "path": path,
                         "name": name, "file": full, "qty": qty})
            continue
        rows.append({
            "kind": "part", "depth": depth, "path": path, "name": name,
            "file": full, "qty": qty,
            "code": parts[6] if len(parts) > 6 else "",
            "title": parts[7] if len(parts) > 7 else "",
            "material": parts[8] if len(parts) > 8 else "",
            "surface": parts[9] if len(parts) > 9 else "",
            "mass": parts[10] if len(parts) > 10 else "",
            "note": parts[11] if len(parts) > 11 else "",
            "brand": parts[12] if len(parts) > 12 else "",
            "ptype": parts[13] if len(parts) > 13 else "",
            "unit": parts[14] if len(parts) > 14 else "",
        })
    return root, rows


def build_table(rows, include_children=True, merge_by="code", columns=None):
    """把零件记录整理成最终表格行（按列定义输出）"""
    columns = columns or DEFAULT_COLUMNS
    items = [r for r in rows if r["kind"] == "part"]
    if not include_children:
        items = [r for r in items if r["depth"] == 0]

    merged, order = {}, []
    for r in items:
        if merge_by == "none":
            key = r["path"]
        elif merge_by == "name":
            key = os.path.basename(r["file"]).lower()
        else:                                   # code：没有图号就退回文件名
            key = (r.get("code") or "").strip().lower() or os.path.basename(r["file"]).lower()
        if key not in merged:
            merged[key] = {
                "code": r.get("code") or "",
                "name": r.get("title") or os.path.splitext(os.path.basename(r["file"]))[0],
                "qty": 0.0,
                "material": r.get("material") or "",
                "surface": r.get("surface") or "",
                "brand": r.get("brand") or "",
                "ptype": r.get("ptype") or "",
                "unit": r.get("unit") or "",
                "note": r.get("note") or "",
                "unit_mass": _num(r.get("mass")),
                "level": "顶层" if r["depth"] == 0 else "子件",
                "file": r["file"],
                "paths": [r["path"]],
            }
            order.append(key)
        row = merged[key]
        row["qty"] += r["qty"]
        if r["path"] not in row["paths"]:
            row["paths"].append(r["path"])
        for fld in ("material", "surface", "brand", "ptype", "unit"):
            if not row.get(fld) and r.get(fld):
                row[fld] = r[fld]

    out = []
    for i, key in enumerate(order, 1):
        r = merged[key]
        unit = r.get("unit_mass") if r.get("unit_mass") != "" else ""
        total = (unit * r["qty"]) if unit != "" else ""
        out.append({
            "no": i,
            "level": r.get("level", ""),
            "code": r.get("code", ""),
            "name": r.get("name", ""),
            "qty": (int(r["qty"]) if float(r["qty"]).is_integer() else round(r["qty"], 3)),
            "material": r.get("material", ""),
            "surface": r.get("surface", ""),
            "brand": r.get("brand", ""),
            "ptype": r.get("ptype", ""),
            "unit": r.get("unit", ""),
            "unit_mass": _fmt_mass(unit),
            "total_mass": _fmt_mass(total),
            "note": r.get("note", ""),
            "file": r.get("file", ""),
            "paths": r.get("paths", []),
            "_key": key,
        })
    return columns, out


def load_template(project_root: str):
    """项目里有 bom-template.json 就用它（列名、顺序、表头块照它的来）

    返回 dict：`columns`（(表头, 字段) 列表）、`title`、`header`（表头前几行）。
    只写 `columns` 数组、或者直接给数组的老写法照样能用。
    """
    p = os.path.join(project_root, TEMPLATE_NAME)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    cols = data.get("columns") if isinstance(data, dict) else data
    out = []
    for c in cols or []:
        if isinstance(c, (list, tuple)) and len(c) == 2:
            out.append((str(c[0]), str(c[1])))
        elif isinstance(c, dict) and c.get("title") and c.get("field"):
            out.append((str(c["title"]), str(c["field"])))
    if not out:
        return None
    res = {"columns": out, "title": "", "header": []}
    if isinstance(data, dict):
        res["title"] = str(data.get("title") or "")
        head = data.get("header") or []
        if isinstance(head, list):
            for row in head:
                if isinstance(row, list):
                    res["header"].append(["" if v is None else str(v) for v in row])
    return res


# --------------------------------------------------------------- 写文件
def _xml_esc(s) -> str:
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyBorder="1" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" applyBorder="1" applyFont="1" xfId="0"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def write_xlsx(path: str, columns, rows, sheet_name: str = "BOM", title: str = "",
               prelude=None):
    """把表格写成 xlsx（只用标准库）

    `prelude` 是表头前要写的几行（照抄别的工具的模板时用，例如 Ican 的
    「项目名称 / 下单时间」信息块），每行是一个字符串列表。
    """
    ncol = len(columns)
    xml = io.StringIO()
    xml.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    xml.write('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
    widths = "".join(
        '<col min="%d" max="%d" width="%d" customWidth="1"/>'
        % (i + 1, i + 1, 10 if f in ("no", "qty", "level") else 22)
        for i, (_t, f) in enumerate(columns))
    xml.write("<cols>%s</cols><sheetData>" % widths)
    r = 0
    if title:
        r += 1
        xml.write('<row r="%d">' % r)
        xml.write('<c r="A%d" s="1" t="inlineStr"><is><t>%s</t></is></c>'
                  % (r, _xml_esc(title)))
        xml.write("</row>")
    for prow in prelude or []:
        r += 1
        xml.write('<row r="%d">' % r)
        for i, v in enumerate(prow):
            if v in ("", None):
                continue
            xml.write('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'
                      % (_col_name(i), r, _xml_esc(v)))
        xml.write("</row>")
    r += 1
    xml.write('<row r="%d">' % r)
    for i, (t, _f) in enumerate(columns):
        xml.write('<c r="%s%d" s="1" t="inlineStr"><is><t>%s</t></is></c>'
                  % (_col_name(i), r, _xml_esc(t)))
    xml.write("</row>")
    for row in rows:
        r += 1
        xml.write('<row r="%d">' % r)
        for i, (_t, field) in enumerate(columns):
            v = row.get(field, "")
            cell = "%s%d" % (_col_name(i), r)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                xml.write('<c r="%s"><v>%s</v></c>' % (cell, v))
            else:
                if v in ("", None):
                    continue
                xml.write('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>'
                          % (cell, _xml_esc(v)))
        xml.write("</row>")
    xml.write("</sheetData></worksheet>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   "</Relationships>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
                   % _xml_esc(sheet_name[:31] or "BOM"))
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", xml.getvalue())
    return path


def write_csv(path: str, columns, rows):
    """CSV 带 BOM，双击用 Excel 打开中文不乱码"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([t for t, _f in columns])
        for row in rows:
            w.writerow([row.get(fld, "") for _t, fld in columns])
    return path


# --------------------------------------------------------------- 入口
def _progids() -> str:
    """已装的 SolidWorks ProgID，从新到旧，逗号分隔（装两版时不能拿旧版开新文件）"""
    try:
        from . import mesh
        return ",".join(mesh.sldworks_progids())
    except Exception:                                           # noqa: BLE001
        return "SldWorks.Application"


def collect(assembly: str, timeout: float = 600.0, max_depth: int = 8):
    """驱动 SolidWorks 读一个装配体，返回 (根名称, 记录列表)"""
    if os.name != "nt":
        raise BomError("生成 BOM 需要装了 SolidWorks 的 Windows")
    if not os.path.isfile(assembly):
        raise BomError("找不到装配体文件：%s" % os.path.basename(assembly))
    if not assembly.lower().endswith(ASSEMBLY_EXT):
        raise BomError("只支持 .sldasm 装配体")
    tmp = tempfile.mkdtemp(prefix="zgr-bom-")
    vbs = os.path.join(tmp, "bom.vbs")
    job = os.path.join(tmp, "job.txt")
    with open(vbs, "w", encoding="ascii", errors="replace") as f:
        f.write(_VBS)
    with open(job, "w", encoding="utf-16-le", newline="\r\n") as f:
        f.write(assembly + "\n%d\n%s" % (max_depth, _progids()))
    try:
        lines = _run_vbs(vbs, job, timeout)
    finally:
        for p in (vbs, job):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    return parse_lines(lines)


# ---------------------------------------------------------------- 和上一次对比
# 制造业真正关心的不是「这一版有哪些料」，而是「这次改完多了哪些料」。
# 每次生成都留一份快照在项目 .zgr/bom/ 下（.zgr 本来就不进版本），
# 下次生成时自动和上一份比，把新增/删除/数量变化的行报出来。
SNAPSHOT_KEEP = 20


def _snap_file(project_root: str, assembly: str) -> str:
    rel = os.path.relpath(assembly, project_root).replace("\\", "/").lower()
    h = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
    return os.path.join(project_root, ".zgr", "bom", h + ".json")


def load_snapshots(project_root: str, assembly: str):
    """这个装配体之前生成过的清单快照（旧 → 新）"""
    p = _snap_file(project_root, assembly)
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def make_snapshot(rows, assembly="", project_root="", mode=None):
    """把一次生成的结果压成快照：只留比对要用的几个字段

    `mode` 记下这次的合并方式（按图号 / 按名称 / 一行一个实例）和「是否含子件」——
    两次口径不一样时行键根本对不上，硬比会把整张表报成「全新增」，所以要比对口径。"""
    return {
        "time": int(time.time() * 1000),
        "assembly": (os.path.relpath(assembly, project_root).replace("\\", "/")
                     if project_root else assembly),
        "mode": mode or {},
        "rows": [{"key": r.get("_key") or r.get("code") or r.get("name"),
                  "code": r.get("code") or "", "name": r.get("name") or "",
                  "qty": r.get("qty"), "unit": r.get("unit") or ""} for r in rows],
    }


def save_snapshot(project_root: str, assembly: str, snap):
    """留一份快照，只保留最近 SNAPSHOT_KEEP 份（旧的最先丢）"""
    p = _snap_file(project_root, assembly)
    snaps = load_snapshots(project_root, assembly)
    snaps.append(snap)
    snaps = snaps[-SNAPSHOT_KEEP:]
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".zgr-part"
        with io.open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(snaps, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except OSError:
        return None
    return p


#: BOM 里靠自定义属性填的列。缺了不是报错——只是这几列空着，图纸给出去对方看不出是什么料。
ATTR_FIELDS = (("code", "图号"), ("material", "材质"),
               ("brand", "品牌"), ("surface", "表面处理"))


def summarize_missing(rows, fields=ATTR_FIELDS, sample=20):
    """统计哪些行没填图号/材质/品牌/表面处理，并带上几个文件名让人去补

    这几个列全靠 SolidWorks 里的自定义属性。真实现场大部分零件都没填，
    于是 BOM 导出去一片空白——与其让用户自己一行行找，不如直接点名。"""
    out = []
    for fld, label in fields:
        miss = [r for r in rows if not str(r.get(fld) or "").strip()]
        if not miss:
            continue
        out.append({
            "field": fld, "label": label, "n": len(miss),
            "files": [r.get("file") or r.get("name") or "" for r in miss[:sample]],
        })
    return out


_MODE_NAMES = {"code": "按图号合并", "name": "按名称合并", "none": "一行一个实例"}


def _name_tail(names, n=5):
    """把「哪几个」拼成一句话：「：底板-1、侧板-2 等 3 个」"""
    names = [x for x in (names or []) if x]
    if not names:
        return ""
    head = "、".join(names[:n])
    more = len(names) - n
    return "：" + head + ("等 %d 个" % len(names) if more > 0 else "")


def _mode_name(mode):
    if not isinstance(mode, dict):
        return "未知"
    s = _MODE_NAMES.get(mode.get("merge_by") or "code", str(mode.get("merge_by")))
    if mode.get("include_children") is False:
        s += "、只要顶层"
    return s


def diff_snapshots(old, new):
    """按行键比两份快照：新增 / 删除 / 数量变化（顺序按新清单）"""
    old = old or {}
    new = new or {}
    om = {r.get("key"): r for r in (old.get("rows") or [])}
    nm = {r.get("key"): r for r in (new.get("rows") or [])}
    added = [nm[k] for k in nm if k not in om]
    removed = [om[k] for k in om if k not in nm]
    changed = []
    for k, r in nm.items():
        o = om.get(k)
        if o is not None and o.get("qty") != r.get("qty"):
            changed.append({"key": k, "code": r.get("code") or "",
                            "name": r.get("name") or "",
                            "old": o.get("qty"), "new": r.get("qty"),
                            "unit": r.get("unit") or ""})
    return {"added": added, "removed": removed, "changed": changed,
            "old_time": old.get("time")}


def export(assembly: str, dest_dir: str, include_children=True,
           merge_by="code", columns=None, project_root="", title="",
           timeout: float = 600.0, log=print):
    """生成 xlsx + csv，返回表头、行、文件路径和统计"""
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    prelude = None
    tpl = load_template(project_root) if columns is None else None
    if columns is None:
        columns = (tpl or {}).get("columns") or DEFAULT_COLUMNS
    if tpl:
        prelude = tpl.get("header") or None
        if not title:
            title = tpl.get("title") or ""

    with _BOM_LOCK:
        root, records = collect(assembly, timeout=timeout)
    columns, rows = build_table(records, include_children=include_children,
                                merge_by=merge_by, columns=columns)
    missing_rows = [r for r in records if r["kind"] == "missing"]
    missing = len(missing_rows)
    missing_names = [r.get("name") or r.get("path") or "" for r in missing_rows]
    if not rows:
        if missing:
            raise BomError("装配体里有 %d 个零件读不出来（引用丢失或文件被移动）%s，"
                           "先在 SolidWorks 里修复引用再出清单"
                           % (missing, _name_tail(missing_names)))
        raise BomError("这个装配体里没有可统计的零件（可能全是压缩的，或在子装配里）")

    stem = os.path.splitext(os.path.basename(assembly))[0]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r'[\\/:*?"<>|]+', "_", stem)[:60] or "装配体"
    xlsx = os.path.join(dest_dir, "%s-BOM-%s.xlsx" % (safe, stamp))
    csv_path = os.path.join(dest_dir, "%s-BOM-%s.csv" % (safe, stamp))
    write_xlsx(xlsx, columns, rows, sheet_name=safe[:20] or "BOM",
               title=title or ("%s 物料清单" % root if root else "物料清单"),
               prelude=prelude)
    write_csv(csv_path, columns, rows)
    total_qty = sum(r["qty"] for r in rows
                    if isinstance(r.get("qty"), (int, float)))

    # 和上一次比：多了哪些料、少了哪些、数量变了多少。
    # 快照放在项目的 .zgr 里——它跟着项目走，但从来不进版本、不同步给对方。
    diff = None
    diff_skip = ""
    if project_root:
        mode = {"merge_by": merge_by, "include_children": bool(include_children)}
        snap = make_snapshot(rows, assembly, project_root, mode)
        prev = load_snapshots(project_root, assembly)
        if prev:
            was = prev[-1].get("mode") or {}
            if was and was != mode:
                # 口径变了就别比：按图号 vs 一行一个实例，行键根本对不上
                diff_skip = ("上一次用的是「%s」，这次是「%s」，口径不一样，"
                             "不做对比" % (_mode_name(was), _mode_name(mode)))
            else:
                diff = diff_snapshots(prev[-1], snap)
        save_snapshot(project_root, assembly, snap)
    return {"root": root, "columns": columns, "rows": rows, "xlsx": xlsx,
            "csv": csv_path, "files": len(rows), "total_qty": total_qty,
            "parts_seen": len([r for r in records if r["kind"] == "part"]),
            "missing": missing, "missing_names": missing_names,
            "diff": diff, "diff_skip": diff_skip,
            "missing_attrs": summarize_missing(rows)}
