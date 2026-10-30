"""实时同步 / 智能合并 / 打包 / 回收站 自测：python -m unittest tests.test_features"""
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import live as L  # noqa: E402
from zgr import merge as M  # noqa: E402
from zgr.store import Repo, RepoError  # noqa: E402


class Merge3Test(unittest.TestCase):
    def test_disjoint_changes_both_kept(self):
        base = ["a\n", "b\n", "c\n", "d\n"]
        ours = ["A\n", "b\n", "c\n", "d\n"]
        theirs = ["a\n", "b\n", "c\n", "D\n"]
        out, conflicts = M.merge3_lines(base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual("".join(out), "A\nb\nc\nD\n")

    def test_same_change_not_conflict(self):
        base = ["a\n", "b\n"]
        side = ["a\n", "B\n"]
        out, conflicts = M.merge3_lines(base, side, side)
        self.assertEqual(conflicts, 0)
        self.assertEqual("".join(out), "a\nB\n")

    def test_same_region_different_is_conflict_keep_ours(self):
        base = ["a\n", "b\n", "c\n"]
        ours = ["a\n", "OURS\n", "c\n"]
        theirs = ["a\n", "THEIRS\n", "c\n"]
        out, conflicts = M.merge3_lines(base, ours, theirs)
        self.assertEqual(conflicts, 1)
        self.assertIn("OURS\n", out)
        self.assertNotIn("THEIRS\n", out)

    def test_both_insert_same_place_conflict(self):
        base = ["a\n", "z\n"]
        ours = ["a\n", "o\n", "z\n"]
        theirs = ["a\n", "t\n", "z\n"]
        out, conflicts = M.merge3_lines(base, ours, theirs)
        self.assertEqual(conflicts, 1)

    def test_insertion_at_different_places_merges(self):
        base = ["a\n", "b\n", "c\n"]
        ours = ["a\n", "new-o\n", "b\n", "c\n"]
        theirs = ["a\n", "b\n", "c\n", "new-t\n"]
        out, conflicts = M.merge3_lines(base, ours, theirs)
        self.assertEqual(conflicts, 0)
        self.assertEqual("".join(out), "a\nnew-o\nb\nc\nnew-t\n")


class RepoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-feat-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("功能测试")
        cfg = self.repo.config
        cfg["author"] = "小王"
        self.repo._write_json("config.json", cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def read(self, rel):
        with open(os.path.join(self.root, rel.replace("/", os.sep)),
                  "r", encoding="utf-8") as f:
            return f.read()


class OpenFileTest(RepoBase):
    """「打开」只允许工作区内真实存在的文件，路径不能越界"""

    def setUp(self):
        super().setUp()
        from zgr.server import Handler
        self.resolve = Handler.project_file

    def test_resolves_inside_project(self):
        self.write("子目录/零件.SLDPRT", "x")
        got = self.resolve(self.repo, "子目录/零件.SLDPRT")
        self.assertTrue(os.path.isfile(got))
        self.assertEqual(os.path.realpath(got),
                         os.path.realpath(os.path.join(self.root, "子目录", "零件.SLDPRT")))

    def test_accepts_backslashes(self):
        self.write("子目录/零件.txt", "x")
        self.assertTrue(os.path.isfile(self.resolve(self.repo, "子目录\\零件.txt")))

    def test_rejects_traversal(self):
        self.write("a.txt", "x")
        for rel in ("../逃逸.txt", "a/../../b.txt", "..", "."):
            with self.assertRaises(RepoError):
                self.resolve(self.repo, rel)

    def test_rejects_drive_letter(self):
        with self.assertRaises(RepoError):
            self.resolve(self.repo, "C:/Windows/win.ini")

    def test_rejects_missing_or_dir(self):
        os.makedirs(os.path.join(self.root, "空的"))
        for rel in ("", "不存在.txt", "空的"):
            with self.assertRaises(RepoError):
                self.resolve(self.repo, rel)

    def test_only_documents_can_be_launched(self):
        # 白名单：漏了只是打不开，黑名单漏了就是「同步进来一个 .scf 用户点一下就执行」
        from zgr.server import Handler
        self.write("看图.exe", "MZ")
        self.write("图纸/装配.DWG", "x")
        for bad in (".exe", ".scf", ".py", ".pyw", ".msc", ".chm", ".bat",
                    ".lnk", ".hta", ".js", ".settingcontent-ms"):
            self.assertNotIn(bad, Handler.OPENABLE_EXT, bad)
        for good in (".dwg", ".sldprt", ".sldasm", ".step", ".pdf", ".txt", ".png"):
            self.assertIn(good, Handler.OPENABLE_EXT, good)


class SmartMergeTest(RepoBase):
    def _diverge(self):
        self.write("说明.txt", "行1\n行2\n行3\n行4\n行5\n")
        base = self.repo.commit("base")
        self.write("说明.txt", "改过的行1\n行2\n行3\n行4\n行5\n")
        ours = self.repo.commit("本机改了第一行")
        self.repo.set_branch("对方", base)
        self.write("说明.txt", "行1\n行2\n行3\n行4\n改过的行5\n")
        theirs = self.repo.commit("对方改了第五行", branch="对方")
        return base, ours, theirs

    def test_plan_marks_auto_merge(self):
        _, ours, theirs = self._diverge()
        p = M.plan(self.repo, theirs, ours=ours, smart=True)
        entry = next(e for e in p["entries"] if e["path"] == "说明.txt")
        self.assertEqual(entry["status"], "conflict")
        self.assertIn("merged", entry)
        self.assertIn("说明.txt", p["auto"])

    def test_apply_smart_keeps_both_edits(self):
        _, ours, theirs = self._diverge()
        p = M.plan(self.repo, theirs, ours=ours, smart=True)
        res = M.apply(self.repo, p, strategy="smart", checkout=True)
        self.assertEqual(self.repo.head_oid(), res["commit"])
        text = self.read("说明.txt")
        self.assertIn("改过的行1", text)
        self.assertIn("改过的行5", text)
        info = self.repo.commit_info(res["commit"])
        self.assertEqual(len(info["parents"]), 2)

    def test_real_conflict_keeps_ours_and_file_survives(self):
        self.write("说明.txt", "a\nb\nc\n")
        base = self.repo.commit("base")
        self.write("说明.txt", "a\nOURS\nc\n")
        ours = self.repo.commit("ours")
        self.repo.set_branch("对方", base)
        self.write("说明.txt", "a\nTHEIRS\nc\n")
        theirs = self.repo.commit("theirs", branch="对方")
        p = M.plan(self.repo, theirs, ours=ours, smart=True)
        entry = next(e for e in p["entries"] if e["path"] == "说明.txt")
        self.assertNotIn("merged", entry)
        self.assertIn("说明.txt", p["conflicts"])
        M.apply(self.repo, p, strategy="smart", checkout=True)
        self.assertIn("OURS", self.read("说明.txt"))


class PackTest(RepoBase):
    def test_pack_commit(self):
        self.write("图纸/a.txt", "hello")
        self.write("b.txt", "world")
        oid = self.repo.commit("v1")
        res = L.pack(self.repo, commit=oid)
        self.assertTrue(os.path.isfile(res["file"]))
        with zipfile.ZipFile(res["file"]) as z:
            names = z.namelist()
        self.assertTrue(any(n.endswith("/图纸/a.txt") for n in names), names)
        self.assertTrue(any(n.endswith("/b.txt") for n in names))
        self.assertTrue(any(n.endswith("追光几何-打包说明.txt") for n in names))
        self.assertEqual(res["files"], 2)
        self.assertTrue(L.pack_file(self.repo, res["name"]))

    def test_pack_worktree_skips_meta_and_ignored(self):
        self.write("a.txt", "hello")
        self.write("临时.tmp", "noise")
        cfg = self.repo.config
        cfg["ignore"] = list(cfg.get("ignore") or []) + ["*.log"]
        self.repo._write_json("config.json", cfg)
        L.pack(self.repo, worktree=True)
        packs = os.listdir(os.path.join(self.repo.meta, "packs"))
        with zipfile.ZipFile(os.path.join(self.repo.meta, "packs", packs[0])) as z:
            names = z.namelist()
        self.assertTrue(any(n.endswith("/a.txt") for n in names))
        self.assertFalse(any(".zgr" in n for n in names))
        self.assertFalse(any(n.endswith("临时.tmp") for n in names), names)

    def test_pack_rejects_path_traversal(self):
        with self.assertRaises(Exception):
            L.pack_file(self.repo, "../outside.zip")


class EventsTest(RepoBase):
    def test_events_incremental(self):
        e1 = L.append_event(self.repo, "commit", head="a" * 64, message="第一版")
        e2 = L.append_event(self.repo, "peer_pull", head="b" * 64, message="对方保存")
        self.assertEqual(e1["seq"] + 1, e2["seq"])
        self.assertEqual(e1["author"], "小王")
        d = L.read_events(self.repo, 0)
        self.assertEqual(len(d["events"]), 2)
        d2 = L.read_events(self.repo, e1["seq"])
        self.assertEqual([e["message"] for e in d2["events"]], ["对方保存"])
        self.assertEqual(d2["seq"], e2["seq"])

    def test_change_summary(self):
        self.write("a.txt", "1")
        c1 = self.repo.commit("v1")
        self.write("a.txt", "2")
        self.write("b.txt", "new")
        c2 = self.repo.commit("v2")
        s = L.change_summary(self.repo, c1, c2)
        self.assertEqual(s["counts"], {"added": 1, "modified": 1, "deleted": 0})
        self.assertEqual(s["total"], 2)


class ConfigWriteTest(RepoBase):
    def test_concurrent_writes_do_not_collide(self):
        """实时同步线程和网页请求会同时写 config.json：临时文件名必须互不覆盖。"""
        import json
        import threading
        errs = []

        def w(i):
            try:
                cfg = self.repo.config
                cfg["probe"] = i
                self.repo._write_json("config.json", cfg)
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(24)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        with open(os.path.join(self.repo.meta, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("probe", cfg)
        leftovers = [n for n in os.listdir(self.repo.meta) if ".tmp." in n]
        self.assertEqual(leftovers, [])


class BackupTest(RepoBase):
    def test_restore_backs_up_and_can_recover(self):
        self.write("a.txt", "原始内容")
        first = self.repo.commit("v1")
        self.write("a.txt", "后来改的内容")
        second = self.repo.commit("v2")
        self.repo.restore(first, backup=True)
        self.assertEqual(self.read("a.txt"), "原始内容")
        rows = L.list_backups(self.repo)
        self.assertTrue(rows, "restore 应该留下备份")
        # 再回到 v2 的内容：备份里应该有“后来改的内容”
        names = [r["name"] for r in rows]
        found = None
        for name in names:
            src = os.path.join(self.repo.meta, "backup", name, "a.txt")
            if os.path.isfile(src):
                with open(src, "r", encoding="utf-8") as f:
                    if f.read() == "后来改的内容":
                        found = name
                        break
        self.assertIsNotNone(found, "被覆盖的版本必须能从备份里找回")
        res = L.restore_backup(self.repo, found)
        self.assertEqual(res["count"], 1)
        self.assertEqual(self.read("a.txt"), "后来改的内容")
        self.assertTrue(os.path.isdir(res["safety_backup"]), "复原前要再备份当前内容")

    def test_restore_backup_rejects_bad_name(self):
        with self.assertRaises(Exception):
            L.restore_backup(self.repo, "../..")


class StatusCacheTest(unittest.TestCase):
    """工作区状态缓存的单飞：并发过期时只能有一个线程去全盘重扫。

    回归点：1.7.1 及以前的实现没有单飞，缓存一过期 N 个并发请求同时重扫，
    实测 8 并发 /api/status p50 5.4 秒、吞吐 1.5 req/s。
    """

    def make_app(self):
        from zgr.server import App
        app = App.__new__(App)          # 只测缓存逻辑，不碰注册表/数据目录
        app._status_cache = {}
        app._status_flight = {}
        return app

    class SlowRepo:
        root = os.path.join(tempfile.gettempdir(), "zgr-status-flight-test") + os.sep + "proj"

        def __init__(self, delay=0.25):
            self.delay = delay
            self.calls = 0
            self.lock = __import__("threading").Lock()

        def status(self):
            with self.lock:
                self.calls += 1
            __import__("time").sleep(self.delay)
            return {"head": "h1", "clean": True}

    def burst(self, app, repo, n=8):
        import threading
        errs = []
        ths = [threading.Thread(target=lambda: self._call(app, repo, errs))
               for _ in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)
        return errs

    @staticmethod
    def _call(app, repo, errs):
        try:
            app.status_cached(repo)
        except Exception as e:          # noqa: BLE001
            errs.append(e)

    def test_cold_burst_scans_once(self):
        app, repo = self.make_app(), self.SlowRepo()
        errs = self.burst(app, repo, 8)
        self.assertEqual(repo.calls, 1, "冷启动并发 8 个请求只该扫一次")
        self.assertEqual(errs, [])

    def test_expired_burst_serves_stale(self):
        import time
        app, repo = self.make_app(), self.SlowRepo()
        app.status_cached(repo)                       # 先有旧值
        self.assertEqual(repo.calls, 1)
        app._status_cache[os.path.normcase(repo.root)] = (0.0, "h1", {"head": "h1"})
        self.burst(app, repo, 8)                      # 缓存过期后 8 个并发
        # 一个线程去刷新，其余 7 个立刻吃旧值，绝不再扫 8 次
        self.assertLessEqual(repo.calls, 2, "过期后并发只能有一个线程去重扫")
        time.sleep(0.05)

    def test_commit_invalidates_status_cache(self):
        """刚记完一版，界面不能再拿到「未保存 N 个文件」的旧状态。

        以前的现场：点完记录，横幅还挂着一秒多（缓存 1.2 秒 + 轮询 3 秒），
        用户以为没记上，又点一次，得到「没有变化」——更慌。
        """
        from zgr.server import App
        base = os.path.join(tempfile.gettempdir(), "zgr-status-invalidate", "项目")
        os.makedirs(base, exist_ok=True)
        from zgr.store import Repo
        repo = Repo(base)
        if not repo.exists:
            repo.init("清缓存")
        with open(os.path.join(base, "件.txt"), "w", encoding="utf-8") as f:
            f.write("一")
        repo.commit("一")
        app = self.make_app()
        app.status_cached(repo, ttl=60)               # 先在缓存里放一份
        self.assertIn(os.path.normcase(repo.root), app._status_cache)
        app.invalidate_status(repo)
        self.assertNotIn(os.path.normcase(repo.root), app._status_cache)


if __name__ == "__main__":
    unittest.main()
