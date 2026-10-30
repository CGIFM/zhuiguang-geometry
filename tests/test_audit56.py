# -*- coding: utf-8 -*-
"""第 56 轮：另一双眼睛查出来的十个真缺陷，逐个钉住

这轮的缺陷有个共同点——**每一步都"看起来成功"**：
记录成功、恢复成功、合并成功、同步成功，可版本没了、工作区混了、对方那版消失了。
所以这里的断言一律不看返回值，只看最后磁盘上到底是什么。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import merge as merge_mod            # noqa: E402
from zgr import remotes                      # noqa: E402
from zgr.store import Repo, RepoError        # noqa: E402
from zgr.sync import SyncError, sync         # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = '''# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, sys.argv[1])
from zgr.store import Repo

root, mode = sys.argv[2], sys.argv[3]
r = Repo(root)
t0 = time.time()
if mode == "commit":
    with open(os.path.join(root, "子进程.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    r.commit("子进程记录")
else:
    r.restore(sys.argv[4])
print("%.2f" % (time.time() - t0), flush=True)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-a56-")
        self.root = os.path.join(self.tmp, "项目")
        os.makedirs(self.root)
        self.repo = Repo(self.root).init("审计")
        self.write("底板.sldprt", "版本一")
        self.write("说明.txt", "第一版说明")
        self.c1 = self.repo.commit("v1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p) or self.root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p


# ------------------------------------------------------------------ 1/2/11 锁
class LockCoverageTest(Base):
    """记录 / 恢复 / 删分支都必须拿项目锁——这不是效率问题，是「版本会不会丢」"""

    def setUp(self):
        super().setUp()
        self.child = os.path.join(self.tmp, "child.py")
        with open(self.child, "w", encoding="utf-8") as f:
            f.write(CHILD)

    def run_child(self, mode, *extra):
        return subprocess.Popen([sys.executable, self.child, HERE, self.root, mode]
                                + list(extra), cwd=HERE,
                                env=dict(os.environ, PYTHONPATH=HERE),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def waited(self, proc):
        out, err = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace")[-500:])
        return float(out.decode("utf-8").split()[0])

    def test_commit_takes_the_project_lock(self):
        """另一个进程记一版，必须等在这把锁后面（以前被 unstable 预检顶掉了，全程无锁）"""
        with self.repo.lock("测试占锁"):
            p = self.run_child("commit")
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "子进程没被锁挡住：记录又变回无锁了")
        self.assertGreater(self.waited(p), 1.0, "子进程几乎没等，锁没生效")

    def test_restore_takes_the_project_lock(self):
        """恢复是唯一没上锁的写盘动作：两个恢复交错会把工作区搞成混合体"""
        with self.repo.lock("测试占锁"):
            p = self.run_child("restore", self.c1)
            time.sleep(1.5)
            self.assertIsNone(p.poll(), "子进程没被锁挡住：恢复又变回无锁了")
        self.assertGreater(self.waited(p), 1.0, "子进程几乎没等，锁没生效")


# ------------------------------------------------------------------ 4 读不了≠删除
class UnreadableNotDeletedTest(Base):
    def test_status_reports_busy_file_as_unchanged_not_deleted(self):
        self.write("底板.sldprt", "改过了但正被 SolidWorks 占着")
        orig = Repo._hash_file

        def fake(repo_self, path):
            if path.replace("\\", "/").endswith("底板.sldprt"):
                raise OSError("文件正被另一个程序使用")
            return orig(repo_self, path)

        with mock.patch.object(Repo, "_hash_file", fake):
            st = self.repo.status()
        self.assertNotIn("底板.sldprt", st["deleted"],
                         "读不了就被报成删除——用户会以为自己的图纸没了")
        self.assertTrue(st["skipped"], "读不了的文件必须在 skipped 里点名")
        self.assertIn("底板.sldprt", [x["path"] for x in st["skipped"]])

    def test_undeletable_extra_file_aborts_instead_of_claiming_success(self):
        """多出来的文件删不掉（只读/被占用）：不能报成功，也不能留个半新半旧的工作区"""
        extra = self.write("多余.txt", "不属于这个版本")
        os.chmod(extra, 0o444)
        self.addCleanup(os.chmod, extra, 0o666)
        self.write("底板.sldprt", "改过但这次恢复要把它换回去")
        with self.assertRaises(RepoError) as e:
            self.repo.restore(self.c1)
        self.assertIn("删不掉", str(e.exception))
        self.assertTrue(os.path.exists(extra), "回滚之后文件应该还在")
        with open(os.path.join(self.root, "底板.sldprt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "改过但这次恢复要把它换回去", "没回滚干净")

    def test_undeletable_extra_without_backup_is_reported(self):
        """--no-backup 时没法回滚，那也必须如实报出来，不能一句「恢复完成」"""
        extra = self.write("多余.txt", "不属于这个版本")
        os.chmod(extra, 0o444)
        self.addCleanup(os.chmod, extra, 0o666)
        res = self.repo.restore(self.c1, backup=False)
        self.assertEqual([x["path"] for x in res["failed_remove"]], ["多余.txt"])

    def test_restore_plan_warns_about_busy_files(self):
        self.write("底板.sldprt", "改过了但正被占着")
        orig = Repo._hash_file

        def fake(repo_self, path):
            if path.replace("\\", "/").endswith("底板.sldprt"):
                raise OSError("占用")
            return orig(repo_self, path)

        with mock.patch.object(Repo, "_hash_file", fake):
            plan = self.repo.restore_plan(self.c1)
        self.assertIn("底板.sldprt", plan.get("busy") or [],
                      "恢复会覆盖它，确认框里却只字不提")


# ------------------------------------------------------------------ 5 写盘中的小文件
class BusySplitTest(Base):
    def churn(self, rel, interval=0.02):
        """持续往文件里写（模拟日志 / CAD 的锁文件）"""
        stop = threading.Event()
        path = os.path.join(self.root, rel)

        def run():
            while not stop.is_set():
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("x" * 30)
                except OSError:
                    pass
                time.sleep(interval)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return stop

    def test_busy_untracked_small_file_is_skipped_not_blocking(self):
        """一个永远在写的日志不能把「记录」永久锁死"""
        self.write("底板.sldprt", "改过了")
        stop = self.churn("auto.log")
        try:
            time.sleep(0.1)
            oid = self.repo.commit("改了底板")
        finally:
            stop.set()
        self.assertIsNotNone(oid, "有日志在写就记不了版本——软件没法用了")
        files = self.repo.files_of(oid)[1]
        self.assertIn("底板.sldprt", files)
        self.assertNotIn("auto.log", files, "写了一半的日志不该进版本")
        self.assertIn("auto.log", [x["path"] for x in self.repo.last_skipped])

    def test_busy_tracked_file_still_blocks(self):
        """已经在版本里的文件正在写：宁可记不成，也不能把半截存进去"""
        stop = self.churn("底板.sldprt")
        try:
            time.sleep(0.1)
            with self.assertRaises(RepoError) as e:
                self.repo.commit("手快点了记录")
            self.assertIn("还在被写入", str(e.exception))
        finally:
            stop.set()


# ------------------------------------------------------------------ 6/7 合并
class MergeFixTest(Base):
    def test_both_kept_never_overwrites_an_existing_name(self):
        """同一次合并里两次「都留」→ 第二份被第一份的名字盖掉，用户以为留了两份"""
        o_mine = self.repo.put("blob", b"mine")
        o_theirs = self.repo.put("blob", b"theirs")
        o_other = self.repo.put("blob", b"other")
        head = self.repo.head_oid()
        p = {
            "base": None, "ours": head, "theirs": head,
            "entries": [
                {"path": "x.txt", "status": "conflict", "pick": "both",
                 "ours": {"oid": o_mine, "size": 4}, "theirs": {"oid": o_theirs, "size": 6}},
                {"path": "x.本机版本.txt", "status": "ours", "pick": "ours",
                 "ours": {"oid": o_other, "size": 5}, "theirs": None},
            ],
        }
        res = merge_mod.apply(self.repo, p, checkout=False)
        files = self.repo.files_of(res["commit"])[1]
        self.assertEqual(files["x.本机版本.txt"]["oid"], o_other,
                         "项目里本来就有的同名文件被「都留」盖掉了")
        self.assertIn("x.本机版本(2).txt", files, "对方那一版没留下来")
        self.assertEqual(files["x.本机版本(2).txt"]["oid"], o_theirs)
        self.assertEqual(files["x.txt"]["oid"], o_mine)

    def test_stale_plan_is_refused(self):
        """计划算完到点合并之间又记了一版：合并挂到旧父提交上，新版本会被挤出分支"""
        self.write("说明.txt", "第二版说明")
        c2 = self.repo.commit("v2")
        p = {"base": None, "ours": c2, "theirs": c2, "entries": []}
        self.write("底板.sldprt", "又改了")
        c3 = self.repo.commit("v3（计划之后才记的一版）")
        p["entries"] = [{"path": "底板.sldprt", "status": "ours", "pick": "ours",
                         "ours": {"oid": self.repo.files_of(c2)[1]["底板.sldprt"]["oid"],
                                  "size": 4}, "theirs": None}]
        with self.assertRaises(merge_mod.MergeError) as e:
            merge_mod.apply(self.repo, p, checkout=False)
        self.assertIn("重新", str(e.exception))
        self.assertEqual(self.repo.head_oid(), c3, "被拒绝的合并不该动 HEAD")


# ------------------------------------------------------------------ 3 同步落地失败
class SyncCheckoutFailTest(Base):
    def test_failed_checkout_leaves_pull_needed_marker(self):
        """同步时图纸正被 SolidWorks 开着：分支别前进了却一声不吭"""
        self.write("底板.sldprt", "版本二")
        self.write("新增.txt", "对方新增的文件")
        c2 = self.repo.commit("v2 对方版本")

        store = os.path.join(self.tmp, "nas", "share")
        be = remotes.StoreBackend(remotes.LocalStore(store), project="P", repo=self.repo)
        sync(self.repo, branch="main", log=lambda *_: None, backend=be)   # 推上去

        # 本机退回 v1 且工作区干净：这样同步会走「快进 + 落地」
        self.repo.restore(self.c1, backup=False)
        self.repo.set_branch("main", self.c1)
        self.assertTrue(self.repo.status()["clean"])
        # 把要改的那个文件设成只读（等同被 SolidWorks 占着）：恢复必然失败
        p = os.path.join(self.root, "底板.sldprt")
        os.chmod(p, 0o444)
        self.addCleanup(os.chmod, p, 0o666)

        be2 = remotes.StoreBackend(remotes.LocalStore(store), project="P",
                                   repo=self.repo)
        with self.assertRaises(SyncError) as e:
            sync(self.repo, push=False, pull=True, checkout=True, branch="main",
                 log=lambda *_: None, backend=be2)
        self.assertIn("工作区", str(e.exception))
        marker = os.path.join(self.repo.meta, "PULL_NEEDED")
        self.assertTrue(os.path.isfile(marker),
                        "分支已经前进、工作区没更新，却没留「待更新」标记："
                        "下一次自动记录会把旧文件记成新版本再推回对方")
        with open(marker, encoding="utf-8") as f:
            j = json.load(f)
        self.assertEqual(j["head"], c2)
        self.assertIn("工作区", j["reason"])


# ------------------------------------------------------------------ 8/9/10 收尾
class FinishPendingRestoreTest(Base):
    def journal(self, **kw):
        j = {"commit": self.c1, "target": self.root, "backup": None,
             "time": 1, "files": 2}
        j.update(kw)
        with open(self.repo._journal_path(), "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)

    def test_interrupted_export_is_not_finished_as_restore(self):
        """导出被强杀后点收尾：按恢复去跑会把导出目录里别人的文件当多余删掉"""
        out = os.path.join(self.tmp, "导出目录")
        os.makedirs(out)
        with open(os.path.join(out, "用户自己的资料.docx"), "w", encoding="utf-8") as f:
            f.write("别删我")
        self.journal(target=out, mirror=False, with_backup=False)
        self.repo.finish_pending_restore("again")
        self.assertTrue(os.path.exists(os.path.join(out, "用户自己的资料.docx")),
                        "收尾把导出目录里不属于这个项目的文件删了")
        self.assertTrue(os.path.exists(os.path.join(out, "底板.sldprt")),
                        "导出该补的文件还是要补上")

    def test_backup_finish_removes_files_created_by_restore(self):
        """选「回到恢复之前」：恢复时新建的文件也要撤掉，索引跟着对齐"""
        bak = os.path.join(self.repo.meta, "backup", "20260101-000000-9")
        os.makedirs(bak)
        with open(os.path.join(bak, "说明.txt"), "w", encoding="utf-8") as f:
            f.write("备份里的原文")
        os.remove(os.path.join(self.root, "底板.sldprt"))     # 恢复之前工作区没有它
        self.write("底板.sldprt", "版本一")                   # 恢复把它写回来了（正是这版内容）
        self.journal(backup=bak, created=["底板.sldprt"])
        res = self.repo.finish_pending_restore("backup")
        self.assertIn("底板.sldprt", res["deleted"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "底板.sldprt")),
                         "恢复新建的文件留在了工作区")
        with open(os.path.join(self.root, "说明.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "备份里的原文")
        self.assertNotIn("底板.sldprt", self.repo.status()["modified"],
                         "索引没跟着对齐：界面会显示一堆假的未记录改动")

    def test_backup_finish_keeps_file_user_edited_after_crash(self):
        bak = os.path.join(self.repo.meta, "backup", "20260101-000000-10")
        os.makedirs(bak)
        self.write("底板.sldprt", "用户自己在恢复之后又改的")
        self.journal(backup=bak, created=["底板.sldprt"])
        res = self.repo.finish_pending_restore("backup")
        self.assertNotIn("底板.sldprt", res["deleted"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "底板.sldprt")),
                        "用户事后自己改过的不该被收尾删掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
