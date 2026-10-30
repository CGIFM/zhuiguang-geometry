"""存储引擎自测：python -m unittest tests.test_store"""
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr.store import Repo, RepoError  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-test-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("测试项目")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_object_id_format(self):
        oid = self.repo.put("blob", b"hello\n")
        self.assertEqual(len(oid), 64)
        raw = zlib.decompress(self.repo.object_bytes(oid))
        self.assertEqual(raw, b"blob\n6\nhello\n")
        kind, payload = self.repo.get(oid)
        self.assertEqual((kind, payload), ("blob", b"hello\n"))

    def test_commit_and_log(self):
        self.write("a.txt", "第一版")
        self.write("子目录/b.txt", "内容")
        oid = self.repo.commit("第一次记录")
        self.assertTrue(oid and len(oid) == 64)
        c = self.repo.commit_info(oid)
        self.assertEqual(c["message"], "第一次记录")
        self.assertEqual(c["stats"]["files"], 2)
        log = self.repo.log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["id"], oid)

    def test_no_change_no_commit(self):
        self.write("a.txt", "x")
        self.assertIsNotNone(self.repo.commit("1"))
        self.assertIsNone(self.repo.commit("2"))

    def test_status_and_diff(self):
        self.write("a.txt", "one")
        self.write("b.txt", "bee")
        first = self.repo.commit("v1")
        self.assertTrue(self.repo.status()["clean"])
        self.write("a.txt", "one changed")
        self.write("c.txt", "new")
        os.remove(os.path.join(self.root, "b.txt"))
        st = self.repo.status()
        self.assertEqual(st["modified"], ["a.txt"])
        self.assertEqual(st["added"], ["c.txt"])
        self.assertEqual(st["deleted"], ["b.txt"])
        second = self.repo.commit("v2")
        d = self.repo.diff(first, second)
        self.assertEqual(d["counts"], {"added": 1, "deleted": 1, "modified": 1})
        # 逐行对比
        _, f1 = self.repo.files_of(first)
        _, f2 = self.repo.files_of(second)
        text = self.repo.diff_text(f1["a.txt"]["oid"], f2["a.txt"]["oid"], "a.txt")
        self.assertFalse(text["binary"])
        self.assertTrue(any(ln.startswith("-one") for ln in text["lines"]))
        self.assertTrue(any(ln.startswith("+one changed") for ln in text["lines"]))

    def test_restore_with_backup(self):
        self.write("a.txt", "旧")
        c1 = self.repo.commit("v1")
        self.write("a.txt", "新")
        self.write("extra.txt", "多出来的")
        self.repo.commit("v2")
        res = self.repo.restore(c1)
        self.assertEqual(res["written"], 1)
        self.assertIn("extra.txt", res["removed"])
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "旧")
        self.assertFalse(os.path.exists(os.path.join(self.root, "extra.txt")))
        self.assertTrue(os.path.isdir(res["backup"]))
        # 备份里应有被覆盖的“新”和删除的 extra
        with open(os.path.join(res["backup"], "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "新")

    def test_restore_other_target_mirror(self):
        """恢复到别的目录时，镜像删除也作用于目标目录，且先备份"""
        self.write("a.txt", "1")
        c = self.repo.commit("v")
        other = os.path.join(self.tmp, "别处")
        os.makedirs(other)
        with open(os.path.join(other, "多余.txt"), "w", encoding="utf-8") as f:
            f.write("先留着")
        res = self.repo.restore(c, other, mirror=True, backup=True)
        self.assertFalse(os.path.exists(os.path.join(other, "多余.txt")))
        self.assertTrue(os.path.isfile(os.path.join(other, "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(res["backup"], "多余.txt")))
        # 项目自己的目录不受影响
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a.txt")))

    def test_export_to_new_dir(self):
        self.write("x/a.txt", "A")
        c = self.repo.commit("v")
        out = os.path.join(self.tmp, "导出")
        res = self.repo.restore(c, out, mirror=False, backup=False)
        self.assertEqual(res["written"], 1)
        with open(os.path.join(out, "x", "a.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "A")
        self.assertFalse(os.path.exists(os.path.join(out, ".zgr")))

    def test_marks_and_tags(self):
        self.write("a", "1")
        c = self.repo.commit("v")
        self.repo.set_mark(c, "red", ["客户", "定稿"])
        log = self.repo.log()
        self.assertEqual(log[0]["mark"], "red")
        self.assertEqual(log[0]["tags"], ["客户", "定稿"])
        self.repo.set_mark(c, "none", [])
        log = self.repo.log()
        self.assertIsNone(log[0]["mark"])
        self.assertEqual(log[0]["tags"], [])

    def test_ignore_rules(self):
        self.write("keep.txt", "1")
        self.write("skip.log", "2")
        self.write("dist/out.bin", "3")
        cfg = self.repo.config
        cfg["ignore"] = list(cfg["ignore"]) + ["*.log", "dist"]
        self.repo._write_json("config.json", cfg)
        oid = self.repo.commit("v")
        _, files = self.repo.files_of(oid)
        self.assertEqual(sorted(files), ["keep.txt"])

    def test_cache_reuse_and_corruption_detection(self):
        p = self.write("a.txt", "hello")
        c1 = self.repo.commit("v1")
        st = os.stat(p)
        # 意图不改，索引应命中缓存
        files, _ = self.repo.build(write=True)
        self.assertEqual(files["a.txt"]["mtime"], st.st_mtime_ns)
        # 篡改对象应被识别
        _, files = self.repo.files_of(c1)
        oid = files["a.txt"]["oid"]
        objp = self.repo._obj_path(oid)
        with open(objp, "wb") as f:
            f.write(zlib.compress(b"blob\n99\nHELLO"))
        with self.assertRaises(RepoError):
            self.repo.get(oid)

    def test_is_ancestor(self):
        self.write("a", "1")
        c1 = self.repo.commit("v1")
        self.write("a", "2")
        c2 = self.repo.commit("v2")
        self.assertTrue(self.repo.is_ancestor(c1, c2))
        self.assertFalse(self.repo.is_ancestor(c2, c1))

    def test_prefix_resolution_and_commit_diff(self):
        self.write("a.txt", "one\n")
        c1 = self.repo.commit("v1")
        self.write("a.txt", "two\n")
        c2 = self.repo.commit("v2")
        self.assertEqual(self.repo.resolve(c1[:8]), c1)
        self.repo.set_mark(c1[:8], "red", ["旧版"])
        oldest = self.repo.log()[-1]
        self.assertEqual(oldest["id"], c1)
        self.assertEqual(oldest["mark"], "red")
        res = self.repo.commit_diff_text(c1[:8], c2, "a.txt")
        self.assertFalse(res["binary"])
        self.assertTrue(any(ln.startswith("-one") for ln in res["lines"]))
        self.assertTrue(any(ln.startswith("+two") for ln in res["lines"]))
        with self.assertRaises(RepoError):
            self.repo.resolve("ffff")

    def test_nested_tree_depth(self):
        """多层目录（根 + 一级 + 三级）都要进树，不能丢子树"""
        self.write("root.txt", "r")
        self.write("一级.txt", "1")
        self.write("a/e.txt", "e")
        self.write("a/b/c/d.txt", "deep")
        oid = self.repo.commit("多层")
        _, files = self.repo.files_of(oid)
        self.assertEqual(sorted(files), ["a/b/c/d.txt", "a/e.txt", "root.txt", "一级.txt"])
        self.assertTrue(self.repo.status()["clean"])
        out = os.path.join(self.tmp, "展开")
        self.repo.restore(oid, out, mirror=False, backup=False)
        self.assertTrue(os.path.isfile(os.path.join(out, "a", "b", "c", "d.txt")))
        with open(os.path.join(out, "a", "b", "c", "d.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "deep")

    def test_tree_is_deterministic(self):
        self.write("a/1.txt", "x")
        c1 = self.repo.commit("v")
        r2 = Repo(os.path.join(self.tmp, "副本")).init("同样内容")
        p = os.path.join(self.tmp, "副本", "a")
        os.makedirs(p)
        with open(os.path.join(p, "1.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        c2 = r2.commit("v")
        # 内容相同 → 树与提交对象一致（提交时间不同不影响 tree）
        self.assertEqual(self.repo.commit_info(c1)["tree"], r2.commit_info(c2)["tree"])


class SyncTest(unittest.TestCase):
    """两仓库经真实 HTTP 服务同步"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-sync-")
        self.a = Repo(os.path.join(self.tmp, "机器A")).init("共享项目")
        self.b = Repo(os.path.join(self.tmp, "机器B")).init("共享项目")
        os.makedirs(self.a.root, exist_ok=True)
        os.makedirs(self.b.root, exist_ok=True)

        from zgr.registry import Registry
        from zgr.server import App
        from http.server import ThreadingHTTPServer
        import threading

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        self.app = App(reg, default_project=self.a.root)
        handler = type("H", (_BaseHandler(),), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, repo, rel, text):
        p = os.path.join(repo.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self, repo, rel):
        with open(os.path.join(repo.root, rel), encoding="utf-8") as f:
            return f.read()

    def test_pull_push_roundtrip(self):
        """A 机作服务端 → B 机拉取 → B 改动推送 → A 工作区自动更新"""
        from zgr.sync import sync
        self._write(self.a, "图纸/总图.dwg.txt", "A 的第一版")
        self._write(self.a, "说明.md", "# 说明")
        oid = self.a.commit("A 机初稿")

        r1 = sync(self.b, self.url, push=False, pull=True, checkout=True, log=lambda m: None)
        self.assertEqual(self.b.head_oid(), oid)
        self.assertEqual(self._read(self.b, "图纸/总图.dwg.txt"), "A 的第一版")
        self.assertTrue(r1["checked_out"])

        self._write(self.b, "图纸/总图.dwg.txt", "B 修改的第二版")
        oid2 = self.b.commit("B 修改")
        r2 = sync(self.b, self.url, push=True, pull=False, log=lambda m: None)
        self.assertGreater(r2["pushed"], 0)
        # 服务端（A 机）历史推进且工作区被自动更新
        self.assertEqual(self.a.head_oid(), oid2)
        self.assertEqual(self._read(self.a, "图纸/总图.dwg.txt"), "B 修改的第二版")

        # B 再拉一次应无事可做
        r3 = sync(self.b, self.url, push=True, pull=True, log=lambda m: None)
        self.assertEqual(r3["pushed"], 0)
        self.assertTrue(any("一致" in m for m in r3["messages"]))

    def test_push_when_server_worktree_dirty(self):
        """服务端工作区有未记录改动时，只推进历史并留下待更新标记"""
        from zgr.sync import sync
        self._write(self.a, "a.txt", "1")
        self.a.commit("v1")
        sync(self.b, self.url, push=False, pull=True, checkout=True, log=lambda m: None)

        self._write(self.a, "a.txt", "A 本地还没记录")   # 弄脏 A 的工作区
        self._write(self.b, "a.txt", "B 的版本")
        oid2 = self.b.commit("B 提交")
        sync(self.b, self.url, push=True, pull=False, log=lambda m: None)

        self.assertEqual(self.a.head_oid(), oid2)
        # A 的工作区未被覆盖
        self.assertEqual(self._read(self.a, "a.txt"), "A 本地还没记录")
        self.assertTrue(os.path.isfile(os.path.join(self.a.meta, "PULL_NEEDED")))

    def test_divergence_detected(self):
        from zgr.sync import sync, SyncError
        self._write(self.a, "a.txt", "1")
        self.a.commit("v1")
        sync(self.b, self.url, push=False, pull=True, checkout=True, log=lambda m: None)

        self._write(self.a, "a.txt", "A 侧改动")
        self.a.commit("A 侧")
        self._write(self.b, "b.txt", "B 侧改动")
        self.b.commit("B 侧")

        with self.assertRaises(SyncError):
            sync(self.b, self.url, push=True, pull=False, log=lambda m: None)

    def test_token_required(self):
        from zgr.sync import sync, SyncError
        self.app.token = "s3cret"
        self._write(self.a, "a.txt", "1")
        self.a.commit("v")
        with self.assertRaises(SyncError):
            sync(self.b, self.url, push=False, pull=True, log=lambda m: None)
        r = sync(self.b, self.url, push=False, pull=True, token="s3cret",
                 log=lambda m: None)
        self.assertEqual(self.b.head_oid(), self.a.head_oid())


class MergeTest(unittest.TestCase):
    """分叉后的选择性合并 + 合并后可推送"""

    def setUp(self):
        SyncTest.setUp(self)
        self._write = SyncTest._write.__get__(self)      # 复用同步测试的写文件助手
        self._read = SyncTest._read.__get__(self)
        self._write(self.a, "总图.txt", "v1")
        self._write(self.a, "说明.txt", "v1")
        self.a.commit("v1")
        from zgr.sync import sync
        sync(self.b, self.url, push=False, pull=True, checkout=True, log=lambda m: None)

    def _fetch_remote(self):
        from zgr.sync import make_backend
        be = make_backend(url=self.url)
        head = be.refs()["branches"]["main"]
        need = [o for o in be.manifest("main", self.b.head_oid()) if not self.b.has(o)]
        be.fetch(self.b, need, log=lambda m: None)
        return head

    def test_selective_merge_and_push(self):
        from zgr import merge
        from zgr.sync import sync
        self._write(self.a, "总图.txt", "A 侧改的总图")
        self._write(self.a, "对方新零件.txt", "A 新增")
        self.a.commit("A 改总图并加零件")
        self._write(self.b, "总图.txt", "B 侧改的总图")
        self._write(self.b, "本机新零件.txt", "B 新增")
        self.b.commit("B 改总图并加零件")

        head = self._fetch_remote()
        p = merge.plan(self.b, head)
        conflict = [e["path"] for e in p["entries"] if e["status"] == "conflict"]
        self.assertIn("总图.txt", conflict)
        theirs_only = [e for e in p["entries"] if e["path"] == "对方新零件.txt"][0]
        self.assertEqual(theirs_only["status"], "theirs_only")
        self.assertEqual(theirs_only["pick"], "theirs")   # 本机没有 → 自动取对方
        ours_only = [e for e in p["entries"] if e["path"] == "本机新零件.txt"][0]
        self.assertEqual(ours_only["pick"], "ours")       # 本机新增不能丢

        res = merge.apply(self.b, p, choices={"总图.txt": "theirs"}, checkout=True)
        self.assertTrue(res["commit"])
        self.assertEqual(self._read(self.b, "总图.txt"), "A 侧改的总图")
        self.assertEqual(self._read(self.b, "对方新零件.txt"), "A 新增")
        self.assertEqual(self._read(self.b, "本机新零件.txt"), "B 新增")

        # 合并提交包含对方历史 → 推送必须被接受
        r = sync(self.b, self.url, push=True, pull=False, log=lambda m: None)
        self.assertEqual(self.a.head_oid(), res["commit"])
        self.assertTrue(r["pushed"] >= 0)
        self.assertEqual(self._read(self.a, "总图.txt"), "A 侧改的总图")

    def test_merge_both_keeps_two_copies(self):
        from zgr import merge
        self._write(self.a, "总图.txt", "A 版")
        self.a.commit("A")
        self._write(self.b, "总图.txt", "B 版")
        self.b.commit("B")
        head = self._fetch_remote()
        p = merge.plan(self.b, head)
        res = merge.apply(self.b, p, choices={"总图.txt": "both"}, checkout=False)
        _, files = self.b.files_of(res["commit"])
        self.assertIn("总图.txt", files)
        self.assertIn("总图.本机版本.txt", files)

    def test_merge_never_deletes_by_default(self):
        """对方删了、本机没动：默认保留文件，绝不悄悄消失"""
        from zgr import merge
        os.remove(os.path.join(self.a.root, "说明.txt"))
        self.a.commit("A 删说明")
        self._write(self.b, "别的东西.txt", "B 加的")
        self.b.commit("B 加东西")
        head = self._fetch_remote()
        p = merge.plan(self.b, head)
        e = [x for x in p["entries"] if x["path"] == "说明.txt"][0]
        self.assertEqual(e["status"], "theirs_deleted")
        self.assertEqual(e["pick"], "ours")
        res = merge.apply(self.b, p, checkout=False)
        _, files = self.b.files_of(res["commit"])
        self.assertIn("说明.txt", files)

    def test_undo_keeps_merge_in_history(self):
        """回退不搬指针：生成新提交，合并结果仍在历史里，能一路推给对端"""
        from zgr import merge
        from zgr.sync import sync
        self._write(self.a, "总图.txt", "A 版")
        self.a.commit("A 改")
        self._write(self.b, "总图.txt", "B 版")
        self.b.commit("B 改")
        head = self._fetch_remote()
        p = merge.plan(self.b, head)
        merged = merge.apply(self.b, p, choices={"总图.txt": "theirs"}, checkout=True)
        self.assertEqual(self._read(self.b, "总图.txt"), "A 版")

        undo = merge.undo(self.b, p["ours"], checkout=True)
        self.assertNotEqual(undo["commit"], merged["commit"])
        self.assertEqual(self._read(self.b, "总图.txt"), "B 版")
        # 合并提交仍是新 HEAD 的祖先，历史没被丢掉
        self.assertIn(merged["commit"], merge.ancestors(self.b, undo["commit"]))
        # 回退之后依然可以推给对端（不是强推）
        sync(self.b, self.url, push=True, pull=False, log=lambda m: None)
        self.assertEqual(self.a.head_oid(), undo["commit"])
        self.assertEqual(self._read(self.a, "总图.txt"), "B 版")

    def test_big_object_raw_transfer(self):
        """超过阈值的大对象走原始字节流，不能被 base64/请求体上限卡住"""
        blob = os.urandom(3 << 20)
        with open(os.path.join(self.a.root, "大模型.SLDPRT"), "wb") as f:
            f.write(blob)
        oid = self.a.commit("大文件")
        from zgr.sync import sync
        sync(self.b, self.url, push=False, pull=True, checkout=True, log=lambda m: None)
        with open(os.path.join(self.b.root, "大模型.SLDPRT"), "rb") as f:
            self.assertEqual(f.read(), blob)
        self.assertTrue(oid)


class ShareTest(unittest.TestCase):
    """共享文件夹后端（对方不用跑服务）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-share-")
        self.a = Repo(os.path.join(self.tmp, "A")).init("示例项目项目")
        self.b = Repo(os.path.join(self.tmp, "B")).init("示例项目项目")
        self.share = os.path.join(self.tmp, "共享")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, repo, rel, text):
        p = os.path.join(repo.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_share_push_pull(self):
        from zgr.sync import sync
        self._write(self.a, "零件/底板.sldprt.txt", "A 版底板")
        oid = self.a.commit("A 初稿")
        r = sync(self.a, share=self.share, push=True, pull=False, log=lambda m: None)
        self.assertGreater(r["pushed"], 0)

        sync(self.b, share=self.share, push=False, pull=True, checkout=True, log=lambda m: None)
        self.assertEqual(self.b.head_oid(), oid)
        with open(os.path.join(self.b.root, "零件/底板.sldprt.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "A 版底板")

        self._write(self.b, "零件/立板.sldprt.txt", "B 加的立板")
        oid2 = self.b.commit("B 加立板")
        sync(self.b, share=self.share, push=True, pull=False, log=lambda m: None)
        sync(self.a, share=self.share, push=False, pull=True, checkout=True, log=lambda m: None)
        self.assertEqual(self.a.head_oid(), oid2)
        with open(os.path.join(self.a.root, "零件/立板.sldprt.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "B 加的立板")


class RemoteUrlTest(unittest.TestCase):
    """跨机地址只允许内网：防止服务被当跳板（SSRF）"""

    def check(self, url, resolve=False):
        from zgr.server import check_remote_url
        return check_remote_url(url, resolve=resolve)

    def test_reject_bad_schemes(self):
        from zgr.server import check_remote_url
        from zgr.sync import SyncError
        for url in ("ftp://10.0.0.1/", "file:///C:/Windows", "", "10.0.0.1:8795"):
            with self.assertRaises(SyncError, msg=url):
                check_remote_url(url, resolve=False)

    def test_reject_credentials(self):
        from zgr.server import check_remote_url
        from zgr.sync import SyncError
        with self.assertRaises(SyncError):
            check_remote_url("http://user:pw@10.0.0.1:8795", resolve=False)

    def test_reject_public_ip(self):
        from zgr.server import check_remote_url
        from zgr.sync import SyncError
        for url in ("http://8.8.8.8:8795", "http://1.1.1.1/"):
            with self.assertRaises(SyncError, msg=url):
                check_remote_url(url, resolve=False)

    def test_allow_lan(self):
        from zgr.server import check_remote_url
        for url in ("http://127.0.0.1:8795", "http://192.168.1.20:8795",
                    "http://192.168.1.10:8795", "http://172.16.5.4:8795",
                    "http://169.254.1.1/", "http://localhost:8795",
                    "http://desktop-mv0kmvl:8795", "https://nas.local:8795"):
            self.assertEqual(check_remote_url(url, resolve=False), url)


class FileHistoryTest(unittest.TestCase):
    """单个零件/文件的改动历史"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-hist-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("历史项目")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def w(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_entries(self):
        from zgr.server import Handler
        self.w("总图.sldprt", "v1")
        self.w("说明.txt", "b1")
        self.repo.commit("第一版")
        self.w("总图.sldprt", "v2 更长一些")
        self.repo.commit("第二版：改总图")
        os.remove(os.path.join(self.root, "说明.txt"))
        self.repo.commit("第三版：删说明")

        a = Handler.file_history(self.repo, "总图.sldprt")
        self.assertEqual([e["st"] for e in a["entries"]], ["M", "A"])
        self.assertGreater(a["entries"][0]["size"], a["entries"][0]["old_size"])
        self.assertEqual(a["entries"][0]["old_oid"], a["entries"][1]["oid"])
        self.assertFalse(a["work"]["changed"])
        self.assertTrue(a["work"]["exists"])

        b = Handler.file_history(self.repo, "说明.txt")
        self.assertEqual([e["st"] for e in b["entries"]], ["D", "A"])
        self.assertFalse(b["work"]["exists"])

        # 工作区改了但还没记录 → work.changed 为真
        self.w("总图.sldprt", "v3 未记录")
        c = Handler.file_history(self.repo, "总图.sldprt")
        self.assertTrue(c["work"]["changed"])

    def test_truncated_flag(self):
        """版本数超过上限时要明确告诉界面，不能假装只有这么多"""
        from zgr.server import Handler
        for i in range(4):
            self.w("总图.sldprt", "v%d" % i)
            self.repo.commit("第 %d 版" % i)
        full = Handler.file_history(self.repo, "总图.sldprt", limit=20)
        self.assertFalse(full["truncated"])
        self.assertEqual(len(full["entries"]), 4)
        part = Handler.file_history(self.repo, "总图.sldprt", limit=2)
        self.assertTrue(part["truncated"])
        self.assertEqual(len(part["entries"]), 2)
        self.assertEqual(part["limit"], 2)

    def test_restore_one_file(self):
        """只恢复一个文件：内容回到旧版，其它文件不动，旧内容先备份"""
        from zgr.server import Handler
        self.w("总图.sldprt", "v1")
        self.w("说明.txt", "keep")
        self.repo.commit("第一版")
        old_oid = Handler.file_history(self.repo, "总图.sldprt")["entries"][0]["oid"]
        self.w("总图.sldprt", "v2")
        self.repo.commit("第二版")

        res = Handler.restore_file(self.repo, "总图.sldprt", old_oid)
        self.assertTrue(res["backup"].startswith(".zgr/backup/"))
        with open(os.path.join(self.root, "总图.sldprt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "v1")
        with open(os.path.join(self.root, "说明.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep")
        bpath = os.path.join(self.root, res["backup"].replace("/", os.sep))
        with open(bpath, encoding="utf-8") as f:
            self.assertEqual(f.read(), "v2")

    def test_restore_file_rejects_bad_oid(self):
        from zgr.server import Handler
        self.w("总图.sldprt", "v1")
        self.repo.commit("第一版")
        with self.assertRaises(RepoError):
            Handler.restore_file(self.repo, "总图.sldprt", "../../etc/passwd")
        with self.assertRaises(RepoError):
            Handler.restore_file(self.repo, "../外面.txt", "0" * 64)


class ChangeCountTest(unittest.TestCase):
    """时间线每个版本要显示改了多少个文件"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-chg-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("改动统计")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def w(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def test_counts(self):
        from zgr.server import Handler
        self.w("a.sldprt", "a1")
        self.w("b.sldprt", "b1")
        self.repo.commit("第一版")
        self.w("a.sldprt", "a2")
        self.w("c.sldasm", "c1")
        os.remove(os.path.join(self.root, "b.sldprt"))
        self.repo.commit("第二版")

        commits = self.repo.log(10)
        # 改动数现在是带缓存的方法：给一个只有 app 的最小外壳
        Handler._attach_change_counts(types.SimpleNamespace(app=None), self.repo, commits)
        by_msg = {c["message"]: c for c in commits}

        root = by_msg["第一版"]["changes"]
        self.assertEqual(root, {"added": 2, "modified": 0, "deleted": 0, "total": 2})
        second = by_msg["第二版"]["changes"]
        self.assertEqual(second, {"added": 1, "modified": 1, "deleted": 1, "total": 3})


class MeshTest(unittest.TestCase):
    """3D 预览：3MF 解析 → 紧凑网格"""

    def _mk3mf(self, xml, name="m.3mf"):
        import zipfile
        p = os.path.join(self.tmp, name)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("3D/3dmodel.model", xml)
        return p

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-mesh-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_component_transform(self):
        from zgr import mesh
        xml = """<?xml version="1.0"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model"><mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
  </mesh></object>
  <object id="2" type="model"><components>
    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/>
  </components></object>
 </resources>
 <build><item objectid="2"/></build>
</model>"""
        v, t, st = mesh.parse_3mf(self._mk3mf(xml))
        self.assertEqual(st["tris"], 1)
        self.assertEqual(st["objects"], 1)
        self.assertAlmostEqual(v[0], 10.0)
        self.assertAlmostEqual(v[3], 11.0)
        self.assertAlmostEqual(v[7], 1.0)

    def test_unit_inch_scaled_to_mm(self):
        from zgr import mesh
        xml = """<?xml version="1.0"?>
<model unit="inch" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources><object id="1" type="model"><mesh>
   <vertices><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices>
   <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
 </mesh></object></resources>
 <build><item objectid="1"/></build>
</model>"""
        v, _t, _st = mesh.parse_3mf(self._mk3mf(xml))
        self.assertAlmostEqual(v[0], 25.4, places=3)
        self.assertAlmostEqual(v[4], 25.4, places=3)
        self.assertAlmostEqual(v[8], 25.4, places=3)

    def test_component_expansion_capped(self):
        """装配体实例展开后的面数也要卡在 MAX_TRIS 以内

        实测踩过：24MB 的装配体源网格没超限，展开出 5470 万面、吃掉 1GB 内存。
        """
        from zgr import mesh
        xml = """<?xml version="1.0"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model"><mesh>
    <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
      <vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/></vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="2" v3="3"/>
      <triangle v1="0" v2="3" v3="1"/><triangle v1="1" v2="3" v3="2"/></triangles>
  </mesh></object>
  <object id="2" type="model"><components>
    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 10 0 0"/>
    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 20 0 0"/>
    <component objectid="1" transform="1 0 0 0 1 0 0 0 1 30 0 0"/>
  </components></object>
 </resources>
 <build><item objectid="2"/></build>
</model>"""
        old = mesh.MAX_TRIS
        mesh.MAX_TRIS = 10          # 4 个实例 × 4 面 = 16 面 > 10
        try:
            with self.assertRaises(mesh.MeshError) as cm:
                mesh.parse_3mf(self._mk3mf(xml, "big.3mf"))
            self.assertIn("展开后", str(cm.exception))
            mesh.MAX_TRIS = 16     # 正好等于上限：不该误杀
            _v, t, st = mesh.parse_3mf(self._mk3mf(xml, "ok.3mf"))
            self.assertEqual(st["tris"], 16)
        finally:
            mesh.MAX_TRIS = old

    def test_bad_3mf(self):
        from zgr import mesh
        p = os.path.join(self.tmp, "empty.3mf")
        with open(p, "wb") as f:
            f.write(b"not a zip")
        with self.assertRaises(mesh.MeshError):
            mesh.parse_3mf(p)

    def test_zgm_roundtrip(self):
        from zgr import mesh
        import array
        p = os.path.join(self.tmp, "x.zgm")
        v = array.array("f", [0, 0, 0, 1, 2, 3])
        t = array.array("I", [0, 1, 1])
        mesh._write_zgm(p, v, t)
        with open(p, "rb") as f:
            raw = f.read()
        self.assertEqual(raw[:4], b"ZGM1")
        nv, nt = struct.unpack("<II", raw[4:12])
        self.assertEqual((nv, nt), (2, 1))
        self.assertEqual(len(raw), 12 + nv * 12 + nt * 12)

    def test_export_hint(self):
        from zgr import mesh
        self.assertIn("缺了它引用的零件", mesh._export_hint("ERR open 0"))
        self.assertIn("没能启动", mesh._export_hint("ERR create 429"))
        self.assertIn("只读", mesh._export_hint("ERR save 5"))
        self.assertIn("导出失败", mesh._export_hint(""))
        # 真实错误信息要原样带出来，别吞掉
        self.assertIn("文件已损坏", mesh._export_hint("ERR open 3 文件已损坏"))

    def test_rejects_non_model(self):
        from zgr import mesh
        root = os.path.join(self.tmp, "proj")
        os.makedirs(root)
        repo = Repo(root).init("网格测试")
        with self.assertRaises(mesh.MeshError):
            mesh.ensure_mesh(repo, "说明.txt", "a" * 64)
        with self.assertRaises(mesh.MeshError):
            mesh.ensure_mesh(repo, "a.sldprt", "not-an-oid")
        with self.assertRaises(mesh.MeshError):
            mesh.ensure_mesh(repo, "../x.sldprt", "a" * 64)

    def test_missing_refs_warns_but_does_not_block(self):
        """装配体缺引用件：预检要报出来（两种字符串编码都要认），但只警告"""
        from zgr import mesh
        root = tempfile.mkdtemp(dir=self.tmp)
        asm = os.path.join(root, "总装.SLDASM")
        # 真实存档里引用是路径字符串（ASCII 或 UTF-16LE），不是裸文件名
        payload = (b"head\x00" + r"C:\proj\PartA.SLDPRT".encode("utf-16-le")
                   + b"\x00\x00" + rb"mid\x00C:\proj\PartB.SLDPRT\x00" + b"tail")
        with open(asm, "wb") as f:
            f.write(payload)
        with open(os.path.join(root, "PartA.SLDPRT"), "wb") as f:
            f.write(b"x")
        miss = mesh.missing_refs(asm, root)
        self.assertIn("partb.sldprt", miss)
        self.assertNotIn("parta.sldprt", miss)
        self.assertNotIn("总装.sldasm", miss)

    def test_valid_zgm_rejects_truncated(self):
        from zgr import mesh
        self.assertIsNone(mesh._valid_zgm(b""))
        self.assertIsNone(mesh._valid_zgm(b"ZGM1" + b"\x01\x00\x00\x00\x01\x00\x00\x00"))
        self.assertEqual(mesh._valid_zgm(struct.pack("<4sII", b"ZGM1", 1, 0)
                                         + b"\x00" * 12), (1, 0))

    def test_compose(self):
        from zgr import mesh
        # 先平移 10，再平移 5 → 15
        a = [1, 0, 0, 0, 1, 0, 0, 0, 1, 10, 0, 0]
        b = [1, 0, 0, 0, 1, 0, 0, 0, 1, 5, 0, 0]
        c = mesh._compose(a, b)
        self.assertEqual(c[9], 15)
        # 先旋转 90°(绕 Z) 再平移 (10,0)：原点 → (10,0)
        r = [0, 1, 0, -1, 0, 0, 0, 0, 1, 0, 0, 0]
        t = [1, 0, 0, 0, 1, 0, 0, 0, 1, 10, 0, 0]
        d = mesh._compose(r, t)
        self.assertAlmostEqual(d[9], 10)
        self.assertAlmostEqual(d[10], 0)


class MeshPeerTest(unittest.TestCase):
    """没装 SolidWorks 的机器按 oid 向对端取网格"""

    def setUp(self):
        import array
        import threading
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App
        from zgr import mesh

        self.mesh = mesh
        self.array = array
        self.tmp = tempfile.mkdtemp(prefix="zgr-peer-")
        self.a = Repo(os.path.join(self.tmp, "167")).init("共享项目")
        self.b = Repo(os.path.join(self.tmp, "174")).init("共享项目")
        for r, text in ((self.a, "假SolidWorks内容"), (self.b, "假SolidWorks内容")):
            p = os.path.join(r.root, "模型.sldprt")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            r.commit("初稿")
            _c, files = r.files_of(r.head_oid())
            self.oid = files["模型.sldprt"]["oid"]

        # 167 上预置一份网格（真实环境是 SolidWorks 跑出来的）
        os.makedirs(mesh.mesh_dir(self.a), exist_ok=True)
        mesh._write_zgm(mesh.cache_file(self.a, self.oid),
                        array.array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0]),
                        array.array("I", [0, 1, 2]))

        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.a.root)
        handler = type("H", (_BaseHandler(),), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self._old_sw = mesh.solidworks_available
        mesh.solidworks_available = lambda: False

    def tearDown(self):
        self.mesh.solidworks_available = self._old_sw
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch_mesh_from_peer_and_cache(self):
        meta = self.mesh.ensure_mesh(self.b, "模型.sldprt", self.oid, peer=self.url)
        self.assertEqual(meta["tris"], 1)
        self.assertTrue(meta["source"].startswith("peer:"))
        self.assertTrue(self.mesh.is_cached(self.b, self.oid))
        # 第二次不再访问网络：把对端地址换成必然连不上的端口，仍应命中本地缓存
        meta2 = self.mesh.ensure_mesh(self.b, "模型.sldprt", self.oid,
                                      peer="http://127.0.0.1:1")
        self.assertEqual(meta2["tris"], 1)

    def test_peer_rejects_bad_oid(self):
        with self.assertRaises(self.mesh.MeshError):
            self.mesh.fetch_peer(self.b, self.url, "", "not-an-oid")

    def test_resolve_rel_from_history(self):
        """文件后来被删了，对端仍能从历史（或指定提交）里找到它对应的路径"""
        old_commit = self.a.head_oid()
        os.remove(os.path.join(self.a.root, "模型.sldprt"))
        self.a.commit("删掉模型")
        self.assertEqual(self.mesh.resolve_rel(self.a, self.oid), "模型.sldprt")
        self.assertEqual(self.mesh.resolve_rel(self.a, self.oid, old_commit), "模型.sldprt")


class LogGraphTest(unittest.TestCase):
    """分支树的数据源：log() 只沿第一父提交走，log_graph() 要能看到分叉。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-graph-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("测试项目")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def build_fork(self):
        self.write("a.txt", "1")
        c1 = self.repo.commit("A")
        self.write("a.txt", "2")
        c2 = self.repo.commit("B")
        self.repo.set_branch("side", c1)          # 从 A 分出去
        self.write("b.txt", "x")
        c3 = self.repo.commit("C", branch="side")
        return c1, c2, c3

    def test_log_hides_side_branch(self):
        c1, c2, c3 = self.build_fork()
        flat = [c["id"] for c in self.repo.log()]
        self.assertEqual(flat, [c2, c1], "老 log 只沿第一父提交，看不见分叉")

    def test_log_graph_sees_both_branches(self):
        c1, c2, c3 = self.build_fork()
        commits, heads = self.repo.log_graph()
        ids = [c["id"] for c in commits]
        self.assertEqual(set(ids), {c1, c2, c3}, "两条分支的提交都要在")
        self.assertEqual(heads.get("side"), c3)
        self.assertEqual(heads.get("main"), c2)
        by_id = {c["id"]: c for c in commits}
        self.assertEqual(by_id[c3]["parents"], [c1], "分叉点要能对上")
        self.assertEqual(by_id[c2]["parents"], [c1])

    def test_log_graph_newest_first(self):
        _, c2, c3 = self.build_fork()
        commits, _ = self.repo.log_graph()
        times = [c.get("time") or 0 for c in commits]
        self.assertEqual(times, sorted(times, reverse=True), "按时间倒序，时间线才对")

    def test_log_graph_limit(self):
        self.write("a.txt", "0")
        for i in range(6):
            self.write("a.txt", str(i))
            self.repo.commit("第%d次" % i)
        commits, _ = self.repo.log_graph(limit=3)
        self.assertEqual(len(commits), 3)


class BranchEndpointTest(unittest.TestCase):
    """/api/branch：新建分支、切换分支都不能把已有分支的指针挪走。

    回归点：1.7.3 及以前切分支是无条件 set_branch(name, 当前HEAD)，
    切到哪条就把哪条挪到当前位置，那条分支原来的提交就从分支上掉下来了。
    """

    def setUp(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from zgr.registry import Registry
        from zgr.server import App

        self.tmp = tempfile.mkdtemp(prefix="zgr-branch-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("分支测试")
        reg = Registry(path=os.path.join(self.tmp, "projects.json"))
        app = App(reg, default_project=self.root)
        handler = type("H", (_BaseHandler(),), {"app": app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.port
        self._urllib = urllib.request

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, route, body):
        req = self._urllib.Request(
            self.url + route, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self._urllib.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def commit(self, text, msg):
        with open(os.path.join(self.root, "a.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        return self.repo.commit(msg)

    def test_create_branch_from_old_commit(self):
        c1 = self.commit("1", "v1")
        c2 = self.commit("2", "v2")
        d = self.post("/api/branch", {"path": self.root, "branch": "side", "commit": c1})
        self.assertTrue(d["ok"])
        self.assertTrue(d["created"])
        self.assertEqual(self.repo.branch, "side")
        self.assertEqual(self.repo.head_oid("side"), c1)
        self.assertEqual(self.repo.head_oid("main"), c2, "主干不能被动到")

    def test_switch_back_keeps_refs(self):
        c1 = self.commit("1", "v1")
        c2 = self.commit("2", "v2")
        self.post("/api/branch", {"path": self.root, "branch": "side", "commit": c1})
        # 切回已有分支：指针一个都不许动；内容不同的两条分支要先问一句再落盘
        d = self.post("/api/branch", {"path": self.root, "branch": "main"})
        self.assertTrue(d["ok"])
        self.assertTrue(d.get("dry"), "两条分支内容不同，先回来问一句")
        self.assertEqual(self.repo.branch, "side", "没确认之前 HEAD 不能动")
        d = self.post("/api/branch", {"path": self.root, "branch": "main", "force": True})
        self.assertTrue(d["ok"])
        self.assertFalse(d["created"])
        self.assertEqual(self.repo.branch, "main")
        self.assertEqual(self.repo.head_oid("main"), c2)
        d = self.post("/api/branch", {"path": self.root, "branch": "side", "force": True})
        self.assertEqual(self.repo.head_oid("side"), c1, "再切回 side 也不能把它挪到 c2")
        self.assertEqual(self.repo.head_oid("main"), c2)

    def test_graph_endpoint_reports_heads(self):
        c1 = self.commit("1", "v1")
        self.commit("2", "v2")
        self.post("/api/branch", {"path": self.root, "branch": "side", "commit": c1})
        import urllib.parse
        q = urllib.parse.urlencode({"path": self.root, "graph": 1, "limit": 20})
        with self._urllib.urlopen(self.url + "/api/log?" + q, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        self.assertTrue(d["ok"])
        self.assertEqual(set(d["heads"]), {"main", "side"})
        self.assertEqual(len(d["commits"]), 2)


def _BaseHandler():
    from zgr.server import Handler
    return Handler


if __name__ == "__main__":
    unittest.main(verbosity=2)
