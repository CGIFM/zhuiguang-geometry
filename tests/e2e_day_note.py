"""「今天改了什么」：打开项目第一眼就该看到的几句话（真实浏览器）

一人一条线之后，早上坐下来第一句想知道的是「昨天到今天，我们改了些什么」。
时间线一列一列画出来能看出来，但要数、要比对，还得一条条点开看时间。
所以抬头下面给一行摘要：今天几个版本、一共动了多少个文件、每个人各几版。

只在真的有「今天」的版本时才出现——没有就不占地方。

用法：python -I tests/e2e_day_note.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-daynote")
ROOT = os.path.join(TMP, "今天")
SHOTS = os.path.join(TMP, "shots")
PORT = 8931
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
    repo = Repo(ROOT).init("今天")
    for i, (msg, who, files) in enumerate([
            ("上午调了侧板", "我", 2),
            ("补两个定位销", "张三", 1),
            ("下午改料盘", "我", 3),
    ]):
        for j in range(files):
            with open(os.path.join(ROOT, "件%d_%d.sldprt" % (i, j)), "w",
                      encoding="utf-8") as f:
                f.write("v")
        repo.commit(msg, who)


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

        note = page.evaluate("""() => {
          const el = document.getElementById('dayNote');
          if (!el) return null;
          return {hidden: el.hidden, text: (el.innerText || '').replace(/\\s+/g, ' ').trim()};
        }""")
        check(note is not None, "时间线上方有「今天」这一行")
        check(note and not note["hidden"], "有今天的版本时显示出来", str(note))
        text = (note or {}).get("text", "")
        check("3" in text and "个版本" in text, "说清今天记了几个版本", text)
        check("6" in text and "个文件" in text, "说清一共动了几个文件", text)
        check("我" in text, "按人分开数（我）", text)
        check("张三" in text, "按人分开数（张三）", text)
        page.screenshot(path=os.path.join(SHOTS, "1-day-note.png"))

        # 开着「只看我的」时，摘要仍然说的是**今天全部**，不是筛完的
        # （筛的是卡片，摘要回答的是「我们改了什么」）
        page.click("#btnMineOnly")
        page.wait_for_timeout(600)
        text2 = page.evaluate(
            "() => (document.getElementById('dayNote').innerText || '').replace(/\\s+/g,' ').trim()")
        check("3" in text2 and "个版本" in text2, "筛「只看我的」时摘要仍算今天全部", text2)
        page.click("#btnMineOnly")
        page.wait_for_timeout(400)

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
