# -*- coding: utf-8 -*-
"""调 Windows 自带的文件 / 文件夹对话框（IFileOpenDialog）

为什么要用系统的那个：用户找文件时要跳桌面、下载、U 盘、网络位置、Quick Access、
最近使用……自己画的目录树永远差一截，而且不支持「新建文件夹」「搜索」「库」这些。
系统对话框就是「打开/另存为」里那个，习惯一致。

实现本身不依赖任何第三方库：ctypes 直接走 COM 接口（IFileOpenDialog 继承 IFileDialog）:
    IUnknown 0-2 | IModalWindow.Show 3 | IFileDialog.SetOptions 9 ... SetFolder 12 ... SetTitle 17
    IFileOpenDialog.GetResults 27
细节都写在下面的常量里，顺序就是 vtable 的下标。

前提是**这个进程有可操作的桌面**：服务跑在会话 0，或者 RDP 已经断开时，
对话框弹在没人看得见的地方，请求线程会一直挂着。所以先 desktop_ready() 检查，
不满足就抛 PickUnavailable，让前端退回网页里的目录树。
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import POINTER, byref, c_ulong, c_void_p, c_wchar_p

HRESULT = ctypes.c_long
CANCELLED = 0x800704C7


class PickUnavailable(RuntimeError):
    """当前进程弹不了系统对话框（不是 Windows / 会话 0 / 会话已断开）"""


class PickBusy(PickUnavailable):
    """已经有一个系统对话框开着（避免弹出两个叠在一起）"""


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
IID_IShellItem = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

FOS_NOCHANGEDIR = 0x00000008
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_ALLOWMULTISELECT = 0x00000200
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000

SIGDN_FILESYSPATH = 0x80058000

# vtable 下标
_IDX_RELEASE = 2
_IDX_SHOW = 3
_IDX_SETOPTIONS = 9
_IDX_GETOPTIONS = 10
_IDX_SETFOLDER = 12
_IDX_SETTITLE = 17
_IDX_GETRESULTS = 27
_IDX_COUNT = 7
_IDX_ITEMAT = 8
_IDX_DISPLAYNAME = 5

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1

_live = threading.Lock()      # 同一时刻只允许一个系统对话框


# --------------------------------------------------------------- 前提检查
def session_id():
    """当前进程所在的 Windows 会话号（0 = 系统会话，弹不了对话框）"""
    if sys.platform != "win32":
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = c_ulong(0)
    if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), byref(sid)):
        return None
    return int(sid.value)


def desktop_ready():
    """(能不能弹, 不能弹的原因)"""
    if sys.platform != "win32":
        return False, "只有 Windows 上才有资源管理器对话框"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        sid = c_ulong(0)
        if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), byref(sid)):
            return False, "读不到当前会话"
        if sid.value == 0:
            return False, "程序跑在系统会话里（会话 0），桌面上看不到对话框"
    except Exception as e:                                   # noqa: BLE001
        return False, "读不到会话：%s" % e

    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)

        class _UOF(ctypes.Structure):
            _fields_ = [("fInherit", c_ulong), ("fReserved", c_ulong), ("dwFlags", c_ulong)]

        flags, need = _UOF(), c_ulong(0)
        hwinsta = u32.GetProcessWindowStation()
        if not hwinsta or not u32.GetUserObjectInformationW(
                hwinsta, 1, byref(flags), ctypes.sizeof(flags), byref(need)):
            return False, "读不到窗口站状态"
        if not (flags.dwFlags & 0x0001):                      # WSF_VISIBLE
            return False, "这个进程没有可操作的桌面（窗口站不可见）"
    except Exception as e:                                   # noqa: BLE001
        return False, "检查桌面失败：%s" % e

    # RDP 断开时桌面还在，但没人能点确认 —— 别把自己卡死
    try:
        wts = ctypes.WinDLL("wtsapi32")
        wts.WTSQuerySessionInformationW.argtypes = [
            c_void_p, c_ulong, ctypes.c_int, POINTER(c_void_p), POINTER(c_ulong)]
        wts.WTSQuerySessionInformationW.restype = ctypes.c_int
        wts.WTSFreeMemory.argtypes = [c_void_p]
        buf, n = c_void_p(), c_ulong(0)
        if wts.WTSQuerySessionInformationW(None, sid.value, 8, byref(buf), byref(n)) and buf.value:
            state = ctypes.cast(buf, POINTER(ctypes.c_int))[0]
            wts.WTSFreeMemory(buf)
            if state == 4:                                   # WTSDisconnected
                return False, "远程桌面会话已断开，没人能点对话框"
    except Exception:                                        # noqa: BLE001
        pass
    return True, ""


# --------------------------------------------------------------- COM 小工具
def _guid(ole32, s):
    g = _GUID()
    hr = ole32.CLSIDFromString(c_wchar_p("{%s}" % s.strip("{}")), byref(g))
    if hr < 0:
        raise OSError("GUID 解析失败：%s" % s)
    return g


def _method(ptr, index, restype, *argtypes):
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(vtbl[index])


def _release(ptr):
    try:
        _method(ptr, _IDX_RELEASE, c_ulong)(ptr)
    except Exception:                                        # noqa: BLE001
        pass


def _check(hr, what):
    if hr < 0 and (hr & 0xFFFFFFFF) != CANCELLED:
        raise OSError("%s 失败：0x%08X" % (what, hr & 0xFFFFFFFF))
    return hr


# --------------------------------------------------------------- 主营业务
def pick(kind="folder", initial="", multiple=False, title="", timeout_note=""):
    """弹系统对话框，返回选中的路径列表；用户取消返回 []

    kind: "folder" 选文件夹 / "files" 选文件（multiple=True 可多选）
    """
    ok, why = desktop_ready()
    if not ok:
        raise PickUnavailable(why)
    if not _live.acquire(blocking=False):
        raise PickBusy("已经有一个选择对话框开着，先在那边选完")
    try:
        return _pick_com(kind, initial, multiple, title)
    finally:
        _live.release()


def _pick_com(kind, initial, multiple, title):
    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_ulong,
                                       POINTER(_GUID), POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = HRESULT
    ole32.CLSIDFromString.argtypes = [c_wchar_p, POINTER(_GUID)]
    ole32.CLSIDFromString.restype = HRESULT
    ole32.CoTaskMemFree.argtypes = [c_void_p]
    shell32.SHCreateItemFromParsingName.argtypes = [c_wchar_p, c_void_p,
                                                    POINTER(_GUID), POINTER(c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = HRESULT

    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:           # 已被别的模式初始化过
        raise OSError("COM 初始化失败：0x%08X" % (hr & 0xFFFFFFFF))
    must_uninit = hr >= 0

    dialog = c_void_p()
    try:
        _check(ole32.CoCreateInstance(
            byref(_guid(ole32, CLSID_FileOpenDialog)), None, CLSCTX_INPROC_SERVER,
            byref(_guid(ole32, IID_IFileOpenDialog)), byref(dialog)), "创建文件对话框")

        opts = c_ulong(0)
        _method(dialog, _IDX_GETOPTIONS, HRESULT, POINTER(c_ulong))(dialog, byref(opts))
        opt = opts.value | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
        if kind == "folder":
            opt |= FOS_PICKFOLDERS
        else:
            opt |= FOS_FILEMUSTEXIST
            if multiple:
                opt |= FOS_ALLOWMULTISELECT
        _check(_method(dialog, _IDX_SETOPTIONS, HRESULT, c_ulong)(dialog, c_ulong(opt)),
               "设置对话框选项")

        if title:
            _method(dialog, _IDX_SETTITLE, HRESULT, c_wchar_p)(dialog, c_wchar_p(title))

        # 从上次用的目录开始（找不到就让它自己决定）
        start = (initial or "").strip()
        if start:
            if os.path.isfile(start):
                start = os.path.dirname(start)
            if os.path.isdir(start):
                item = c_void_p()
                hr2 = shell32.SHCreateItemFromParsingName(
                    c_wchar_p(start), None, byref(_guid(ole32, IID_IShellItem)), byref(item))
                if hr2 >= 0 and item.value:
                    _method(dialog, _IDX_SETFOLDER, HRESULT, c_void_p)(dialog, item)
                    _release(item)

        hr = _method(dialog, _IDX_SHOW, HRESULT, c_void_p)(dialog, None)
        if (hr & 0xFFFFFFFF) == CANCELLED:
            return []
        _check(hr, "显示对话框")

        arr = c_void_p()
        _check(_method(dialog, _IDX_GETRESULTS, HRESULT, POINTER(c_void_p))(dialog, byref(arr)),
               "取选择结果")
        if not arr.value:
            return []
        out = []
        try:
            n = c_ulong(0)
            _method(arr, _IDX_COUNT, HRESULT, POINTER(c_ulong))(arr, byref(n))
            for i in range(min(n.value, 4000)):
                item = c_void_p()
                if _method(arr, _IDX_ITEMAT, HRESULT, c_ulong, POINTER(c_void_p))(
                        arr, c_ulong(i), byref(item)) < 0 or not item.value:
                    continue
                try:
                    name = c_wchar_p()
                    if _method(item, _IDX_DISPLAYNAME, HRESULT, c_ulong,
                               POINTER(c_wchar_p))(item, c_ulong(SIGDN_FILESYSPATH),
                                                   byref(name)) >= 0 and name.value:
                        out.append(name.value)
                        ole32.CoTaskMemFree(ctypes.cast(name, c_void_p))
                finally:
                    _release(item)
        finally:
            _release(arr)
        return out
    finally:
        if dialog.value:
            _release(dialog)
        if must_uninit:
            ole32.CoUninitialize()


def self_test():
    """不弹窗，只把 COM 那一串调用全走一遍（建对话框 → 设选项 → 关掉）。

    用来在无头环境/部署后确认接口序号、类型都没写错。
    """
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoCreateInstance.argtypes = [POINTER(_GUID), c_void_p, c_ulong,
                                       POINTER(_GUID), POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = HRESULT
    ole32.CLSIDFromString.argtypes = [c_wchar_p, POINTER(_GUID)]
    ole32.CLSIDFromString.restype = HRESULT
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    dialog = c_void_p()
    try:
        _check(ole32.CoCreateInstance(
            byref(_guid(ole32, CLSID_FileOpenDialog)), None, CLSCTX_INPROC_SERVER,
            byref(_guid(ole32, IID_IFileOpenDialog)), byref(dialog)), "创建文件对话框")
        opts = c_ulong(0)
        _check(_method(dialog, _IDX_GETOPTIONS, HRESULT, POINTER(c_ulong))(dialog, byref(opts)),
               "读选项")
        _check(_method(dialog, _IDX_SETOPTIONS, HRESULT, c_ulong)(
            dialog, c_ulong(opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)), "设置选项")
        _check(_method(dialog, _IDX_SETTITLE, HRESULT, c_wchar_p)(
            dialog, c_wchar_p("追光几何 · 自检")), "设置标题")
        _method(dialog, 23, HRESULT)(dialog)                 # IFileDialog::Close
        _release(dialog)
        dialog = c_void_p()
        return True
    finally:
        if dialog.value:
            _release(dialog)
        if hr >= 0:
            ole32.CoUninitialize()


if __name__ == "__main__":                                   # 手动试
    ok, why = desktop_ready()
    print("desktop_ready:", ok, why)
    if "--self-test" in sys.argv:
        print("self_test:", self_test())
    else:
        kind = "files" if "--files" in sys.argv else "folder"
        print(pick(kind, sys.argv[1] if len(sys.argv) > 1 else "",
                   multiple="--multi" in sys.argv))
