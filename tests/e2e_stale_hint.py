"""「好几天没记录了」要提醒

真实情况：工程师一周只在交图那天点一次记录。中间攒了三十几个改动都没版本，
硬盘坏了、误删了、SolidWorks 崩了——全都没得退。这条提醒只在「真的很久」时出现，
不能天天喊（喊久了就没人看了）。

用法：python -I tests/e2e_stale_hint.py
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-stale")
ROOT = os.path.join(TMP, "久未记录项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8917
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
DAY = 86400000


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("久未记录")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("上周的记录")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("这周改的，还没记录")


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


def shift_log(page, days):
    """把时间线里的时间整体往前挪几天（不改磁盘，只改回给页面的数据）"""
    def handler(route):
        resp = route.fetch()
        try:
            d = resp.json()
        except Exception:
            return route.fulfill(response=resp)
        for c in d.get("commits") or []:
            if c.get("time"):
                c["time"] -= days * DAY
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(d, ensure_ascii=False))
    page.route("**/api/log**", handler)


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(600)
        # 刚记录过：不该有这条提醒（喊多了就没人看）
        n = page.evaluate("() => document.querySelectorAll('.pb-stale').length")
        check(n == 0, "刚记录过不提醒", str(n))

        page.reload()
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(200)
        # 把时间线里的版本时间挪到 3 天前，再刷新一次未保存条
        shift_log(page, 3)
        page.reload()
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1500)
        txt = page.evaluate("""() => {
          const el = document.querySelector('.pb-stale');
          return el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
        }""")
        check("3 天" in txt, "三天没记录会提醒", txt[:60])
        page.screenshot(path=os.path.join(SHOTS, "1-stale.png"))

        # 记一版之后提醒就该消失
        page.keyboard.press("Control+Enter")
        page.wait_for_selector(".modal .msg", timeout=5000)
        page.fill(".modal .msg", "补记一版")
        page.click(".modal .go")
        page.wait_for_selector(".modal", state="detached", timeout=20000)
        page.wait_for_timeout(1500)
        n2 = page.evaluate("() => document.querySelectorAll('.pb-stale').length")
        check(n2 == 0, "记录之后提醒消失", str(n2))
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
