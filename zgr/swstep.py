# -*- coding: utf-8 -*-
"""把 SolidWorks 零件/装配体/图纸批量导出成 STEP（交给别人、能进 CAM 的中性格式）。

为什么单独一个模块：3D 预览那条路（mesh.py）导出的是 3MF，只为出图；
STEP 是交付格式，一次往往要导几十个零件。要是每个文件都启停一次 SolidWorks，
光启动就好几十秒，所以这里**整个批次共用一个 SolidWorks 实例**，导完才退出。

沿用 mesh.py 已经踩平的三条规矩，不另搞一套：
  · 跨进程锁：同一台机器同一时刻只有一个人能驱动 SolidWorks（批量补图那个
    计划任务是另一个进程，进程内的线程锁管不到它）；
  · 用户正开着 SolidWorks 就明确拒绝，绝不去关它、也不往它的会话里塞文档；
  · 导完等 SolidWorks 真正退出再放锁，不然紧接着的下一次导出会把它当成
    「用户的 SolidWorks 开着」而直接失败。
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from . import mesh

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STEP_EXTS = (".sldprt", ".sldasm", ".slddrw")
PER_FILE_TIMEOUT = 300            # 单个零件导出上限（秒）
EXTRA_TIMEOUT = 240               # 整批的额外余量（启动/退出/timeout 对话框）
SETTLE = 60                       # SolidWorks 说导完了以后，等文件落盘的上限（秒）
STALL = 240                       # 一个文件多久没动静就判定卡住（秒）


class StepError(Exception):
    pass


# --------------------------------------------------------------------- 输出命名
def _safe_stem(name: str) -> str:
    stem = os.path.splitext(name)[0] or "未命名"
    for ch in '\\/:*?"<>|':
        stem = stem.replace(ch, "-")
    return stem.strip() or "未命名"


def out_names(rels) -> dict:
    """把「项目里的相对路径」映射成「输出文件名」，重名的不互相覆盖。

    同一个装配体下面经常有 `左件/底板.sldprt` 和 `右件/底板.sldprt`，
    拍平到一个文件夹里就会打架。规则：
      1. 先都用 <原名>.step
      2. 撞名的，改用 <上层目录>_<原名>.step
      3. 还撞（或多个同名多层的），再补 (2)、(3)
    """
    first = {}
    for rel in rels:
        first.setdefault(_safe_stem(os.path.basename(rel)).lower(), []).append(rel)
    names = {}
    used = set()
    for low, group in first.items():
        if len(group) == 1:
            nm = _safe_stem(os.path.basename(group[0])) + ".step"
            names[group[0]] = nm
            used.add(nm.lower())
        else:
            for rel in group:
                parent = os.path.basename(os.path.dirname(rel).replace("\\", "/"))
                base = (_safe_stem(parent) + "_" + _safe_stem(os.path.basename(rel))) \
                    if parent else _safe_stem(os.path.basename(rel))
                nm = base + ".step"
                n = 2
                while nm.lower() in used:
                    nm = "%s (%d).step" % (base, n)
                    n += 1
                names[rel] = nm
                used.add(nm.lower())
    return names


# --------------------------------------------------------------------- 目标目录
def check_dest(dest: str, project_root: str = "") -> str:
    dest = (dest or "").strip()
    if not dest:                       # abspath("") 会变成当前目录，得先挡掉空串
        raise StepError("请选择 STEP 放到哪个文件夹")
    dest = os.path.abspath(dest)
    if os.path.exists(dest) and not os.path.isdir(dest):
        raise StepError("这个路径是个文件，不是文件夹：%s" % dest)
    if project_root and os.path.normcase(dest) == os.path.normcase(
            os.path.abspath(project_root)):
        raise StepError("别直接放到项目根目录里，另选一个文件夹（比如项目旁边的 STEP 导出）")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        raise StepError("建不了这个文件夹：%s（%s）" % (dest, e)) from None
    return dest


# --------------------------------------------------------------------- 驱动 SolidWorks
_VBS = r"""On Error Resume Next
Dim a, fso, jf, line, parts, sw, m, ok, ids, one, i, n, src, dst, t
Set a = WScript.Arguments
Set fso = CreateObject("Scripting.FileSystemObject")
Set jf = fso.OpenTextFile(a(0), 1, False, -1)
If a.Count > 1 Then
  ids = Split(a(1), ",")
Else
  ids = Array("SldWorks.Application")
End If
Set sw = Nothing
For Each one In ids
  ' 已开着就退出：结尾会 ExitApp + CloseAllDocuments，接上去等于关掉用户正在编辑的图
  Dim pre, alive
  pre = False
  On Error Resume Next
  Set alive = GetObject(, one)
  If Err.Number = 0 And Not (alive Is Nothing) Then pre = True
  Err.Clear
  Set alive = Nothing
  On Error Resume Next
  If pre Then
    WScript.Echo "BUSY " & one
    WScript.Quit 5
  End If
  Set sw = CreateObject(one)
  If Err.Number = 0 Then
    If Not sw Is Nothing Then Exit For
  End If
  Err.Clear
  Set sw = Nothing
Next
If sw Is Nothing Then
  WScript.Echo "FATAL create " & Err.Number
  WScript.Quit 2
End If
sw.Visible = False
sw.UserControl = False
sw.CommandInProgress = True
i = 0
Do Until jf.AtEndOfStream
  line = jf.ReadLine
  If Len(line) > 1 Then
    WScript.Echo "BEGIN " & i
    parts = Split(line, vbTab)
    src = parts(0)
    dst = parts(1)
    Err.Clear
    Set m = Nothing
    Set m = sw.OpenDoc(src, 1)
    If m Is Nothing Then Set m = sw.OpenDoc(src, 2)
    If m Is Nothing Then Set m = sw.OpenDoc(src, 3)
    If m Is Nothing Then Set m = sw.LoadFile2(src, "r")
    If m Is Nothing Then
      WScript.Echo "ERR " & i & " open " & Err.Number & " " & Err.Description
    Else
      t = m.GetTitle
      Err.Clear
      ' 只用 SaveAs。SaveAs2 在这里是坑：它可能抛「类型不匹配」，也可能谎报成功
      ' 却一个字节都不写，于是同一个会话里第二个零件开始全部导不出来（174 实测）。
      ' SaveAs 导成 STEP 是副本导出，零件本身不会被改名，关文档也不弹框。
      ok = m.SaveAs(dst)
      If fso.FileExists(dst) Then
        If fso.GetFile(dst).Size > 0 Then
          WScript.Echo "OK " & i
        Else
          WScript.Echo "ERR " & i & " empty file"
        End If
      Else
        WScript.Echo "ERR " & i & " save " & Err.Number & " " & Err.Description
      End If
      ' 每个文件导完都把文档关干净，别把状态带给下一个
      Err.Clear
      sw.CloseDoc CStr(t)
      If sw.GetDocumentCount > 0 Then sw.CloseAllDocuments True
      Err.Clear
    End If
    i = i + 1
  End If
Loop
jf.Close
sw.ExitApp
WScript.Echo "DONE " & i
"""


def _hint(code: str, desc: str) -> str:
    """把 SolidWorks 的返回翻成用户能照着处理的话"""
    desc = (desc or "").strip()
    tail = ("（SolidWorks 说：" + desc + "）") if desc and not desc.isdigit() else ""
    if code == "open":
        return ("SolidWorks 打不开这个文件：多半是缺了它引用的零件（引用件要和主文件"
                "在同一目录），也可能是文件损坏或用更高版本存的" + tail)
    if code == "save":
        return "打开了，但导出 STEP 失败：文件可能只读、被占用，或同名文件正开着" + tail
    if code == "empty":
        return "导出出来的 STEP 是空文件（零件里可能没有实体）"
    return "导出失败" + tail


def export(pairs, dest, progress=None, log=print) -> dict:
    """pairs: [(相对路径, 源文件绝对路径)]；dest 已校验过

    progress(phase, done, total, detail) 会被频繁调用。
    返回 {ok:[...], failed:[{rel, error, out}], dest, files, seconds}
    """
    if os.name != "nt":
        raise StepError("这台机器不能导出 STEP（需要装了 SolidWorks 的 Windows）")
    pairs = list(pairs)
    if not pairs:
        raise StepError("没有选中任何零件")
    dest = check_dest(dest)
    cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                           "System32", "cscript.exe")
    if not os.path.exists(cscript):
        raise StepError("找不到 cscript.exe，无法驱动 SolidWorks")

    def say(phase, done=0, total=0, detail=""):
        if progress:
            progress(phase, done, total, detail)

    names = out_names([rel for rel, _src in pairs])
    missing = [(rel, src) for rel, src in pairs
               if not os.path.isfile(src)]
    todo = [(rel, src) for rel, src in pairs if os.path.isfile(src)]
    failed = [{"rel": rel, "error": "工作区里找不到这个文件（可能已被删除）", "out": ""}
              for rel, _src in missing]
    if not todo:
        return {"ok": [], "failed": failed, "dest": dest, "files": 0, "seconds": 0}

    tmp = tempfile.mkdtemp(prefix="zgr-step-")
    vbs = os.path.join(tmp, "step.vbs")
    with open(vbs, "w", encoding="ascii", errors="replace") as f:
        f.write(_VBS)

    t0 = time.time()
    ok, total = [], len(todo)

    def drive(batch, before, started=None):
        """起一轮 SolidWorks 把这批导完（同一轮里 SolidWorks 只启动一次）。
        某些零件打开/另存会弹一个看不见的对话框，cscript 就那么卡着不返回，
        //T 又是按整批算的（两千多个零件能挂好几天）。所以自己盯着输出：
        一段时间没有任何动静就判定这一个卡住，杀掉这轮、把这一个记失败、
        从下一个接着导——坏一个不能拖垮一整批。"""
        idx = 0
        while idx < len(batch):
            rest = batch[idx:]
            job = os.path.join(tmp, "job.txt")
            # UTF-16LE：VBS 用 TristateTrue 读回来，中文路径不会乱码。
            # 每行「源路径<TAB>输出路径」；换行交给 newline 参数翻成 CRLF
            # （自己再写 CRLF 会被翻成 CR+CRLF，VBS 的 ReadLine 会把多出来的当成空行）
            with open(job, "w", encoding="utf-16-le", newline="\r\n") as f:
                for rel, src in rest:
                    f.write("%s\t%s\n" % (src, os.path.join(dest, names[rel])))
            cmd = [cscript, "//nologo",
                   "//T:%d" % (PER_FILE_TIMEOUT * len(rest) + EXTRA_TIMEOUT),
                   vbs, job, ",".join(mesh.sldworks_progids())]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="mbcs", errors="replace",
                                    creationflags=CREATE_NO_WINDOW)
            q = queue.Queue()

            def _reader(pr=proc, qq=q):
                try:
                    for ln in pr.stdout:
                        qq.put(ln)
                except (OSError, ValueError):
                    pass
                finally:
                    qq.put(None)

            threading.Thread(target=_reader, daemon=True).start()
            cur, stalled, fatal = 0, False, ""
            while True:
                try:
                    raw = q.get(timeout=STALL)
                except queue.Empty:
                    stalled = True
                    break
                if raw is None:
                    break
                line = (raw or "").strip().replace("\r", "")
                if not line:
                    continue
                head = line.split(" ")[0]
                if head == "BEGIN":
                    try:
                        cur = int(line.split(" ")[1])
                    except (IndexError, ValueError):
                        cur = 0
                elif head in ("OK", "ERR"):
                    try:
                        i = int(line.split(" ")[1])
                    except (IndexError, ValueError):
                        continue
                    if i < 0 or i >= len(rest):
                        continue
                    cur = i + 1
                    rel, _src = rest[i]
                    if head == "OK":
                        ok.append(rel)
                        say("导出中", idx + i + 1, total, os.path.basename(rel))
                    else:
                        parts = line.split(" ", 3)
                        kind = parts[2] if len(parts) > 2 else ""
                        desc = parts[3] if len(parts) > 3 else ""
                        failed.append({"rel": rel, "error": _hint(kind, desc),
                                       "out": names.get(rel, "")})
                        say("导出中", idx + i + 1, total, "失败：" + os.path.basename(rel))
                elif head == "FATAL":
                    fatal = "SolidWorks 没能启动，先手动打开一次确认它能正常用"
                elif head == "BUSY":
                    # 用户中途自己打开了 SolidWorks：这一批让给他，绝不接上去
                    # 再 ExitApp（那等于关掉他没保存的图）
                    fatal = ("这台机器的 SolidWorks 正开着（可能是你自己打开的），"
                             "这一批先不导出；关掉它再点一次就行")
                elif head == "DONE":
                    cur = len(rest)
            if stalled:
                rel = rest[cur][0] if cur < len(rest) else "?"
                failed.append({"rel": rel, "out": names.get(rel, ""),
                               "error": "SolidWorks 卡住了 %d 秒没有反应，多半是弹了个"
                                        "看不见的对话框（缺引用件或版本不匹配），这一个先跳过"
                                        % STALL})
                say("导出中", idx + cur + 1, total, "卡住，跳过：" + os.path.basename(rel))
            if fatal:
                failed.append({"rel": "", "error": fatal, "out": ""})
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            # 杀掉/退出之后把这次起的 SolidWorks 收干净，下一轮才能重新启动。
            # 传 started：只收「这一轮开始之后才起来的」——师傅中途自己打开的
            # SolidWorks 不能杀（他可能正在画图，杀了就是丢工作）。
            mesh._wait_exit()
            mesh._cleanup_own(before, started)
            if stalled:
                idx += cur + 1
                continue
            if fatal:
                break
            idx = len(batch)

    def gated(batch):
        """过 SolidWorks 独占闸门跑一轮；用户正开着 SolidWorks 就一点不动它"""
        with mesh._sw_gate():
            started = time.time()
            before = mesh._sldworks_pids()
            if before:
                time.sleep(min(mesh.SW_EXIT_GRACE, 3))
                before = mesh._sldworks_pids()
            if before:
                raise StepError("这台机器的 SolidWorks 正开着，先关掉它再导出 STEP"
                                "（不会去动你正在编辑的文档）")
            drive(batch, before, started)
        ours = mesh._sldworks_pids() - before
        mesh._wait_exit(pids=ours or None)     # 只等自己那几个，别陪用户的 SW 等满 20 秒
        mesh._cleanup_own(before, started)

    # 以磁盘上真的有文件为准，但要给它时间落盘：大零件的 STEP 转换有时会晚几秒
    # 才写完（实现在 sldworks_fs 那个后台进程里），立刻检查会把好文件判成失败。
    def _ready(rel):
        p = os.path.join(dest, names.get(rel, ""))
        try:
            return os.path.getsize(p) > 0
        except OSError:
            return False

    def settle(rels):
        deadline = time.time() + SETTLE
        while True:
            pending = [r for r in rels if not _ready(r)]
            if not pending or time.time() >= deadline:
                break
            say("等待写入磁盘", 0, total, "%d 个还在写" % len(pending))
            time.sleep(0.5)
        return [r for r in rels if _ready(r)]

    say("启动 SolidWorks", 0, total, "%d 个文件" % total)
    gated(todo)
    real_ok = settle(ok)

    # SolidWorks 在同一个会话里连着导好几个，个别零件会「报成功但没写文件」；
    # 单独给它开一次 SolidWorks 再导就正常（174 上实测过）。所以这些补一次，
    # 补导还不行才判定失败——不能自己没导出来却告诉用户成功了。
    stuck = [(rel, src) for rel, src in todo if rel in ok and rel not in real_ok]
    if stuck:
        skip = set(x[0] for x in stuck)
        ok = [r for r in ok if r not in skip]
        say("补导", 0, total, "%d 个单独重试" % len(stuck))
        for n, (rel, src) in enumerate(stuck):
            gated([(rel, src)])
            if settle([rel]):
                real_ok.append(rel)
                say("补导", n + 1, len(stuck), os.path.basename(rel))
            else:
                failed.append({"rel": rel, "error": "导了两次都没写出文件"
                               "（零件可能是空的，或者导出时被别的程序占着）",
                               "out": names[rel]})
    shutil.rmtree(tmp, ignore_errors=True)
    say("完成", len(real_ok), total)
    log("STEP 导出：成功 %d，失败 %d → %s" % (len(real_ok), len(failed), dest))
    # 成功项原来只有源路径，导出清单里「输出文件」那列只能拿源文件名凑数；
    # 这里把「源 → 实际写出的 STEP 文件名」一起带出去，清单才对得上
    outputs = {rel: names.get(rel, "") for rel in real_ok}
    return {"ok": real_ok, "failed": failed, "dest": dest, "outputs": outputs,
            "files": len(real_ok), "seconds": round(time.time() - t0, 1)}
