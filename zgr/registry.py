"""项目登记表：%APPDATA%\\zhuiguang-record\\projects.json"""
from __future__ import annotations

import json
import os
import time


PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def appdata_dir() -> str:
    """项目表/缓存放哪。

    三种模式，优先级从高到低：
      1. `ZGR_HOME` 环境变量（多套配置、测试、或者是装在 U 盘上）；
      2. 应用目录里有 `portable.flag`（绿色版：不往 C 盘写任何东西）；
      3. `%APPDATA%/zhuiguang-record`（正常安装）。
    """
    env = os.environ.get("ZGR_HOME")
    if env:
        d = os.path.abspath(env)
    elif os.path.isfile(os.path.join(PKG_ROOT, "portable.flag")):
        d = os.path.join(PKG_ROOT, "userdata")
    else:
        base = (os.environ.get("APPDATA")
                or os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
        d = os.path.join(base, "zhuiguang-record")
    os.makedirs(d, exist_ok=True)
    return d


def data_dir() -> str:
    d = os.path.join(appdata_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


class Registry:
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(appdata_dir(), "projects.json")
        self._mtime = self._stamp()
        self.projects = self._load()

    def _stamp(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def reload(self):
        """别的进程（命令行）改了项目表时，让常驻服务也能立刻看到"""
        m = self._stamp()
        if m != self._mtime:
            self.projects = self._load()
            self._mtime = m
        return self.projects

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("projects", [])
            return [p for p in data if isinstance(p, dict) and p.get("path")]
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            # 文件损坏：先留证据再继续，绝不能让下一次 save 把原登记表覆盖掉
            try:
                os.replace(self.path, self.path + ".损坏-%d" % int(time.time()))
            except OSError:
                pass
            return []

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = "%s.tmp.%d" % (self.path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"projects": self.projects}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._mtime = self._stamp()

    def add(self, path: str, name: str | None = None):
        path = os.path.abspath(path)
        for p in self.projects:
            if os.path.normcase(p["path"]) == os.path.normcase(path):
                if name:
                    p["name"] = name
                    self.save()
                return p
        entry = {
            "path": path,
            "name": name or os.path.basename(path.rstrip("\\/")) or path,
            "added": int(time.time() * 1000),
        }
        self.projects.append(entry)
        self.save()
        return entry

    def remove(self, path: str):
        key = os.path.normcase(os.path.abspath(path))
        before = len(self.projects)
        self.projects = [
            p for p in self.projects if os.path.normcase(os.path.abspath(p["path"])) != key
        ]
        if len(self.projects) != before:
            self.save()
            return True
        return False

    def get(self, path: str):
        key = os.path.normcase(os.path.abspath(path))
        for p in self.projects:
            if os.path.normcase(os.path.abspath(p["path"])) == key:
                return p
        return None
