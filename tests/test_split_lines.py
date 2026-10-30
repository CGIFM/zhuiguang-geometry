# -*- coding: utf-8 -*-
"""一人一条线：两个人各记各的分支，互相看得见，合不合并不由程序替你决定

场景（两台机器用「共享目录」当通道，逻辑和 HTTP 那条路完全一样）：

    张三那边：main → 张三            （他自己的线，两个版本）
    我这边：    main → 张三            （我自己的线）
    开实时同步（split）之后：
      · 他的线出现在我这边的分支列表里，时间线多一列
      · 我的工作区**一个字节都不动**，也没被合并
      · 我继续在我自己的线上记，推过去，他那边也能看到
      · 想合的时候才手动「并入」
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import live as L
from zgr.store import Repo


def _write(root, rel, text):
    p = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def _read(root, rel):
    with open(os.path.join(root, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def _set_author(repo, name):
    cfg = repo.config
    cfg["author"] = name
    repo._write_json("config.json", cfg)


class _ShareLive(L.LiveSync):
    """把实时同步的后端换成「共享目录」，就不用起 HTTP 服务了"""

    def __init__(self, repo, share_root, **kw):
        self.share_root = share_root
        super().__init__(repo, "share://" + share_root, **kw)

    def _be(self):
        from zgr.sync import make_backend
        return make_backend(share=self.share_root, project=self.repo.name)


class SplitLinesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-split-")
        self.share = os.path.join(self.tmp, "share")
        os.makedirs(self.share)

        # 他的机器：一个项目，两条提交都在「张三」这条线上
        theirs = os.path.join(self.tmp, "A", "支架")
        os.makedirs(theirs)
        self.rA = Repo(theirs).init("支架")
        _set_author(self.rA, "张三")
        _write(theirs, "readme.txt", "他的第一版")
        _write(theirs, "他的零件.txt", "他画的")
        c1 = self.rA.commit("第一版")
        # 从 main 分出「张三」这条线
        L.ensure_personal_branch(self.rA, "张三")
        _write(theirs, "readme.txt", "他的第二版")
        self.their_head = self.rA.commit("第二版")
        self.assertNotEqual(c1, self.their_head)

        # 我的机器：同名项目，我自己的线
        mine = os.path.join(self.tmp, "B", "支架")
        os.makedirs(mine)
        self.rB = Repo(mine).init("支架")
        _set_author(self.rB, "张三")
        _write(mine, "readme.txt", "我的第一版")
        _write(mine, "我的零件.txt", "我画的")
        self.rB.commit("我的第一版")
        L.ensure_personal_branch(self.rB, "张三")
        self.my_head = self.rB.head_oid()
        self.assertEqual(self.rB.branch, "张三")
        # 他那边也开着实时同步：先把自己的线推到「共享目录」这个中转站
        self._publish_theirs()

    def _publish_theirs(self):
        """模拟他那边的实时同步：把「张三」这条线推到中转站"""
        from zgr.sync import make_backend, reachable
        be = make_backend(share=self.share, project="支架")
        head = self.rA.head_oid("张三") or self.rA.head_oid()
        be.push(self.rA, sorted(reachable(self.rA, head)))
        be.set_ref("张三", be.repo.head_oid("张三"), head)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 分分支
    def test_personal_branch_is_cut_from_current_head(self):
        r = Repo(os.path.join(self.tmp, "B", "支架"))
        self.assertEqual(r.branch, "张三")
        self.assertIn("main", r.branches(), "原来那条线要留着，否则就是「改名」不是「分支」")
        self.assertEqual(r.head_oid("main"), self.my_head,
                         "新线应当从当时那一版分出来，指针指向同一个提交")
        self.assertEqual(_read(r.root, "readme.txt"), "我的第一版",
                         "分分支不该动工作区")

    def test_personal_branch_is_idempotent(self):
        note = L.ensure_personal_branch(self.rB)
        self.assertFalse(note["created"])
        self.assertFalse(note["switched"])
        self.assertEqual(self.rB.branch, "张三")
        self.assertEqual(self.rB.head_oid(), self.my_head)

    def test_set_ref_does_not_move_my_branch_or_index(self):
        idx_before = self.rB._read_json("index.json", {})
        self.rB.set_ref("别人的线", self.my_head)
        self.assertEqual(self.rB.branch, "张三", "同步别人的线绝不能把我切走")
        self.assertEqual(self.rB.head_oid(), self.my_head)
        idx_after = self.rB._read_json("index.json", {})
        self.assertEqual(idx_before.get("head"), idx_after.get("head"))
        self.assertEqual(idx_before.get("branch"), idx_after.get("branch"))

    # ---------------------------------------------------------------- 实时同步
    def test_split_sync_brings_his_line_without_touching_mine(self):
        w = _ShareLive(self.rB, self.share, mode="split", interval=5,
                       logger=lambda m: None)
        st = w.tick()

        self.assertEqual(self.rB.branch, "张三", "对账之后我还得在我自己的线上")
        self.assertEqual(self.rB.head_oid(), self.my_head, "我的线不能被改")
        self.assertEqual(_read(self.rB.root, "readme.txt"), "我的第一版",
                         "他的版本不能被合进来、更不许覆盖我的图纸")
        self.assertEqual(_read(self.rB.root, "我的零件.txt"), "我画的")

        self.assertIn("张三", self.rB.branches(), "他的线要在我这边看得见")
        self.assertEqual(self.rB.head_oid("张三"), self.their_head)
        self.assertEqual(st["mode"], "split")
        self.assertEqual([b["branch"] for b in st["peer_branches"]], ["张三"])

        evs = [e for e in L.read_events(self.rB)["events"]]
        self.assertTrue(any(e.get("kind") == "peer_branch_new"
                            and e.get("branch") == "张三" for e in evs),
                        "要看得到「对方开了一条新线」，不能悄悄多出来一列")

    def test_split_sync_reports_new_versions_on_his_line(self):
        w = _ShareLive(self.rB, self.share, mode="split", interval=5,
                       logger=lambda m: None)
        w.tick()
        # 他在自己的线上又记了一版
        _write(self.rA.root, "readme.txt", "他的第三版")
        self.rA._write_text("HEAD", "张三")
        new_head = self.rA.commit("第三版")
        self._publish_theirs()

        w.tick()
        self.assertEqual(self.rB.head_oid("张三"), new_head)
        self.assertEqual(self.rB.head_oid(), self.my_head)
        self.assertEqual(_read(self.rB.root, "readme.txt"), "我的第一版")
        evs = L.read_events(self.rB)["events"]
        got = [e for e in evs if e.get("kind") == "peer_branch"]
        self.assertTrue(got, "对方前进要有动态")
        self.assertIn("1 个版本", got[-1]["message"])

    def test_my_new_version_is_pushed_on_my_own_line(self):
        w = _ShareLive(self.rB, self.share, mode="split", interval=5,
                       logger=lambda m: None)
        w.tick()
        _write(self.rB.root, "我的零件.txt", "我改的第二版")
        mine2 = self.rB.commit("我的第二版")
        w.tick()

        remote = Repo(os.path.join(self.share, "支架"))
        self.assertIn("张三", remote.branches(), "我的线要推到他那边")
        self.assertEqual(remote.head_oid("张三"), mine2)
        self.assertEqual(remote.head_oid("张三"), self.their_head,
                         "推自己的线不能把对方的线弄丢")
        self.assertEqual(self.rB.branch, "张三")

    def test_split_never_merges_even_when_both_sides_changed_same_file(self):
        w = _ShareLive(self.rB, self.share, mode="split", interval=5,
                       logger=lambda m: None)
        w.tick()
        # 两边都改 readme，内容不同
        _write(self.rA.root, "readme.txt", "他改的说明")
        self.rA._write_text("HEAD", "张三")
        their2 = self.rA.commit("他改说明")
        self._publish_theirs()
        _write(self.rB.root, "readme.txt", "我改的说明")
        mine2 = self.rB.commit("我改说明")

        w.tick()
        w.tick()
        self.assertEqual(self.rB.head_oid(), mine2, "我的线必须还是我的最新版")
        self.assertEqual(self.rB.head_oid("张三"), their2)
        self.assertEqual(_read(self.rB.root, "readme.txt"), "我改的说明",
                         "一人一条线时绝不允许把对方的内容合进来")
        merged = self.rB.commit_info(mine2)
        self.assertEqual(merged.get("parents", []) and len(merged["parents"]), 1,
                         "不该凭空造出双父的合并提交")

    def test_split_reconcile_is_stable_when_nothing_changes(self):
        w = _ShareLive(self.rB, self.share, mode="split", interval=5,
                       logger=lambda m: None)
        w.tick()
        seq_before = L.read_events(self.rB)["seq"]
        for _ in range(3):
            w.tick()
        self.assertEqual(L.read_events(self.rB)["seq"], seq_before,
                         "没有新东西时不许每轮都刷动态")

    def test_shared_mode_still_auto_merges(self):
        """老行为不能被改坏：选「共用一条线」时，分叉照样自动合并成双父提交"""
        from zgr.sync import make_backend, reachable

        a_dir = os.path.join(self.tmp, "S", "共用")
        os.makedirs(a_dir)
        a = Repo(a_dir).init("共用")
        _set_author(a, "张三")
        _write(a_dir, "readme.txt", "共同起点")
        _write(a_dir, "a.txt", "A1")
        a.commit("起点")

        b_dir = os.path.join(self.tmp, "T", "共用")
        shutil.copytree(a_dir, b_dir)          # 两台机器同一段历史
        b = Repo(b_dir)
        _set_author(b, "张三")

        be = make_backend(share=self.share, project="共用")
        be.push(a, sorted(reachable(a, a.head_oid())))
        be.set_ref(b.branch, None, a.head_oid())

        base = a.head_oid()
        _write(a_dir, "a.txt", "A2")           # 他改一个文件
        a.commit("他那版")
        be.push(a, sorted(reachable(a, a.head_oid())))     # 他的实时同步把新版本推上去
        be.set_ref("main", base, a.head_oid())
        _write(b_dir, "b.txt", "B2")           # 我改另一个文件
        b.commit("我这版")

        w = _ShareLive(b, self.share, mode="shared", interval=5,
                       logger=lambda m: None)
        w.tick()
        head = b.head_oid()
        info = b.commit_info(head)
        self.assertEqual(len(info.get("parents") or []), 2,
                         "共用一条线还是应该自动合并（双父提交）")
        self.assertEqual(_read(b_dir, "a.txt"), "A2")
        self.assertEqual(_read(b_dir, "b.txt"), "B2")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SplitOverHttpTest(unittest.TestCase):
    """走真正的 HTTP 接口：我把自己的线推给他，**不能把他的工作区改成我的**

    这是最容易出事的一处：接收方原来的逻辑是「收到推送就 set_branch + restore」，
    在共用一条线时没问题；一人一条线时等于把对方从自己的线拽到我的线上，
    他正开着的图纸会被我的覆盖，看起来就像自己的版本丢了。
    """

    def setUp(self):
        from tests.test_peer_clone import _Server
        self.tmpA = tempfile.mkdtemp(prefix="zgr-httpA-")
        self.tmpB = tempfile.mkdtemp(prefix="zgr-httpB-")
        self.A = _Server(self.tmpA, "A")

        self.a_dir = os.path.join(self.tmpA, "支架")
        os.makedirs(self.a_dir)
        self.rA = Repo(self.a_dir).init("支架")
        _set_author(self.rA, "张三")
        _write(self.a_dir, "他的零件.txt", "他在画的东西")
        self.rA.commit("他的第一版")
        L.ensure_personal_branch(self.rA, "张三")
        self.A.app.registry.add(self.a_dir, "支架")

        self.b_dir = os.path.join(self.tmpB, "支架")
        os.makedirs(self.b_dir)
        self.rB = Repo(self.b_dir).init("支架")
        _set_author(self.rB, "张三")
        _write(self.b_dir, "我的零件.txt", "我在画的东西")
        self.rB.commit("我的第一版")
        L.ensure_personal_branch(self.rB, "张三")

    def tearDown(self):
        try:
            self.A.httpd.shutdown()
            self.A.httpd.server_close()
        except Exception:                                      # noqa: BLE001
            pass
        shutil.rmtree(self.tmpA, ignore_errors=True)
        shutil.rmtree(self.tmpB, ignore_errors=True)

    def test_push_to_own_branch_is_ref_only(self):
        from zgr.sync import make_backend, reachable
        be = make_backend(url=self.A.url, project="支架")
        head = self.rB.head_oid()
        be.push(self.rB, sorted(reachable(self.rB, head)))
        be.set_ref("张三", None, head)

        a = Repo(self.a_dir)
        self.assertEqual(a.branch, "张三", "收到推送不能把他切到我的分支上")
        self.assertEqual(_read(self.a_dir, "他的零件.txt"), "他在画的东西",
                         "他的工作区一个字节都不能被我的覆盖")
        self.assertFalse(os.path.exists(os.path.join(self.a_dir, "我的零件.txt")),
                         "我的文件不该出现在他的工作区里")
        self.assertEqual(a.head_oid("张三"), head, "我的线要在他那边看得见")
        self.assertEqual(a.head_oid("张三"), a.head_oid("张三"))

    def test_shared_branch_push_still_updates_worktree(self):
        """共用一条线（推到接收方当前分支）时，老行为要原样保留"""
        from zgr.sync import make_backend, reachable
        be = make_backend(url=self.A.url, project="支架")
        b = Repo(self.b_dir)
        # 让 B 和 A 在同一条线上：把 B 切到 main 并接上 A 的历史
        b._write_text("HEAD", "main")
        base = self.rA.head_oid("main")
        be2 = make_backend(url=self.A.url, project="支架")
        # 从 A 拉历史到 B
        need = [o for o in be2.manifest("main", None) if not b.has(o)]
        be2.fetch(b, need)
        b.set_branch("main", base)
        b.restore(base, backup=False)
        _write(self.b_dir, "他补的说明.txt", "共用线上新增")
        new = b.commit("共用线新增")

        self.rA._write_text("HEAD", "main")     # 接收方也站在 main 上，才是老的共用场景
        be.push(b, sorted(reachable(b, new)))
        be.set_ref("main", base, new)

        a = Repo(self.a_dir)
        self.assertEqual(a.head_oid("main"), new)
        self.assertTrue(os.path.exists(os.path.join(self.a_dir, "他补的说明.txt")),
                        "同一条线上的推送，接收方工作区还是要跟着更新")


if __name__ == "__main__":
    unittest.main(verbosity=2)
