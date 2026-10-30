# -*- coding: utf-8 -*-
"""回收没人引用的对象：项目只会越用越大，得有一条「安全地变小」的路

风险全在「删错了」上，所以这一组测试盯的是**不该删的一个都没删**：
分支上能走到的对象、刚进来还没被引用的对象（同步中途）、体检不过时的整体拒绝。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


class GcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-gc-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("回收")
        self.write("a.txt", "甲")
        self.c1 = self.repo.commit("第一版")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    # ---------------------------------------------------------------- 辅助
    def make_dangling(self, text="别人不要了"):
        """造一个「没挂在任何分支上」的对象（模拟删掉的分支留下的）"""
        oid = self.repo.put("blob", text.encode("utf-8"))
        return oid

    def age(self, oid, days):
        p = self.repo._obj_path(oid)
        t = time.time() - days * 86400
        os.utime(p, (t, t))

    def objects_on_disk(self):
        base = self.repo._p("objects")
        out = set()
        for d in os.listdir(base):
            sub = os.path.join(base, d)
            if os.path.isdir(sub):
                out.update(d + n for n in os.listdir(sub))
        return out

    # ---------------------------------------------------------------- 用例
    def test_dry_run_reports_without_deleting(self):
        oid = self.make_dangling()
        self.age(oid, 30)
        plan = self.repo.gc(dry=True)
        self.assertTrue(plan["dry"])
        self.assertGreaterEqual(plan["objects"], 1)
        self.assertEqual(plan["dangling"], 1)
        self.assertGreater(plan["bytes"], 0)
        self.assertIn(oid, plan["sample"])              # 试算要说得清「会删哪几个」
        self.assertIn(oid, self.objects_on_disk())      # 但一个都不删

    def test_real_run_removes_only_dangling(self):
        before = self.objects_on_disk()
        oid = self.make_dangling()
        self.age(oid, 30)
        after = self.objects_on_disk()
        extra = after - before
        self.assertEqual(extra, {oid})
        rep = self.repo.gc(dry=False)
        self.assertEqual(rep["removed"], 1)
        self.assertFalse(os.path.exists(self.repo._obj_path(oid)))
        # 分支上能走到的对象一个都不能少
        for keep in before:
            self.assertIn(keep, self.objects_on_disk(), keep)
        self.assertTrue(self.repo.fsck()["ok"])

    def test_recent_objects_are_kept(self):
        """刚进来的对象可能正处在同步中途（对象先到、分支指针后到），不能删"""
        oid = self.make_dangling()             # 就是「刚刚」，没改时间
        rep = self.repo.gc(dry=False)
        self.assertEqual(rep["removed"], 0)
        self.assertGreaterEqual(rep["kept_recent"], 1)
        self.assertTrue(os.path.exists(self.repo._obj_path(oid)))

    def test_keep_days_zero_removes_recent(self):
        oid = self.make_dangling()
        rep = self.repo.gc(dry=False, keep_days=0)
        self.assertEqual(rep["removed"], 1)
        self.assertFalse(os.path.exists(self.repo._obj_path(oid)))

    def test_refuses_when_fsck_fails(self):
        """对象库本身有问题时，宁可一个都不删（分不清哪些是真孤儿）"""
        orphan = self.make_dangling()
        self.age(orphan, 30)
        # 把分支头指向的提交弄坏：体检必失败，这时连孤儿也不能删（分不清谁是谁）
        p = self.repo._obj_path(self.c1)
        with open(p, "wb") as f:
            f.write(b"not a zlib object")
        with self.assertRaises(RepoError):
            self.repo.gc(dry=False)
        self.assertTrue(os.path.exists(self.repo._obj_path(orphan)))   # 孤儿也没删
        self.assertTrue(os.path.exists(self.repo._obj_path(self.c1)))  # 坏对象留给用户修

    def test_history_of_deleted_branch_is_reclaimable(self):
        """删掉的分支留下的历史：正是该回收的东西"""
        self.write("b.txt", "乙" * 500)
        c2 = self.repo.commit("第二版")
        self.repo._set_head(self.c1, self.repo.branch)   # 退回第一版，第二版成了孤儿
        dangling = self.repo.fsck()["dangling"]
        self.assertIn(c2, dangling)
        for o in dangling:
            self.age(o, 30)
        rep = self.repo.gc(dry=False)
        self.assertGreaterEqual(rep["removed"], 1)
        self.assertFalse(os.path.exists(self.repo._obj_path(c2)))
        self.assertTrue(self.repo.fsck()["ok"])


    def test_refuses_when_branch_pointer_lost(self):
        """分支指针没了 ≠ 对象都是垃圾：这种状态下清一次就是整库历史消失"""
        oid = self.make_dangling()
        self.age(oid, 30)
        os.remove(os.path.join(self.root, ".zgr", "refs", "heads", "main"))
        with self.assertRaises(RepoError) as e:
            self.repo.gc(dry=True)
        self.assertIn("分支指针是空的", str(e.exception))
        self.assertTrue(os.path.exists(self.repo._obj_path(oid)))

    def test_api_never_allows_zero_grace(self):
        """接口上强制留一天宽限：界面不会传 0，但不能让别的调用在同步途中清空"""
        oid = self.make_dangling()          # 刚写的对象
        import json as _json
        import urllib.request
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, token=None)
        handler = type("H2", (Handler,), {"app": app})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/api/gc" % httpd.server_address[1]
            req = urllib.request.Request(url, data=_json.dumps(
                {"path": self.repo.root, "keep_days": 0}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                res = _json.loads(r.read().decode("utf-8"))
            self.assertEqual(res["plan"]["removed"], 0)
            self.assertEqual(res["plan"]["keep_days"], 1)
            self.assertTrue(os.path.exists(self.repo._obj_path(oid)))
        finally:
            httpd.shutdown()
            httpd.server_close()


class GcApiTest(unittest.TestCase):
    """/api/gc：GET 只报数、POST 才真删（和体检一样，看一眼不能改动任何东西）"""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-gcapi-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("回收接口")
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write("甲")
        self.repo.commit("第一版")
        self.orphan = self.repo.put("blob", b"old")
        t = time.time() - 30 * 86400
        os.utime(self.repo._obj_path(self.orphan), (t, t))

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, token=None)
        handler = type("H", (Handler,), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, obj=None):
        import json as _json
        import urllib.request
        data = None if obj is None else _json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return _json.loads(r.read().decode("utf-8"))

    def test_get_reports_and_post_removes(self):
        q = urllib.parse.quote(self.root)
        r = self.call("/api/gc?path=" + q)
        self.assertTrue(r["ok"])
        self.assertEqual(r["plan"]["total"], 1)
        self.assertTrue(r["plan"]["dry"])
        self.assertTrue(os.path.exists(self.repo._obj_path(self.orphan)))  # 看一眼不动手
        # 不写 yes 的 POST 也只是演练（删除不可逆，空 body 不能真删）
        r_dry = self.call("/api/gc", {"path": self.root})
        self.assertEqual(r_dry["plan"]["removed"], 0)
        self.assertTrue(os.path.exists(self.repo._obj_path(self.orphan)))
        r2 = self.call("/api/gc", {"path": self.root, "yes": True})
        self.assertEqual(r2["plan"]["removed"], 1)
        self.assertFalse(os.path.exists(self.repo._obj_path(self.orphan)))
        self.assertTrue(self.repo.fsck()["ok"])




if __name__ == "__main__":
    unittest.main(verbosity=2)
