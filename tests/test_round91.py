# -*- coding: utf-8 -*-
"""第 91 轮：回收站里挑一个文件恢复

回收站原来只有「整批复原」这一挡：只丢了 `底盘.sldprt` 一个零件，却要把当时
被覆盖的全部文件一起盖回去——把别人的改动也一起改回旧版，这比丢文件更糟。

现在能先看清一批备份里有什么，再挑一个恢复；恢复前当前这个文件同样另存一批
「单文件-时间」备份，恢复错了还能再恢复回来。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import live as live_mod     # noqa: E402
from zgr.store import Repo, RepoError  # noqa: E402


def write(root, rel, text):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "r", encoding="utf-8") as f:
        return f.read()


class BackupFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-bkf-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("回收站")
        write(self.root, "底盘.sldprt", "旧底盘")
        write(self.root, "零件/件001.sldprt", "旧件")
        self.repo.commit("第一版", "我")
        # 造一批备份：把两个文件按旧内容存进去，工作区改成新内容
        self.batch = "20261030-120000"
        src = os.path.join(self.repo.meta, "backup", self.batch)
        write(src, "底盘.sldprt", "旧底盘")
        write(src, "零件/件001.sldprt", "旧件")
        write(src, ".zgr/config.json", "不该出现")
        write(self.root, "底盘.sldprt", "新底盘")
        write(self.root, "零件/件001.sldprt", "新件")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lists_files_without_meta(self):
        d = live_mod.backup_files(self.repo, self.batch)
        paths = [x["path"] for x in d["files"]]
        self.assertEqual(paths, ["底盘.sldprt", "零件/件001.sldprt"])
        self.assertEqual(d["total"], 2)
        self.assertFalse(d["truncated"])
        self.assertTrue(all(x["size"] > 0 for x in d["files"]))

    def test_limit_marks_truncated(self):
        d = live_mod.backup_files(self.repo, self.batch, limit=1)
        self.assertEqual(len(d["files"]), 1)
        self.assertEqual(d["total"], 2)
        self.assertTrue(d["truncated"])

    def test_restore_one_leaves_the_other_alone(self):
        res = live_mod.restore_backup_file(self.repo, self.batch, "底盘.sldprt")
        self.assertEqual(res["file"], "底盘.sldprt")
        self.assertEqual(read(self.root, "底盘.sldprt"), "旧底盘")
        # 另一个文件一个字节都不能动——这正是「整批复原」做不到的事
        self.assertEqual(read(self.root, "零件/件001.sldprt"), "新件")

    def test_restore_keeps_a_safety_copy(self):
        res = live_mod.restore_backup_file(self.repo, self.batch, "底盘.sldprt")
        self.assertTrue(res["safety_backup"])
        keep = os.path.join(self.repo.meta, "backup", res["safety_backup"])
        with open(os.path.join(keep, "底盘.sldprt"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "新底盘")   # 恢复的是「恢复前」的内容

    def test_no_backup_when_asked(self):
        res = live_mod.restore_backup_file(self.repo, self.batch, "底盘.sldprt",
                                           backup=False)
        self.assertEqual(res["safety_backup"], "")
        self.assertEqual(read(self.root, "底盘.sldprt"), "旧底盘")

    def test_missing_file_is_clear(self):
        with self.assertRaises(RepoError) as cm:
            live_mod.restore_backup_file(self.repo, self.batch, "没有这个.sldprt")
        self.assertIn("没有这个文件", str(cm.exception))

    def test_bad_batch_name_rejected(self):
        for bad in ("", "..", "../backup", "a/b", "a\\b"):
            with self.assertRaises(RepoError):
                live_mod.backup_files(self.repo, bad)
            with self.assertRaises(RepoError):
                live_mod.restore_backup_file(self.repo, bad, "底盘.sldprt")

    def test_bad_path_rejected(self):
        """恢复的路径不能借 `..` 或盘符跑出项目（备份文件也可能被恶意塞进来）"""
        for bad in ("../外面.txt", "C:/Windows/win.ini", "", "/绝对路径"):
            with self.assertRaises(RepoError):
                live_mod.restore_backup_file(self.repo, self.batch, bad)

    def test_creates_parent_dir(self):
        write(os.path.join(self.repo.meta, "backup", self.batch), "新目录/新件.sldprt", "备份里的")
        live_mod.restore_backup_file(self.repo, self.batch, "新目录/新件.sldprt")
        self.assertEqual(read(self.root, "新目录/新件.sldprt"), "备份里的")


class BackupApiTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App, Handler

        self.tmp = tempfile.mkdtemp(prefix="zgr-bkapi-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("接口")
        write(self.root, "a.txt", "新内容")
        self.repo.commit("第一版", "我")
        self.batch = "20261030-121500"
        write(os.path.join(self.repo.meta, "backup", self.batch), "a.txt", "旧内容")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        reg.add(self.root, "接口")
        self.app = App(reg)
        handler = type("H", (Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        for w in list(self.app.watchers.values()):
            w.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, route, **params):
        import urllib.parse
        q = urllib.parse.urlencode(params)
        with urllib.request.urlopen(self.base + route + "?" + q, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, route, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + route, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_files_then_restore_one(self):
        d = self.get("/api/backup/files", path=self.root, name=self.batch)
        self.assertTrue(d["ok"])
        self.assertEqual([x["path"] for x in d["files"]], ["a.txt"])
        r = self.post("/api/backup/restore_file",
                      {"path": self.root, "name": self.batch, "file": "a.txt"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(read(self.root, "a.txt"), "旧内容")

    def test_restore_is_in_destructive_set(self):
        """/api/backup/restore_file 会写工作区：跨机必须带令牌（和整批复原同一道关）"""
        from zgr.server import Handler
        self.assertIn("/api/backup/restore_file", Handler.DESTRUCTIVE)

    def test_bad_request_is_400(self):
        import urllib.error
        try:
            self.post("/api/backup/restore_file",
                      {"path": self.root, "name": self.batch, "file": "../x.txt"})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)
        else:
            self.fail("越界路径必须被拒绝")


if __name__ == "__main__":
    unittest.main()
