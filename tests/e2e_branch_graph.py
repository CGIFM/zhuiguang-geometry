"""分支板 + 合并 + 删除（真实浏览器验证）

用法：python -I tests/e2e_branch_graph.py   （需要 playwright + chromium）

造一个真的分叉：A → B（main），A → C（side），side 还没合并。
断言：
  1. 两条分支是两列实体卡片，列底有「由 xx 分出」
  2. 点 side 列的「并入 main」能合出一个双父版本，工作区拿到 side 的文件
  3. 合并完 side 自动收进「已合并」一格（默认不占横向地方），可展开成完整列
  4. 「×」能把这条分支删掉，主干历史不受影响
  5. 没有 JS 报错
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

try:                            # 控制台是 GBK 时别被 ⎇ 这类字符打断
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from zgr.store import Repo                  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-graph")
ROOT = os.path.join(TMP, "分支项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8905
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("分支测试")

    def write(rel, text):
        with open(os.path.join(ROOT, rel), "w", encoding="utf-8") as f:
            f.write(text)

    write("a.txt", "1")
    c1 = repo.commit("A 起点", "测试")
    time.sleep(1.1)
    write("a.txt", "2")
    c2 = repo.commit("B 主干继续", "测试")
    time.sleep(1.1)
    repo.set_branch("side", c1)                  # 从 A 分叉
    write("b.txt", "side")
    c3 = repo.commit("C 侧枝", "测试", branch="side")
    repo._write_text("HEAD", "main")
    os.remove(os.path.join(ROOT, "b.txt"))       # 主干工作区不能带着侧枝的文件
    return c1, c2, c3


def serve():
    env = dict(os.environ)
    env["APPDATA"] = os.path.join(TMP, "appdata")
    os.makedirs(env["APPDATA"], exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-I", "zgr.py", "serve", "--port", str(PORT),
                             "--host", "127.0.0.1"], cwd=APP, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            urllib.request.urlopen(BASE + "/api/ping", timeout=1).read()
            return proc
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("服务没起来")


PROBE = """() => {
  const box = document.getElementById('timeline');
  const lanes = Array.from(box ? box.querySelectorAll('.lane') : []);
  const live = lanes.filter(l => !l.classList.contains('merged-bin'));
  return {
    commits: box ? box.querySelectorAll('.commit').length : 0,
    lanes: live.length,
    laneNames: live.map(l => (l.querySelector('.lane-name') || {}).textContent || ''),
    laneCounts: live.map(l => l.querySelectorAll('.commit').length),
    heads: live.map(l => !!l.querySelector('.lane-head')),
    forked: live.filter(l => l.querySelector('.lane-fork')).length,
    current: live.filter(l => l.classList.contains('current')).length,
    currentName: (box && box.querySelector('.lane.current .lane-name') || {}).textContent || '',
    mergeBtns: box ? box.querySelectorAll('.lane-merge').length : 0,
    delBtns: box ? box.querySelectorAll('.lane-del').length : 0,
    bin: box ? box.querySelectorAll('.merged-bin').length : 0,
    chips: box ? Array.from(box.querySelectorAll('.mchip-name')).map(e => e.textContent) : [],
    meta: (document.getElementById('timelineMeta') || {}).textContent || '',
    laneWidth: lanes.length ? Math.round(lanes[0].getBoundingClientRect().width) : 0,
  };
}"""


def run_ui(c1, c2, c3):
    from playwright.sync_api import sync_playwright
    errors = []
    reqs = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: reqs.append(r.url.split("/api/")[-1])
                if "/api/" in r.url else None)
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(700)
        st = page.evaluate(PROBE)
        page.screenshot(path=os.path.join(SHOTS, "1-before-merge.png"))
        print("  合并前：%s" % {k: st[k] for k in ("commits", "lanes", "laneNames",
                                                   "laneCounts", "currentName", "meta")})
        check(st["lanes"] == 2, "两条分支 = 两列实体卡片", "%d 列 %s" % (st["lanes"], st["laneNames"]))
        check(all(st["heads"]), "每列都有分支标题卡片")
        check(set(st["laneNames"]) == {"main", "side"}, "main / side 都有名字", str(st["laneNames"]))
        check(sum(st["laneCounts"]) == 3, "提交按分支分到各列，不重不漏", str(st["laneCounts"]))
        check(st["forked"] >= 1, "列底有「由 xx 分出」接点", "%d 个" % st["forked"])
        check(st["current"] == 1 and st["currentName"] == "main", "当前分支是 main", st["currentName"])
        check(st["mergeBtns"] == 1 and st["delBtns"] == 1,
              "非当前分支才有「并入」和「删除」", "并入 %d 删除 %d" % (st["mergeBtns"], st["delBtns"]))
        check(st["bin"] == 0, "还没合并时没有「已合并」格")
        check(st["laneWidth"] >= 240, "每列是实体卡片宽度（不是细条）", "%s px" % st["laneWidth"])

        # ---- 并入 main ----
        page.click(".lane[data-branch='side'] .lane-merge")
        page.wait_for_selector(".modal .go", timeout=5000)
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "2-merge-confirm.png"))
        page.click(".modal .go")
        page.wait_for_selector(".modal h3:has-text('合并完成')", timeout=20000)
        page.wait_for_timeout(400)
        page.click(".modal .cancel")                 # 「保留」分支
        page.wait_for_timeout(900)
        st2 = page.evaluate(PROBE)
        page.screenshot(path=os.path.join(SHOTS, "3-after-merge.png"))
        print("  合并后：%s" % {k: st2[k] for k in ("lanes", "laneNames", "bin", "chips", "meta")})
        check(st2["lanes"] == 1, "合并后只剩当前分支一列", str(st2["laneNames"]))
        check(st2["bin"] == 1 and st2["chips"] == ["side"],
              "已合并的 side 收进「已合并」一格", str(st2["chips"]))
        check("已合并" in st2["meta"], "标题栏写明有几条已合并", st2["meta"])
        # 合并提交必须真的落到主干上
        page.wait_for_timeout(300)
        head = json.loads(urllib.request.urlopen(
            BASE + "/api/status?" + urllib.parse.urlencode({"path": ROOT}), timeout=20).read())
        info = json.loads(urllib.request.urlopen(
            BASE + "/api/commit?" + urllib.parse.urlencode({"path": ROOT, "id": head["head"]}),
            timeout=20).read())["commit"]
        check(len(info.get("parents") or []) == 2, "合并版本是双父提交（两边的历史都留着）",
              str(info.get("parents")))
        check(os.path.isfile(os.path.join(ROOT, "b.txt")), "分支上的文件已经落到主干工作区")

        # ---- 展开已合并 ----
        page.click(".bin-expand")
        page.wait_for_timeout(600)
        st3 = page.evaluate(PROBE)
        page.screenshot(path=os.path.join(SHOTS, "4-expanded.png"))
        check(st3["lanes"] == 2 and "side" in st3["laneNames"],
              "点「展开」后已合并的分支铺成完整列", str(st3["laneNames"]))
        check(st3["laneCounts"].count(1) >= 1, "展开后能看到 side 自己的版本", str(st3["laneCounts"]))
        page.click(".bin-toggle")
        page.wait_for_timeout(500)

        # ---- 删掉已合并的分支 ----
        page.click(".mchip-del")
        page.wait_for_selector(".modal .go", timeout=5000)
        page.screenshot(path=os.path.join(SHOTS, "5-delete-confirm.png"))
        page.click(".modal .go")
        page.wait_for_timeout(1200)
        st4 = page.evaluate(PROBE)
        page.screenshot(path=os.path.join(SHOTS, "6-after-delete.png"))
        check(st4["bin"] == 0 and "side" not in st4["laneNames"],
              "删掉之后「已合并」格也消失", str(st4["laneNames"]))
        check("已合并的历史" in st4["laneNames"],
              "侧枝删掉后它的版本归到「已合并的历史」，不会变成「其它」", str(st4["laneNames"]))
        brands = json.loads(urllib.request.urlopen(
            BASE + "/api/log?" + urllib.parse.urlencode(
                {"path": ROOT, "graph": 1, "limit": 50}), timeout=20).read())
        check(set(brands["heads"]) == {"main"}, "后端里已经只剩 main", str(list(brands["heads"])))
        check(len(brands["commits"]) == 4, "分支删了但提交一个没少（3 个 + 1 个合并）",
              "%d 个" % len(brands["commits"]))
        b.close()
    check(not errors, "没有 JS 报错", "; ".join(errors[:3]))
    check(any("branch/merge" in r for r in reqs), "合并走的是 /api/branch/merge")
    check(any("branch/delete" in r for r in reqs), "删除走的是 /api/branch/delete")


def main():
    c1, c2, c3 = build()
    proc = serve()
    try:
        run_ui(c1, c2, c3)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
    print("\n截图：%s" % SHOTS)
    print(("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
