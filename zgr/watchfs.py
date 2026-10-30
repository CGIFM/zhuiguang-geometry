# -*- coding: utf-8 -*-
"""文件系统通知：有人动文件时立刻知道，没人动就一次扫描都不用做

界面每 1.2～3 秒就要问一次「有没有未记录的改动」，现在的答案靠全盘扫描：
三千个零件的项目里每次两百多毫秒，而绝大多数时候答案都是「什么都没变」——
纯属白烧 CPU（笔记本上是真能听见风扇的）。

Windows 有 ReadDirectoryChangesW：改一个零件，系统会主动告诉我们改的是哪个路径。
于是「什么都没发生」时可以一次系统调用都不用就回上一次的结果，
而有人改动时反而**比轮询更快**被发现（毫秒级，不用等下一次问）。

铁律：这只是**加速**，不是唯一的信息来源。
- 拿不到通知（非 Windows、句柄建不出来、线程挂了）→ state() 返回 None，调用方照旧全盘扫；
- 就算一切正常，也至少每 MAX_IDLE 秒强制重扫一次；
- 缓冲区溢出（改动比我们读得快）本身就是「有变化」的信号，直接标记为脏。
宁可白扫一遍，也不能因为漏掉一条通知让界面永远显示旧状态。
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time

META_DIR = ".zgr"
MAX_IDLE = 10.0          # 就算没收到任何通知，也至少这么久强制重扫一次
_NOTIFY = 0x1 | 0x2 | 0x4 | 0x8 | 0x10   # 文件名/目录名/属性/大小/最后写入

# 版本号必须**全局唯一**，不能每个 Watcher 从 0 数起：
# 监视器被淘汰重建后新对象也停在 0，状态缓存会以为「版本没变」继续用旧答案。
_VERSION_SRC = itertools.count(1)


class Watcher:
    """盯着一个目录（含子目录）。只报「有东西变了」，不报变了什么。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()   # 并发第一次 start() 只能起一条线程
        self._version = next(_VERSION_SRC)
        self._stamp = time.time()
        self._ok = False
        self._error = ""
        self._stop = threading.Event()
        self._thread = None
        self._handle = None
        self._k32 = None

    # ---------------------------------------------------------------- 对外
    def start(self) -> bool:
        # 同一秒里两个请求都来问这个项目时，两边都会走到这里。不锁的话会各起
        # 一条监听线程，而且后写的那条会覆盖 self._handle —— stop() 只能取消
        # 其中一个句柄，另一条线程永远卡在 ReadDirectoryChangesW 里。
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return self._ok
            if not sys.platform.startswith("win"):
                self._error = "这个系统没有文件通知，照常轮询"
                return False
            if not os.path.isdir(self.root):
                self._error = "目录不存在：%s" % self.root
                return False
            self._stop.clear()          # 被停过又拿来用：这次是新的开始
            self._thread = threading.Thread(target=self._run, name="zgr-watch", daemon=True)
            self._thread.start()
            # 等一下，让「能不能用」在第一次询问时就有答案（最多 0.5 秒）
            for _ in range(50):
                if self._ok or self._error:
                    break
                time.sleep(0.01)
            return self._ok

    def state(self):
        """(版本号, 最后一次变动时间)；用不了就返回 None（调用方退回轮询）"""
        if not self._ok:
            return None
        with self._lock:
            return self._version, self._stamp

    def stop(self):
        self._stop.set()
        with self._lock:
            self._ok = False          # 停了就别再自称可用，调用方要退回轮询
            h = self._handle
            self._handle = None       # 先交出去，别让线程回头再关一次
        k32 = self._k32
        if h and k32 is not None:
            # 另一个线程正阻塞在 ReadDirectoryChangesW 里：直接 CloseHandle 会**卡住**
            # （要等那次读真正结束），必须先 CancelIoEx 把它打断。
            # 顺带：CloseHandle 不走 argtypes 的话句柄会被截成 32 位——关错东西。
            try:
                cancel = getattr(k32, "CancelIoEx", None)
                if cancel is not None:
                    cancel(h, None)
            except Exception:                              # noqa: BLE001
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)

    # ---------------------------------------------------------------- 内部
    def _mark(self):
        with self._lock:
            self._version += 1
            self._stamp = time.time()

    def _run(self):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID, wintypes.LPVOID]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        cancel = getattr(k32, "CancelIoEx", None)
        if cancel is not None:
            cancel.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            cancel.restype = wintypes.BOOL
        self._k32 = k32

        FILE_LIST_DIRECTORY = 0x0001
        FILE_SHARE_ALL = 0x0007
        OPEN_EXISTING = 3
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        ERROR_NOTIFY_ENUM_DIR = 1023
        size = 64 * 1024

        failures = 0
        while not self._stop.is_set():
            h = k32.CreateFileW(self.root, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                                OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
            if not h or h in (-1, 0xFFFFFFFFFFFFFFFF):     # INVALID_HANDLE_VALUE
                failures += 1
                self._error = "打不开目录（错误码 %d）" % ctypes.get_last_error()
                with self._lock:
                    self._ok = False
                if failures >= 15:      # 连续半分钟打不开：别一直空转，交回轮询
                    self._error = "目录打不开，已交回轮询：%s" % self.root
                    return
                time.sleep(2.0)
                continue
            failures = 0
            read_failures = 0
            with self._lock:
                if self._stop.is_set():      # 已经有人叫停了：别再自称可用
                    break
                self._ok = True
                self._error = ""
                self._handle = h
            buf = ctypes.create_string_buffer(size)
            while not self._stop.is_set():
                got = wintypes.DWORD(0)
                ok = k32.ReadDirectoryChangesW(h, buf, size, True, _NOTIFY,
                                               ctypes.byref(got), None, None)
                if self._stop.is_set():
                    break
                if not ok:
                    err = ctypes.get_last_error()
                    if err == ERROR_NOTIFY_ENUM_DIR:
                        read_failures = 0
                        self._mark()                       # 改动挤爆了缓冲区：当有变化
                        continue
                    # 句柄失效（目录被删/移动）之类。以前每次失败都 _mark() 一次再
                    # 0.5 秒重试：目录被改名后 CreateFileW 还能成功、读却一直失败，
                    # 于是每 0.5 秒推一次版本号，界面就退化成每轮全盘扫描还查不出原因。
                    # 现在只标一次，连续失败够多就认输、交回轮询。
                    read_failures += 1
                    if read_failures == 1:
                        self._mark()
                    if read_failures >= 5:
                        with self._lock:
                            self._ok = False
                            self._error = "目录读不到改动（错误码 %d），已交回轮询" % err
                            self._handle = None
                        # 句柄必须还回去：以前这里直接 return，跳过了下面的
                        # CloseHandle。被放弃的目录不会自己释放句柄，而这个目录
                        # 之后每被问一次就会重建一次监视器、再漏一个——几十秒就几十个，
                        # 用户还会发现这个项目文件夹改名/删除一直失败（被占着）。
                        try:
                            k32.CloseHandle(h)
                        except Exception:                  # noqa: BLE001
                            pass
                        return
                    time.sleep(0.5)
                    break
                read_failures = 0
                if got.value and self._interesting(buf.raw, got.value):
                    self._mark()
            with self._lock:
                self._handle = None
            try:
                k32.CloseHandle(h)
            except Exception:                              # noqa: BLE001
                pass

    @staticmethod
    def _interesting(raw: bytes, length: int) -> bool:
        """事件里有没有「项目自己」的改动？我们写 .zgr 的时候会触发一堆通知，
        那些不算变化——否则恢复一版就自己把自己叫醒，白扫。"""
        import struct
        off = 0
        while off + 12 <= min(length, len(raw)):
            nxt, _action, nlen = struct.unpack_from("<III", raw, off)
            name = raw[off + 12: off + 12 + nlen]
            try:
                rel = name.decode("utf-16-le", "ignore")
            except Exception:                              # noqa: BLE001
                rel = ""
            first = rel.split("\\", 1)[0].split("/", 1)[0]
            if first and first != META_DIR:
                return True
            if not nxt:
                break
            off += nxt
        return False


class Manager:
    """按项目路径管理监视器；同一个项目只开一个，最多同时开几个"""

    # 以前是 8。项目列表会把**每一个**项目都问一遍状态，9 个以上时每一轮轮询
    # 都要淘汰一个、下一轮又淘汰同一个——监视器被反复建了拆、拆了建，
    # 每个请求还白白背上最长 3 秒的 join。政策是「宁可多留几个」：
    # 每个监视器只占一个目录句柄和一条阻塞线程，32 个也不过如此。
    MAX_WATCHERS = 32

    def __init__(self):
        self._lock = threading.Lock()
        self._map = {}
        self._used = {}

    def get(self, root: str, start: bool = True):
        key = os.path.normcase(os.path.abspath(root))
        victim = None
        with self._lock:
            w = self._map.get(key)
            self._used[key] = time.time()
            if w is None:
                if len(self._map) >= self.MAX_WATCHERS:
                    # 项目比名额多：停掉最久没被问过的那个，让正在看的项目始终有通知，
                    # 而不是「先来的几个占着、后面的永远退回轮询」
                    vkey = min(self._map, key=lambda k: self._used.get(k, 0.0))
                    victim = self._map.pop(vkey, None)
                    self._used.pop(vkey, None)
                w = Watcher(root)
                self._map[key] = w
        if victim is not None:
            # 停线程要 join（最长 3 秒），不能在请求线程里等：那会把这个项目的
            # 状态查询一起拖住。丢给后台线程慢慢收尾。
            threading.Thread(target=victim.stop, name="zgr-watch-reap",
                             daemon=True).start()
        if start and w.state() is None:
            w.start()
        return w if w.state() is not None else None

    def stop_all(self):
        with self._lock:
            items = list(self._map.values())
            self._map.clear()
            self._used.clear()
        for w in items:
            w.stop()

    def stop(self, root: str):
        key = os.path.normcase(os.path.abspath(root))
        with self._lock:
            w = self._map.pop(key, None)
        if w:
            w.stop()
