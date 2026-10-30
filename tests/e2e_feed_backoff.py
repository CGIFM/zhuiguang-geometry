"""静置后自动放慢刷新（真实浏览器验证）

用法：python -I tests/e2e_feed_backoff.py   （需要 playwright + chromium）

这台机器常年开着，界面在后台挂一天就是几万次 /api/status（每次都要扫一遍
几千个文件的目录）。没人动的时候没必要那么勤——静置后降频，一动就回到快节奏。
测试把 3 秒 / 12 秒 / 40 次换成 0.4 秒 / 3 秒 / 2 次，几秒钟就能验完。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-feed")
ROOT = os.path.join(TMP, "刷新项目")
PORT = 8909
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []

FAST, SLOW, TICKS = 400, 3000, 2


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    repo = Repo(ROOT).init("刷新")
    with open(os.path.join(ROOT, "a.txt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("起点", "测试")


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
    hits = []
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: hits.append((time.time(), r.url))
                if "/api/status" in r.url else None)
        page.add_init_script(
            "window.__zgrFeedFast=%d; window.__zgrFeedSlow=%d; window.__zgrFeedIdleTicks=%d;"
            % (FAST, SLOW, TICKS))
        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)

        # 前 2 秒：快节奏（400ms），应该有好几次
        page.wait_for_timeout(2000)
        fast_n = len(hits)
        check(fast_n >= 3, "有人看着时是快节奏（2 秒内 ≥3 次）", "%d 次" % fast_n)

        # 静置 5 秒 → 应该只剩慢节奏（3 秒一次），不会再刷屏
        t0 = time.time()
        n0 = len(hits)
        page.wait_for_timeout(5000)
        slow_n = len(hits) - n0
        check(slow_n <= 3, "静置后自动降到慢节奏（5 秒内 ≤3 次）",
              "%d 次 / %.1f 秒" % (slow_n, time.time() - t0))

        # 动一下鼠标 → 立刻回到快节奏
        page.mouse.move(300, 300)
        page.mouse.move(360, 380)
        n1 = len(hits)
        page.wait_for_timeout(1500)
        back_n = len(hits) - n1
        check(back_n >= 2, "一动鼠标就回到快节奏（1.5 秒内 ≥2 次）", "%d 次" % back_n)
        b.close()
    check(not errors, "没有 JS 报错", "; ".join(errors[:3]))


def main():
    build()
    proc = serve()
    try:
        run_ui()
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
    print(("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
