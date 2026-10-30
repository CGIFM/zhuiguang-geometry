# -*- coding: utf-8 -*-
"""第 66 轮安全修复：把「能借服务读全盘 / 借令牌管整机」的几条路堵掉

每条都对应一条真实的调用路径，不是「理论上可能」：
- 对端同步时说 `project: "C:\\别的目录"` → 本机把那个目录当项目读写
- `/api/preview?file=C:/Windows/…` → 一个按名字取缩略图的接口变成了读全盘
- `/api/log?limit=abc` → ValueError 被兜底成 500，日志里一段 traceback
- 两台机器同名项目时的报错把本机路径全部回给对方
- `/api/pick` 开着系统对话框时持有全局写锁，整个服务停摆几分钟
- 令牌 = 整机管理权：拿着它就能让这台机器弹框、开文件、改远端存储凭据
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


class Base(unittest.TestCase):
    token = None
    local = True

    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr import server as srv
        from zgr.registry import Registry
        from zgr.server import App, Handler

        srv._AUTH_FAILS.clear()
        self.srv = srv
        self.tmp = tempfile.mkdtemp(prefix="zgr-sec66-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        repo = Repo(self.root).init("安全66")
        with open(os.path.join(self.root, "侧板.sldprt"), "w", encoding="utf-8") as f:
            f.write("一")
        repo.commit("一")

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg, token=self.token)
        self.app.registry.add(self.root, "安全66")
        H = Handler if self.local else type(
            "H", (Handler,), {"is_local": lambda self: False})
        handler = type("HH", (H,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.repo = repo

    def tearDown(self):
        self.srv._AUTH_FAILS.clear()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, route, obj=None, headers=None, method=None):
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        h = dict(headers or {})
        if obj is not None:
            h.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + route, data=data, headers=h,
                                   method=method or ("POST" if obj is not None else "GET"))
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jget(self, route, headers=None):
        code, body = self.req(route, headers=headers)
        try:
            return code, json.loads(body.decode("utf-8"))
        except ValueError:
            return code, {}


class SyncRepoPathTest(Base):
    """对端说的「项目」只能当名字，不能当路径"""

    def test_remote_cannot_point_at_arbitrary_path(self):
        other = os.path.join(self.tmp, "别人的目录")
        os.makedirs(other)
        Repo(other).init("别人的")
        with self.assertRaises(RepoError):
            self.app.sync_repo(other, allow_path=False)
        # 本机自己（网页/脚本）还是可以按路径指定
        got = self.app.sync_repo(other, allow_path=True)
        self.assertEqual(os.path.normcase(got.root), os.path.normcase(os.path.abspath(other)))

    def test_unc_path_is_not_a_project_for_remote(self):
        with self.assertRaises(RepoError):
            self.app.sync_repo(r"\\127.0.0.1\share\x", allow_path=False)

    def test_same_name_error_does_not_leak_paths(self):
        second = os.path.join(self.tmp, "另一个项目")
        os.makedirs(second)
        Repo(second).init("安全66")            # 和已有项目同名
        self.app.registry.add(second)          # 两条记录、同一个名字
        try:
            self.app.sync_repo("安全66", allow_path=False)
            self.fail("同名项目应该报错，不能随便挑一个")
        except RepoError as e:
            msg = str(e)
            self.assertNotIn(self.tmp, msg)     # 本机路径一个字都不能出现
            self.assertNotIn(os.path.abspath(second), msg)
            self.assertIn("2", msg)

    def test_sync_refs_rejects_bad_branch(self):
        code, d = self.jget("/api/sync/refs?project=" + urllib.parse.quote("安全66"))
        self.assertEqual(code, 200, d)
        head = d["branches"].get(d["default"])
        code, body = self.req("/api/sync/refs",
                              {"project": "安全66", "branch": "../../etc",
                               "old": head, "new": head})
        self.assertEqual(code, 400, body[:200])
        self.assertIn("分支", json.loads(body.decode("utf-8"))["error"])


class PreviewPathTest(Base):
    """按名字取缩略图，不能变成按名字读全盘"""

    def _secret(self):
        p = os.path.join(self.tmp, "secret.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        return p

    def test_absolute_path_is_refused(self):
        p = self._secret().replace("\\", "/")
        code, d = self.jget("/api/preview?path=%s&file=%s"
                            % (urllib.parse.quote(self.root), urllib.parse.quote(p)))
        self.assertEqual(code, 403, d)

    def test_drive_relative_path_is_refused(self):
        code, d = self.jget("/api/preview?path=%s&file=%s"
                            % (urllib.parse.quote(self.root),
                               urllib.parse.quote("C:/Windows/win.ini")))
        self.assertEqual(code, 403, d)

    def test_dotdot_is_refused(self):
        code, d = self.jget("/api/preview?path=%s&file=../secret.txt"
                            % urllib.parse.quote(self.root))
        self.assertEqual(code, 403, d)

    def test_missing_file_is_404_not_500(self):
        code, d = self.jget("/api/preview?path=%s&file=%s"
                            % (urllib.parse.quote(self.root),
                               urllib.parse.quote("没有这个.sldprt")))
        self.assertEqual(code, 404, d)


class BadIntParamTest(Base):
    """参数不对是调用方的问题，回 400，别回 500 还留一段 traceback"""

    def test_log_limit_not_int(self):
        code, d = self.jget("/api/log?path=%s&limit=abc"
                            % urllib.parse.quote(self.root))
        self.assertEqual(code, 400, d)
        self.assertIn("整数", d.get("error", ""))

    def test_log_limit_zero_is_clamped(self):
        code, d = self.jget("/api/log?path=%s&limit=0"
                            % urllib.parse.quote(self.root))
        self.assertEqual(code, 200, d)
        self.assertTrue(d["commits"])

    def test_events_since_not_int(self):
        code, d = self.jget("/api/events?path=%s&since=xyz"
                            % urllib.parse.quote(self.root))
        self.assertEqual(code, 400, d)

    def test_search_limit_clamped(self):
        code, d = self.jget("/api/search?path=%s&q=%s&limit=999"
                            % (urllib.parse.quote(self.root), urllib.parse.quote("侧板")))
        self.assertEqual(code, 200, d)
        self.assertIn("rows", d)


class SearchRouteTest(Base):
    """/api/search 只剩一条路由（按零件名找历史），老的「搜提交说明」已删"""

    def test_search_returns_history_rows(self):
        code, d = self.jget("/api/search?path=%s&q=%s"
                            % (urllib.parse.quote(self.root), urllib.parse.quote("侧板")))
        self.assertEqual(code, 200, d)
        self.assertIn("rows", d)
        self.assertNotIn("commits", d)
        self.assertEqual(d["rows"][0]["path"], "侧板.sldprt")


class LockScopeTest(unittest.TestCase):
    """/api/pick 不能占着全局写锁（等人点「确定」可能几分钟）"""

    def test_pick_is_not_locked(self):
        from zgr.server import Handler
        self.assertIn("/api/pick", Handler.DESTRUCTIVE)     # 仍然只允许本机
        self.assertIn("/api/pick", Handler.NO_LOCK)

    def test_only_pick_skips_lock(self):
        from zgr.server import Handler
        # 别的写操作照样串行：这一条防止「顺手把 NO_LOCK 越加越大」
        self.assertEqual(Handler.NO_LOCK - {"/api/pick"}, set())


class RemoteGuiTest(Base):
    """拿着令牌的远程调用不能驱动这台机器的桌面/改全局设置"""

    token = "tok-66"
    local = False

    def _call(self, route, obj):
        return self.req(route, obj, headers={"X-ZGR-Token": self.token})

    def test_remote_openfolder_refused(self):
        code, body = self._call("/api/openfolder", {"path": self.root})
        self.assertEqual(code, 403, body[:200])
        self.assertIn("本机", body.decode("utf-8"))

    def test_remote_pick_refused(self):
        code, body = self._call("/api/pick", {"kind": "folder"})
        self.assertEqual(code, 403, body[:200])

    def test_remote_harmless_op_still_allowed(self):
        # 普通的读接口不受影响（远程管理还是能用的）
        code, d = self.jget("/api/projects", headers={"X-ZGR-Token": self.token})
        self.assertEqual(code, 200, d)


class IsLocalTest(unittest.TestCase):
    """「本机」的判定：回环地址 + 本机自己的网卡地址，别人的地址不算"""

    def _is_local(self, ip, names):
        from unittest import mock
        from zgr import server as srv
        fake = type("F", (), {"client_address": (ip, 5000), "is_local": srv.Handler.is_local})
        with mock.patch.object(srv, "allowed_hosts", lambda: names):
            return fake().is_local()

    def test_loopback(self):
        for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(self._is_local(ip, set()), ip)

    def test_own_lan_address_counts_as_local(self):
        # 用自己机器书签里的 10.x 地址打开界面，也要算本机
        self.assertTrue(self._is_local("192.168.1.20", {"192.168.1.20", "desktop-abc"}))

    def test_other_machine_is_not_local(self):
        self.assertFalse(self._is_local("192.168.1.20", {"192.168.1.20", "desktop-abc"}))

class CsvFormulaTest(unittest.TestCase):
    """CSV 公式注入：`=cmd|' /C calc'!A0.sldprt` 这种零件名要当文本"""

    def _js(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "web", "app.js"), encoding="utf-8") as f:
            return f.read()

    def test_cell_guard_exists(self):
        src = self._js()
        self.assertIn("function csvCell(", src)
        # 文本列才加前缀，数字列不能加（加了就变文本，排序/求和失效）
        self.assertIn("if (!numeric &&", src)
        self.assertIn("/^[=+\\-@\\t\\r]/.test(s)", src)

    def test_both_csv_exports_use_it(self):
        src = self._js()
        self.assertEqual(src.count("const esc2 = (v) => csvCell(v);"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
