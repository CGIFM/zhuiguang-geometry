"""盘快满时，界面上必须在「点记录之前」就说出来（真实浏览器）

盘满的表现很吓人：记录写到一半失败、对象库留半截文件，接着 SolidWorks 存盘也失败。
这些提示只有在点记录之前出现才有用，事后报错只是让人更慌。
这里用拦截 /api/status 的剧本造一个「只剩 100 MB」的现场。

用法：python -I tests/e2e_disk_low.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-disk-low")
ROOT = os.path.join(TMP, "盘项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8922
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
MB = 1024 * 1024


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("盘")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("一", "测试")


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


def stub_disk(page, free_mb):
    def handler(route):
        resp = route.fetch()
        d = resp.json()
        d["disk"] = {"free": free_mb * MB, "total": 200 * 1024 * MB,
                     "low": free_mb * MB < 512 * MB, "min_free": 512 * MB}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(d, ensure_ascii=False))
    page.route("**/api/status?**", handler)


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
        stub_disk(page, 100)
        page.wait_for_timeout(4000)             # 等一轮状态轮询

        txt = page.evaluate("() => document.getElementById('banner').textContent")
        hidden = page.evaluate("() => document.getElementById('banner').hidden")
        check(not hidden, "横幅出现了")
        check("盘上只剩" in txt, "直接说「盘上只剩」", txt[:60])
        check("100.0 MB" in txt, "说出具体剩多少", txt[:60])
        check("至少留 512.0 MB" in txt, "给出建议余量", txt[:80])
        page.screenshot(path=os.path.join(SHOTS, "1-low.png"))

        # 给出口：点「看看能清什么」要能打开体积面板
        page.click("#banner button")
        page.wait_for_selector(".size-table", timeout=20000)
        rows = page.evaluate("() => document.querySelectorAll('.size-table tr').length")
        check(rows >= 6, "点按钮打开了体积面板", str(rows))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # 盘够用时横幅必须收起来（不能天天喊狼来了）
        page.unroute("**/api/status?**")
        stub_disk(page, 50000)
        page.wait_for_timeout(4000)
        hidden2 = page.evaluate("() => document.getElementById('banner').hidden")
        check(hidden2, "盘够用之后横幅收起来")
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
