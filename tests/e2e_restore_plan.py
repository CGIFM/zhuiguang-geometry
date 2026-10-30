"""防呆两件事（真实浏览器验证）

用法：python -I tests/e2e_restore_plan.py   （需要 playwright + chromium）

1. 恢复整个项目之前，确认框里要**先摆出后果**：覆盖几个、新增几个、删除几个，
   以及会写多大；一个文件都不用改的版本，按钮直接变灰。
2. 删除「还有没合并版本」的分支，必须**手打分支名**才解锁按钮；
   已合并的分支不受这个限制。
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from zgr.store import Repo                  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-poka")
ROOT = os.path.join(TMP, "防呆项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8907
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def write(rel, text):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def read(rel):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("防呆测试")
    write("a.txt", "一")
    write("b.txt", "旧")
    c1 = repo.commit("起点", "测试")
    time.sleep(1.1)
    write("b.txt", "改过的")
    write("c.txt", "新加的")
    repo.commit("第二版", "测试")
    # 一条还有未合并版本的分支，用来验「删分支要打名字」
    repo.set_branch("side", c1)
    write("s.txt", "侧枝")
    repo.commit("侧枝版本", "测试", branch="side")
    repo._write_text("HEAD", "main")
    os.remove(os.path.join(ROOT, "s.txt"))
    # 一个「改了还没记录」的文件：恢复会把它连同 c.txt 一起移除，必须先说清楚
    write("d-未记录.txt", "画了一半还没记")
    return c1


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


def run_ui(c1):
    from playwright.sync_api import sync_playwright
    errors, reqs = [], []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: reqs.append(r.url.split("/api/")[-1])
                if "/api/" in r.url else None)
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(600)

        # ---- 1. 恢复前的试算
        # 按提交号选中「起点」那一版（时间线是分支板，用位置点容易点到别的分支上）
        page.evaluate("(id) => selectCommit(id)", c1)
        page.wait_for_selector(".act-restore", timeout=8000)
        page.click(".act-restore")
        page.wait_for_selector(".modal", timeout=5000)
        page.wait_for_selector(".plan-table", timeout=8000)   # 试算回来了
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "1-plan.png"))
        plan = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('.plan-table tr'))
            .map(tr => [(tr.querySelector('th')||{}).textContent,
                        (tr.querySelector('b')||{}).textContent]);
          return {
            rows: Object.fromEntries(rows.map(r => [r[0], Number(r[1] || 0)])),
            text: (document.querySelector('.restore-plan') || {}).innerText || '',
            disabled: document.querySelector('.modal .go').disabled,
            goText: document.querySelector('.modal .go').textContent,
          };
        }""")
        print("  试算：%s" % plan["rows"])
        check(plan["rows"].get("覆盖") == 1, "说清楚要覆盖 1 个文件", str(plan["rows"]))
        check(plan["rows"].get("删除") == 2,
              "说清楚要删掉 2 个文件（c.txt + 未记录的 d）", str(plan["rows"]))
        check("一共要写" in plan["text"], "写明总共要写多大")
        check(not plan["disabled"], "试算回来后确认按钮可用")
        # 有未记录的改动：要说出来，并且就地给一条「先记录一版」的出路
        dirty = page.evaluate("""() => ({
          text: (document.querySelector('.restore-plan') || {}).innerText || '',
          snap: !!document.querySelector('.modal .snapfirst'),
          go: document.querySelector('.modal .go').textContent,
        })""")
        check("没记录" in dirty["text"], "点名还有没记录的改动", dirty["text"][:60])
        check(dirty["snap"], "给「先记录一版」的按钮")
        check("未记录的改动" in dirty["go"], "确认按钮上写明会覆盖几个", dirty["go"])
        check(read("b.txt") == "改过的", "试算一个字节都没动工作区")

        # ---- 真恢复
        page.click(".modal .go")
        page.wait_for_selector(".modal", state="detached", timeout=20000)
        page.wait_for_timeout(1000)
        check(read("b.txt") == "旧", "恢复后 b.txt 回到起点那一版")
        check(not os.path.exists(os.path.join(ROOT, "c.txt")), "c.txt 被移除")

        # ---- 同一个版本再点一次：按钮变灰「已经就是这个版本」
        page.wait_for_timeout(800)
        page.evaluate("(id) => selectCommit(id)", c1)
        page.wait_for_selector(".act-restore", timeout=8000)
        page.click(".act-restore")
        page.wait_for_selector(".plan-table", timeout=8000)
        page.wait_for_timeout(300)
        again = page.evaluate("""() => ({
          disabled: document.querySelector('.modal .go').disabled,
          goText: document.querySelector('.modal .go').textContent.trim(),
        })""")
        check(again["disabled"], "无事可做时按钮是灰的", again["goText"])
        check("已经就是这个版本" in again["goText"], "按钮上写清楚为什么按不了", again["goText"])
        page.screenshot(path=os.path.join(SHOTS, "2-noop.png"))
        page.click(".modal .cancel")
        page.wait_for_timeout(300)

        # ---- 2. 删未合并的分支要打名字
        page.click(".lane[data-branch='side'] .lane-del")
        page.wait_for_selector(".modal .confirm-name", timeout=5000)
        page.wait_for_timeout(200)
        page.screenshot(path=os.path.join(SHOTS, "3-del-branch.png"))
        st = page.evaluate("""() => ({
          disabled: document.querySelector('.modal .go').disabled,
          hasInput: !!document.querySelector('.modal .confirm-name'),
        })""")
        check(st["hasInput"], "删未合并分支时要求输入分支名")
        check(st["disabled"], "没输入之前删除按钮是灰的")
        page.fill(".modal .confirm-name", "不是这个名字")
        page.wait_for_timeout(150)
        check(page.evaluate("() => document.querySelector('.modal .go').disabled"),
              "名字打错还是灰的")
        page.fill(".modal .confirm-name", "side")
        page.wait_for_timeout(150)
        check(not page.evaluate("() => document.querySelector('.modal .go').disabled"),
              "打对了才解锁")
        page.click(".modal .cancel")            # 这次不真删
        page.wait_for_timeout(300)
        heads = json.loads(urllib.request.urlopen(
            BASE + "/api/log?" + urllib.parse.urlencode(
                {"path": ROOT, "graph": 1, "limit": 50}), timeout=20).read())["heads"]
        check("side" in heads, "点了取消，分支还在", str(list(heads)))

        b.close()
    check(not errors, "没有 JS 报错", "; ".join(errors[:3]))
    check(any("restore" in r for r in reqs), "恢复走的是 /api/restore")


def main():
    c1 = build()
    proc = serve()
    try:
        run_ui(c1)
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
