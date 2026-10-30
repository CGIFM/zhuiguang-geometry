# -*- coding: utf-8 -*-
"""POST /api/pick：选文件夹 / 选文件走 Windows 自带对话框

这里不打真对话框（跑测试时弹窗会把人烦死，也会把用例挂住），而是把
nativepick.pick / desktop_ready 换掉，验证接口这一层：
  · kind 只认 folder / files
  · 没有可操作桌面时返回 409，前端据此退回网页里的目录树
  · 取消（选出来是空）也要是 ok，前端好区分「取消」和「出错」
  · initial / multiple / title 原样传下去
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from zgr import nativepick


def _BaseHandler():
    from zgr.server import Handler
    return Handler


class PickEndpointTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tempfile.mkdtemp(prefix="zgr-pick-")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg)
        handler = type("H", (_BaseHandler(),), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, route, body):
        req = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def post_err(self, route, body):
        try:
            self.post(route, body)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return e.code, (json.loads(raw).get("error") or raw)
            except ValueError:
                return e.code, raw
        self.fail("预期失败，实际成功了")

    def test_folder_roundtrip(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick", return_value=["D:\\项目\\某目录"]) as m:
            d = self.post("/api/pick", {"kind": "folder", "initial": "D:\\项目"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["paths"], ["D:\\项目\\某目录"])
        self.assertFalse(d["canceled"])
        self.assertEqual(d["kind"], "folder")
        kwargs = m.call_args.kwargs
        self.assertEqual(kwargs["kind"], "folder")
        self.assertEqual(kwargs["initial"], "D:\\项目")
        self.assertFalse(kwargs["multiple"])

    def test_files_multiple_passthrough(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick", return_value=["C:\\a.sldprt",
                                                                "C:\\b.sldprt"]) as m:
            d = self.post("/api/pick", {"kind": "files", "multiple": True,
                                        "title": "选文件", "initial": "C:\\"})
        self.assertEqual(len(d["paths"]), 2)
        self.assertTrue(m.call_args.kwargs["multiple"])
        self.assertEqual(m.call_args.kwargs["title"], "选文件")

    def test_cancel_is_ok_not_error(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick", return_value=[]):
            d = self.post("/api/pick", {"kind": "folder"})
        self.assertTrue(d["ok"])
        self.assertEqual(d["paths"], [])
        self.assertTrue(d["canceled"])

    def test_no_desktop_is_409(self):
        with mock.patch.object(nativepick, "desktop_ready",
                               return_value=(False, "程序跑在系统会话里（会话 0）")):
            code, msg = self.post_err("/api/pick", {"kind": "folder"})
        self.assertEqual(code, 409)
        self.assertIn("会话 0", msg)

    def test_busy_dialog_is_429(self):
        """已经有一个框开着：429（前端只提示、不退回网页目录树，免得弹出两个选择器）"""
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick",
                               side_effect=nativepick.PickBusy("已经有一个选择对话框开着")):
            code, msg = self.post_err("/api/pick", {"kind": "folder"})
        self.assertEqual(code, 429)
        self.assertIn("对话框", msg)

    def test_pick_unavailable_is_409(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick",
                               side_effect=nativepick.PickUnavailable("远程桌面会话已断开")):
            code, msg = self.post_err("/api/pick", {"kind": "folder"})
        self.assertEqual(code, 409)
        self.assertIn("断开", msg)

    def test_bad_kind_rejected(self):
        code, msg = self.post_err("/api/pick", {"kind": "everything"})
        self.assertEqual(code, 400)
        self.assertIn("folder", msg)

    def test_ready_endpoint(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(False, "会话 0")):
            with urllib.request.urlopen(self.url + "/api/pick/ready", timeout=20) as r:
                d = json.loads(r.read().decode("utf-8"))
        self.assertTrue(d["ok"])
        self.assertFalse(d["ready"])
        self.assertEqual(d["reason"], "会话 0")
        self.assertIn("session", d)

    def test_com_failure_reports_500(self):
        with mock.patch.object(nativepick, "desktop_ready", return_value=(True, "")), \
             mock.patch.object(nativepick, "pick", side_effect=OSError("创建文件对话框 失败：0x80004005")):
            code, msg = self.post_err("/api/pick", {"kind": "folder"})
        self.assertEqual(code, 500)
        self.assertIn("打不开", msg)


class DesktopReadyTest(unittest.TestCase):
    def test_returns_pair(self):
        ok, why = nativepick.desktop_ready()
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(why, str)
        if ok:
            self.assertEqual(why, "")

    def test_self_test_does_not_show_dialog(self):
        """self_test 只建对象、设选项、关掉，不弹窗——部署后可以放心跑"""
        if os.name != "nt":
            self.skipTest("只有 Windows 有系统对话框")
        ok, _ = nativepick.desktop_ready()
        if not ok:
            self.skipTest("当前没有可操作桌面")
        self.assertTrue(nativepick.self_test())


if __name__ == "__main__":
    unittest.main(verbosity=2)
