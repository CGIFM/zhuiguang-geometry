"""键盘手感（真实浏览器验证）

用法：python -I tests/e2e_hotkeys.py   （需要 playwright + chromium）

桌面软件该有的三件事：Esc 能关弹窗、Ctrl+F 能找零件、Ctrl+Enter 能记录。
以前弹窗只能点右上角 × 或点窗外，键盘用户被卡在里面。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-hotkeys")
ROOT = os.path.join(TMP, "快捷键项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8913
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
    repo = Repo(ROOT).init("快捷键")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("第一版", "测试")


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
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(300)

        # Ctrl+F → 找零件
        page.keyboard.press("Control+f")
        page.wait_for_selector("#searchQ", timeout=5000)
        check(True, "Ctrl+F 打开「找零件」")
        page.fill("#searchQ", "侧板")
        page.wait_for_selector(".search-row", timeout=20000)
        # Esc → 关掉弹窗（焦点在输入框里也要能关）
        page.keyboard.press("Escape")
        page.wait_for_selector("#searchQ", state="detached", timeout=5000)
        check(True, "Esc 关掉弹窗（焦点在输入框里也管用）")
        page.screenshot(path=os.path.join(SHOTS, "1-after-esc.png"))

        # Ctrl+Enter → 记录
        with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
            f.write("二")
        page.wait_for_timeout(400)
        page.keyboard.press("Control+Enter")
        page.wait_for_selector(".modal .msg", timeout=5000)
        check(True, "Ctrl+Enter 打开「记录」")
        # 弹窗已经在时，再按 Ctrl+Enter 不该叠第二个（先把焦点挪出输入框，
        # 输入框里的 Ctrl+Enter 是「确认记录」，不算重复触发）
        page.evaluate("() => document.querySelector('.modal .msg').blur()")
        page.keyboard.press("Control+Enter")
        page.wait_for_timeout(300)
        n = page.evaluate("() => document.querySelectorAll('#modalRoot .overlay').length")
        check(n == 1, "不会叠出第二个记录框", str(n))
        page.fill(".modal .msg", "快捷键记录的一版")
        page.click(".modal .go")
        page.wait_for_selector(".modal", state="detached", timeout=20000)
        page.wait_for_timeout(1200)
        cnt = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(cnt >= 2, "记录真的成功了", str(cnt))
        page.screenshot(path=os.path.join(SHOTS, "2-recorded.png"))

        # ? → 快捷键说明（焦点在页面上才触发）
        page.click("body")
        page.keyboard.press("Shift+?")
        page.wait_for_selector(".modal .plan-table kbd", timeout=5000)
        txt = page.inner_text(".modal .plan-table")
        check("Ctrl + F" in txt and "Esc" in txt, "? 打开快捷键说明", txt.replace("\n", " ")[:60])
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached", timeout=5000)
        check(True, "快捷键说明也能用 Esc 关")

        # 输入框里打 ? 不该弹说明（否则搜文件名里带 ? 就废了）
        page.keyboard.press("Control+f")
        page.wait_for_selector("#searchQ", timeout=5000)
        page.fill("#searchQ", "?")
        page.wait_for_timeout(300)
        n = page.evaluate("() => document.querySelectorAll('#modalRoot .overlay').length")
        check(n == 1, "输入框里打 ? 不会弹说明", str(n))
        page.keyboard.press("Escape")

        # Esc 在没有弹窗时不该报错
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        check(page.evaluate("() => document.querySelectorAll('#modalRoot .overlay').length") == 0,
              "没有弹窗时按 Esc 也不出错")
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
