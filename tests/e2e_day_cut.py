"""「今天 N 个版本」不许报一个自己都不确定的数（真实浏览器）

时间线只取最近一屏（默认 400 个版本）。一个项目今天记了 500 版，
按已加载的那一屏数出来是 400——说「今天 400 个版本」就是在撒谎。
只要这一屏最老的一条都是今天的，就说明今天还有更早的没取回来，
必须写「今天 至少 400 个版本」，并指路「加载更早的」。

用法：python -I tests/e2e_day_cut.py   （需要 playwright + chromium）
"""
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-daycut")
ROOT = os.path.join(TMP, "很多人记")
SHOTS = os.path.join(TMP, "shots")
PORT = 8933
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
    repo = Repo(ROOT).init("很多人记")
    for i in range(4):
        with open(os.path.join(ROOT, "件%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("v")
        repo.commit("第 %d 版" % (i + 1), "我")


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
        # 只取 3 个版本：4 个今天的版本里，最老的那条也是今天的 → 必然还有更早的
        page.add_init_script("window.__zgrLogLimit = 3")
        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1200)

        note = page.evaluate(
            "() => (document.getElementById('dayNote').innerText||'').replace(/\\s+/g,' ').trim()")
        check("今天 4 个版本" in note, "总数是准数 4（不是数这一屏的 3）", note)
        check("下面画了 3 个" in note, "并说清这一屏只画了 3 个", note)
        check("加载更早的" in note, "并指出怎么看到全部", note)
        meta = page.evaluate("() => document.getElementById('timelineMeta').innerText")
        check("更早的还没加载" in meta, "抬头同样说明了还没加载", meta)
        page.screenshot(path=os.path.join(SHOTS, "1-partial.png"))

        # 点「加载更早的」把全部取回来之后，就该是准数了
        page.click("#btnMoreLog")
        page.wait_for_timeout(1200)
        note2 = page.evaluate(
            "() => (document.getElementById('dayNote').innerText||'').replace(/\\s+/g,' ').trim()")
        check("下面画了" not in note2, "全取回来之后不再说「只画了 N 个」", note2)
        check("今天 4 个版本" in note2, "总数仍然是 4", note2)
        check("我 4" in note2, "取全之后能按人分开数", note2)

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
