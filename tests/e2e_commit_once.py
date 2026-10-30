"""连点「立即记录」只该记一版（真实浏览器）

大项目记一版要等一两秒。这期间按钮一直是可点的，手快的人（或者回车连按）
会排进第二次请求，最后可能多出一个「什么都没有」的版本，或者让人以为记了两版。

用法：python -I tests/e2e_commit_once.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-once")
ROOT = os.path.join(TMP, "只记一版")
SHOTS = os.path.join(TMP, "shots")
PORT = 8926
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
    repo = Repo(ROOT).init("只记一版")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("起始版本", "测试")
    # 得先有未记录的改动，记录按钮才会出现
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v2 改了")


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
    hits = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))

        # 只记请求次数，不拖时间：拖时间会卡住驱动线程，反而测不出「请求进行中」
        def record(route):
            hits.append(route.request.post_data)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"ok": True, "commit": None}, ensure_ascii=False))
        page.route("**/api/snapshot", record)

        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        # 「记录一次」在「未记录变更」标签页里，默认是隐藏的，得先切过去
        page.click('[data-tab="pending"]')
        page.wait_for_selector("#btnCommit", state="visible", timeout=8000)
        page.wait_for_timeout(800)

        # 同一时刻连点三次 + 回车一次：处理函数在 await 之前就同步锁住按钮，
        # 所以这几次都得被吃掉，只留一次请求
        state = page.evaluate("""() => {
          const b = document.getElementById('btnCommit');
          const msg = document.getElementById('commitMsg');
          b.click(); b.click();
          msg.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          b.click();
          return {disabled: b.disabled, text: b.textContent};
        }""")
        check(state["disabled"], "记录过程中按钮被禁用")
        check("记录中" in state["text"], "按钮上说「记录中…」", state["text"])
        page.wait_for_timeout(1500)

        check(len(hits) == 1, "连点四次只发了一次记录请求", "实际 %d 次" % len(hits))
        label2 = page.evaluate("() => document.getElementById('btnCommit').textContent")
        check("记录一次" in label2, "记录完了按钮恢复原样", label2)
        check(not page.evaluate("() => document.getElementById('btnCommit').disabled"),
              "按钮重新可用")
        page.screenshot(path=os.path.join(SHOTS, "1-once.png"))
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
