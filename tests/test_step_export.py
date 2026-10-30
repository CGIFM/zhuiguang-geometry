# -*- coding: utf-8 -*-
"""零件一键导出 STEP：命名、目录体检、SolidWorks 运行输出解析、接口

真机导出要有 SolidWorks（167/174 上验），这里把 cscript 那一层换成假的，
验证「我们这一层」：路径怎么拼、失败怎么记、进度怎么走、接口挡住哪些请求。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import swstep  # noqa: E402


class NamesTest(unittest.TestCase):
    def test_no_collision(self):
        n = swstep.out_names(["a/底板.sldprt", "b/侧板.sldasm"])
        self.assertEqual(n["a/底板.sldprt"], "底板.step")
        self.assertEqual(n["b/侧板.sldasm"], "侧板.step")

    def test_same_name_different_dirs(self):
        n = swstep.out_names(["左件/底板.sldprt", "右件/底板.sldprt", "总装.sldasm"])
        self.assertEqual(n["左件/底板.sldprt"], "左件_底板.step")
        self.assertEqual(n["右件/底板.sldprt"], "右件_底板.step")
        self.assertEqual(n["总装.sldasm"], "总装.step")

    def test_three_layers_still_unique(self):
        n = swstep.out_names(["a/底板.sldprt", "b/底板.sldprt", "c/底板.sldprt"])
        outs = sorted(n.values())
        self.assertEqual(len(set(x.lower() for x in outs)), 3, outs)

    def test_bad_chars_removed(self):
        n = swstep.out_names(["底板:改.sldprt"])
        self.assertNotIn(":", n["底板:改.sldprt"].replace(".step", ""))


class DestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-step-dest-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_rejected(self):
        with self.assertRaises(swstep.StepError):
            swstep.check_dest("")

    def test_file_rejected(self):
        p = os.path.join(self.tmp, "x.txt")
        open(p, "w").close()
        with self.assertRaises(swstep.StepError):
            swstep.check_dest(p)

    def test_project_root_rejected(self):
        with self.assertRaises(swstep.StepError) as e:
            swstep.check_dest(self.root, self.root)
        self.assertIn("项目根目录", str(e.exception))

    def test_created_when_missing(self):
        d = os.path.join(self.tmp, "STEP导出")
        self.assertEqual(swstep.check_dest(d, self.root), os.path.abspath(d))
        self.assertTrue(os.path.isdir(d))


class _FakeProc:
    """假的 cscript：按剧本吐行，同时把输出文件写出来"""

    def __init__(self, lines, writer):
        self.stdout = iter(lines)
        self.returncode = 0
        self._writer = writer

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class _HangProc:
    """假的 cscript：吐几行以后就不动了（模拟弹了个看不见的对话框）"""

    def __init__(self, lines):
        self._lines = list(lines)
        self._dead = False
        self.returncode = 1
        self.stdout = self._gen()

    def _gen(self):
        for ln in self._lines:
            yield ln
        while not self._dead:
            time.sleep(0.05)

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self._dead = True


class ExportRunTest(unittest.TestCase):
    def setUp(self):
        # 别真等 60 秒落盘：测试里把等待压到 1 秒
        settle = mock.patch.object(swstep, "SETTLE", 1.0)
        settle.start()
        self.addCleanup(settle.stop)
        self.tmp = tempfile.mkdtemp(prefix="zgr-step-run-")
        self.root = os.path.join(self.tmp, "项目")
        self.dest = os.path.join(self.tmp, "STEP")
        os.makedirs(os.path.join(self.root, "零件"))
        self.a = os.path.join(self.root, "零件", "底板.sldprt")
        self.b = os.path.join(self.root, "零件", "侧板.sldprt")
        for p in (self.a, self.b):
            # 一定要 with 关掉：Windows 上句柄没关，tearDown 的 rmtree 会静默失败，
            # 临时目录就留在盘上（这台机器累积过上千个，吃掉 2.8 GB）
            with open(p, "wb") as f:
                f.write(b"fake")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, lines, writer=None):
        calls = []
        seq = {"i": 0}

        def fake_popen(cmd, **kw):
            calls.append(cmd)
            # 模拟 SolidWorks 写出 STEP
            job = os.path.join(os.path.dirname(cmd[cmd.index(next(
                c for c in cmd if c.endswith(".vbs"))) + 1]), "job.txt")
            with open(job, encoding="utf-16-le") as f:
                for ln in f:
                    src, dst = ln.rstrip("\r\n").split("\t")
                    if writer:
                        writer(src, dst)
            return _FakeProc(lines, writer)

        out = []
        with mock.patch.object(swstep.subprocess, "Popen", fake_popen), \
             mock.patch.object(swstep.mesh, "_sw_gate", _no_gate), \
             mock.patch.object(swstep.mesh, "_sldworks_pids", return_value=set()), \
             mock.patch.object(swstep.mesh, "_wait_exit", lambda *a, **k: None), \
             mock.patch.object(swstep.mesh, "_cleanup_own", lambda *a, **k: None):
            res = swstep.export([("零件/底板.sldprt", self.a), ("零件/侧板.sldprt", self.b)],
                                self.dest, progress=lambda *a, **k: out.append(a),
                                log=lambda *a: None)
        return res, out, calls

    def test_all_ok(self):
        def writer(src, dst):
            with open(dst, "wb") as f:
                f.write(b"STEP-BYTES")

        res, prog, calls = self._run(["OK 0\n", "OK 1\n", "DONE 2\n"], writer)
        self.assertEqual(sorted(res["ok"]), ["零件/侧板.sldprt", "零件/底板.sldprt"])
        self.assertEqual(res["failed"], [])
        self.assertEqual(res["files"], 2)
        # 导出清单要能对上「源文件 → 实际写出的 STEP 名」，只有源路径是没用的
        self.assertEqual(res["outputs"], {"零件/底板.sldprt": "底板.step",
                                          "零件/侧板.sldprt": "侧板.step"})
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "底板.step")))
        self.assertTrue(any("cscript" in c[0].lower() or c[0].endswith("cscript.exe")
                            for c in calls))
        self.assertTrue(prog, "应该有进度回调")

    def test_one_failure_keeps_going(self):
        def writer(src, dst):
            if src.endswith("侧板.sldprt"):
                return                      # 模拟没写出来
            with open(dst, "wb") as f:
                f.write(b"STEP-BYTES")

        res, _p, _c = self._run(
            ["OK 0\n", "ERR 1 save 5 文件只读\n", "DONE 2\n"], writer)
        self.assertEqual(res["ok"], ["零件/底板.sldprt"])
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(res["failed"][0]["rel"], "零件/侧板.sldprt")
        self.assertIn("STEP 失败", res["failed"][0]["error"])

    def test_reported_ok_but_empty_file(self):
        """SolidWorks 说成功了，但文件是 0 字节：按失败算，不能骗用户"""
        def writer(src, dst):
            open(dst, "wb").close()

        res, _p, _c = self._run(["OK 0\n", "OK 1\n", "DONE 2\n"], writer)
        self.assertEqual(res["ok"], [])
        self.assertEqual(len(res["failed"]), 2)
        self.assertIn("空的", res["failed"][0]["error"])


    def test_file_appears_late_is_still_ok(self):
        """SolidWorks 说导完了，文件晚半秒才落地：不能判失败"""
        def writer(src, dst):
            def late():
                time.sleep(0.6)
                with open(dst, "wb") as f:
                    f.write(b"STEP-BYTES")
            threading.Thread(target=late, daemon=True).start()

        res, _p, _c = self._run(["OK 0\n", "OK 1\n", "DONE 2\n"], writer)
        self.assertEqual(sorted(res["ok"]), ["零件/侧板.sldprt", "零件/底板.sldprt"])
        self.assertEqual(res["failed"], [])


    def test_reports_ok_but_no_file_is_retried_alone(self):
        """整批里报成功却没写出文件：单独补导一次，第二次写出来就算成功"""
        rounds = []

        def fake_popen(cmd, **kw):
            jobfile = cmd[4]
            with open(jobfile, encoding="utf-16-le") as f:
                rows = [x.rstrip("\r\n") for x in f if x.strip()]
            rounds.append(len(rows))
            out = []
            for i, row in enumerate(rows):
                _src, dst = row.split("\t")
                if len(rounds) > 1:          # 补导这一轮才真的写出文件
                    with open(dst, "wb") as fh:
                        fh.write(b"STEP")
                out.append("BEGIN %d\n" % i)
                out.append("OK %d\n" % i)
            out.append("DONE %d\n" % len(rows))
            return _FakeProc(out, None)

        with mock.patch.object(swstep.subprocess, "Popen", fake_popen), \
             mock.patch.object(swstep.mesh, "_sw_gate", _no_gate), \
             mock.patch.object(swstep.mesh, "_sldworks_pids", return_value=set()), \
             mock.patch.object(swstep.mesh, "_wait_exit", lambda *a, **k: None), \
             mock.patch.object(swstep.mesh, "_cleanup_own", lambda *a, **k: None):
            res = swstep.export([("零件/底板.sldprt", self.a),
                                 ("零件/侧板.sldprt", self.b)],
                                self.dest, log=lambda *a: None)
        self.assertEqual(rounds, [2, 1, 1], "先整批导一次，没写出来的各补一次")
        self.assertEqual(sorted(res["ok"]), ["零件/侧板.sldprt", "零件/底板.sldprt"])
        self.assertEqual(res["failed"], [])


    def test_one_stuck_file_does_not_kill_the_batch(self):
        """某个零件把 SolidWorks 卡住：只把那一个记失败，后面的接着导"""
        stall = mock.patch.object(swstep, "STALL", 0.6)
        stall.start()
        self.addCleanup(stall.stop)
        runs = []

        def fake_popen(cmd, **kw):
            jobfile = cmd[4]
            with open(jobfile, encoding="utf-16-le") as f:
                rows = [x.rstrip("\r\n") for x in f if x.strip()]
            runs.append(len(rows))
            if len(runs) == 1:
                return _HangProc(["BEGIN 0\n"])
            out = []
            for i, row in enumerate(rows):
                src, dst = row.split("\t")
                with open(dst, "wb") as fh:
                    fh.write(b"STEP")
                out.append("BEGIN %d\n" % i)
                out.append("OK %d\n" % i)
            out.append("DONE %d\n" % len(rows))
            return _FakeProc(out, None)

        with mock.patch.object(swstep.subprocess, "Popen", fake_popen), \
             mock.patch.object(swstep.mesh, "_sw_gate", _no_gate), \
             mock.patch.object(swstep.mesh, "_sldworks_pids", return_value=set()), \
             mock.patch.object(swstep.mesh, "_wait_exit", lambda *a, **k: None), \
             mock.patch.object(swstep.mesh, "_cleanup_own", lambda *a, **k: None):
            res = swstep.export([("零件/底板.sldprt", self.a),
                                 ("零件/侧板.sldprt", self.b)],
                                self.dest, log=lambda *a: None)
        self.assertEqual(runs, [2, 1], "第一次导两个，卡住后从下一个重来")
        self.assertEqual(res["ok"], ["零件/侧板.sldprt"])
        self.assertEqual([x["rel"] for x in res["failed"]], ["零件/底板.sldprt"])
        self.assertIn("卡住", res["failed"][0]["error"])

    def test_missing_source_reported(self):
        out = []
        with mock.patch.object(swstep.mesh, "_sw_gate", _no_gate), \
             mock.patch.object(swstep.mesh, "_sldworks_pids", return_value=set()), \
             mock.patch.object(swstep.mesh, "_wait_exit", lambda *a, **k: None), \
             mock.patch.object(swstep.mesh, "_cleanup_own", lambda *a, **k: None), \
             mock.patch.object(swstep.subprocess, "Popen",
                               lambda *a, **k: _FakeProc(["DONE 0\n"], None)):
            res = swstep.export([("零件/没了.sldprt", os.path.join(self.root, "没了.sldprt"))],
                                self.dest, progress=lambda *a: out.append(a),
                                log=lambda *a: None)
        self.assertEqual(res["ok"], [])
        self.assertIn("找不到", res["failed"][0]["error"])

    def test_solidworks_open_is_refused(self):
        with mock.patch.object(swstep.mesh, "_sw_gate", _no_gate), \
             mock.patch.object(swstep.mesh, "_sldworks_pids", return_value={123}):
            with self.assertRaises(swstep.StepError) as e:
                swstep.export([("零件/底板.sldprt", self.a)], self.dest,
                              log=lambda *a: None)
        self.assertIn("正开着", str(e.exception))


import contextlib                          # noqa: E402


@contextlib.contextmanager
def _no_gate(*a, **k):
    yield


def _BaseHandler():
    from zgr.server import Handler
    return Handler


class StepEndpointTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App
        from zgr.store import Repo

        self.tmp = tempfile.mkdtemp(prefix="zgr-step-api-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        repo = Repo(self.root).init("导测试")
        for n in ("底板.sldprt", "侧板.sldasm", "说明.txt"):
            with open(os.path.join(self.root, n), "w", encoding="utf-8") as f:
                f.write(n)
        repo.commit("起点")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        reg.add(self.root, "导测试")
        app = App(reg, default_project=self.root)
        handler = type("H", (_BaseHandler(),), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.app = app

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
                return e.code, json.loads(raw).get("error") or raw
            except ValueError:
                return e.code, raw
        self.fail("预期失败：%s %s" % (route, body))

    # ---- 校验
    def test_rejects_non_solidworks(self):
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": ["说明.txt"], "dest": os.path.join(self.tmp, "o")})
        self.assertEqual(code, 400)
        self.assertIn("SolidWorks", msg)

    def test_rejects_path_traversal(self):
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": ["../../etc/底板.sldprt"],
            "dest": os.path.join(self.tmp, "o")})
        self.assertEqual(code, 400)

    def test_rejects_empty(self):
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": [], "dest": os.path.join(self.tmp, "o")})
        self.assertEqual(code, 400)
        self.assertIn("没有选中", msg)

    def test_rejects_project_root_as_dest(self):
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": ["底板.sldprt"], "dest": self.root})
        self.assertEqual(code, 400)
        self.assertIn("项目根目录", msg)

    # ---- 跑起来（把真 SolidWorks 换掉）
    def test_job_progress_and_result(self):
        dest = os.path.join(self.tmp, "STEP")
        fake = {"ok": ["底板.sldprt", "侧板.sldasm"],
                "failed": [{"rel": "x.sldprt", "error": "缺引用件", "out": "x.step"}],
                "dest": dest, "files": 2, "seconds": 1.0}
        with mock.patch.object(swstep, "export", return_value=fake):
            d = self.post("/api/export/step", {
                "path": self.root, "files": ["底板.sldprt", "侧板.sldasm"], "dest": dest})
            jid = d["job"]["id"]
            deadline = time.time() + 15
            st = None
            while time.time() < deadline:
                with urllib.request.urlopen(
                        self.url + "/api/export/step/status?id=" + jid, timeout=10) as r:
                    st = json.loads(r.read().decode("utf-8"))["job"]
                if st["state"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["result"]["files"], 2)
        self.assertEqual(len(st["result"]["failed"]), 1)

    def test_dedupes_and_ignores_blank(self):
        dest = os.path.join(self.tmp, "STEP2")
        with mock.patch.object(swstep, "export", return_value={
                "ok": [], "failed": [], "dest": dest, "files": 0, "seconds": 0}):
            d = self.post("/api/export/step", {
                "path": self.root,
                "files": ["底板.sldprt", "./底板.sldprt", "", "侧板.sldasm"],
                "dest": dest})
        self.assertEqual(d["job"]["total"], 2)

    # ---- 一台机器同时只能动一个 SolidWorks（防呆，也是真会互相踩）
    def test_refuses_while_another_step_running(self):
        from zgr.server import StepJob
        j = StepJob(self.root, [("底板.sldprt", os.path.join(self.root, "底板.sldprt"))],
                    os.path.join(self.tmp, "x"))
        self.app.steps[j.id] = j                 # 只登记成 running，不起线程
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": ["底板.sldprt"], "dest": os.path.join(self.tmp, "o")})
        self.assertEqual(code, 400)
        self.assertIn("还在跑", msg)
        self.assertIn("导出 STEP", msg)
        j.state = "done"                         # 跑完了就能再来
        with mock.patch.object(swstep, "export",
                               return_value={"ok": [], "failed": [], "files": 0,
                                             "dest": os.path.join(self.tmp, "o"),
                                             "seconds": 0}):
            d = self.post("/api/export/step", {
                "path": self.root, "files": ["底板.sldprt"],
                "dest": os.path.join(self.tmp, "o")})
        self.assertTrue(d["ok"])

    def test_refuses_while_bom_running(self):
        """BOM 在跑的时候不许开 STEP —— 两边会接到同一个 SolidWorks 实例上"""
        from zgr.server import BomJob
        j = BomJob(self.root, os.path.join(self.root, "侧板.sldasm"),
                   os.path.join(self.tmp, "bom"))
        self.app.boms[j.id] = j
        code, msg = self.post_err("/api/export/step", {
            "path": self.root, "files": ["底板.sldprt"], "dest": os.path.join(self.tmp, "o")})
        self.assertEqual(code, 400)
        self.assertIn("物料清单", msg)
        j.state = "done"

    def test_status_unknown_404(self):
        try:
            urllib.request.urlopen(self.url + "/api/export/step/status?id=nope", timeout=10)
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        else:
            self.fail("未知任务应该 404")

    # ---- 修改/创建时间
    def test_filetimes(self):
        p = os.path.join(self.root, "底板.sldprt")
        os.utime(p, (1700000000, 1700000000))
        d = self.post("/api/filetimes", {"path": self.root,
                                         "files": ["底板.sldprt", "不存在.sldprt",
                                                   "../越界.sldprt"]})
        self.assertTrue(d["ok"])
        self.assertIn("底板.sldprt", d["times"])
        self.assertEqual(d["times"]["底板.sldprt"]["m"], 1700000000000)
        self.assertNotIn("不存在.sldprt", d["times"])
        self.assertEqual(len(d["times"]), 1)


class JobCleanupTest(unittest.TestCase):
    """后台任务对象要会自己清：每个 BOM 把整张表存在内存里，只增不减会吃掉几百 MB"""

    class _J:
        def __init__(self, i, state, age):
            self.id = i
            self.state = state
            self.started = time.time() - age

    def test_keeps_recent_done_and_running(self):
        from zgr.server import _prune_jobs
        jobs = {}
        for i in range(12):
            jobs["a%d" % i] = self._J("a%d" % i, "done", 100 + i)   # a0 最新
        jobs["run"] = self._J("run", "running", 0)
        _prune_jobs(jobs, 8)
        self.assertIn("run", jobs)
        self.assertEqual(len(jobs), 9)
        self.assertIn("a0", jobs)
        self.assertNotIn("a11", jobs)                                # 最旧的丢了

    def test_drops_stale_even_if_done(self):
        from zgr.server import _prune_jobs
        jobs = {"old": self._J("old", "done", 7 * 3600)}
        _prune_jobs(jobs, 8)
        self.assertEqual(jobs, {})

    def test_noop_when_few(self):
        from zgr.server import _prune_jobs
        jobs = {"a": self._J("a", "done", 5)}
        _prune_jobs(jobs, 8)
        self.assertIn("a", jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
