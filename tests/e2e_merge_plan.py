"""合并之前先说清楚：几个文件会变、几个两边都改了（真实浏览器）

以前点「与这一版合并」只有一个按钮和一句「合并后可撤销」——用户不知道会发生什么，
只能先按下去再看结果，而合并是要写工作区的。现在会先算一遍（不落盘）并摆出来。

用法：python -I tests/e2e_merge_plan.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-mergeplan")
ROOT = os.path.join(TMP, "两条线")
SHOTS = os.path.join(TMP, "shots")
PORT = 8929
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def write(name, text):
    with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
        f.write(text)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("两条线")
    write("说明.txt", "第一行\n第二行\n第三行\n")
    write("底板.sldprt", "底板 v1")
    write("侧板.sldprt", "侧板 v1")
    base = repo.commit("起始版本", "我")

    # 对方那条线：改说明（会和本机撞）、改侧板（只有对方改）
    repo.set_branch("对方的线", base)
    write("说明.txt", "第一行\n对方改的第二行\n第三行\n")
    write("侧板.sldprt", "侧板 v2 对方改的")
    theirs = repo.commit("对方改了两处", "张三", branch="对方的线")

    # 本机这条线：把工作区改回起始状态再动自己的地方
    write("说明.txt", "第一行\n我改的第二行\n第三行\n")
    write("侧板.sldprt", "侧板 v1")
    write("底板.sldprt", "底板 v2 我改的")
    write("顶板.sldprt", "新加的顶板")
    mine = repo.commit("我改了两处", "我")
    return mine, theirs


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


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1200)

        page.evaluate("""() => {
          const el = [...document.querySelectorAll('#timeline .commit')]
            .find(e => e.innerText.includes('对方改了两处'));
          if (el) el.click();
        }""")
        page.wait_for_timeout(1000)
        page.click(".act-mergefrom")
        page.wait_for_selector("#mergeFromPlan li", timeout=15000)
        page.wait_for_timeout(300)

        plan = page.evaluate("() => document.getElementById('mergeFromPlan').innerText")
        plan = " ".join(plan.split())
        check("共 4 个文件不一样" in plan, "先说清一共几个文件不一样", plan[:90])
        check("1 个只有对方改过" in plan, "只有对方改的：直接采用", plan[:90])
        check("2 个只有本机改过" in plan, "只有本机改的：保持不动", plan[:90])
        check("1 个两边都改了" in plan, "两边都改了：会逐行合", plan[:120])
        check("说明.txt" in plan, "点名是哪几个文件两边都改了", plan[:120])

        btn = page.evaluate("""() => {
          const b = document.querySelector('#modal .go, .modal .go, .go');
          return b ? {text: b.textContent, disabled: b.disabled} : null;
        }""")
        check(btn is not None and not btn["disabled"], "核对完了合并按钮才可用", str(btn))
        check(btn is not None and "4 个文件" in btn["text"], "按钮上写清合并几个文件",
              str(btn))
        page.screenshot(path=os.path.join(SHOTS, "1-plan.png"))

        # 点取消：不该真的合并（历史里还是 3 个版本）
        page.click(".cancel")
        page.wait_for_timeout(800)
        n = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(n == 3, "点了取消就什么都没发生", str(n))

        # 整条分支的「并入 main」本来就有预览；补上冲突文件名，跟上面那条一致
        page.click(".lane-merge")
        page.wait_for_selector(".merge-stats", timeout=15000)
        page.wait_for_timeout(300)
        stats = page.evaluate("() => document.querySelector('.merge-stats').innerText")
        stats = " ".join(stats.split())
        check("冲突" in stats and "1" in stats, "整条分支合并也先说冲突几个", stats[:80])
        files = page.evaluate("() => (document.querySelector('.merge-plan-files') || {}).innerText || ''")
        check("说明.txt" in files, "冲突的文件也点名", " ".join(files.split())[:60])
        page.screenshot(path=os.path.join(SHOTS, "2-branch.png"))
        page.click(".cancel")
        page.wait_for_timeout(600)

        check(not errors, "没有 JS 报错", "；".join(errors[:3]))
        b.close()


def main():
    build()
    proc = serve()
    try:
        run_ui()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    print("\n截图：%s" % SHOTS)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
