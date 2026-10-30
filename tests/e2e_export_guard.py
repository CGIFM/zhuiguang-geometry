"""导出目录落在项目里面：先提醒、按两次才动（真实浏览器验证）

用法：python -I tests/e2e_export_guard.py   （需要 playwright + chromium）

不用真的跑 SolidWorks —— 这道闸在发请求之前，看的就是「第一次点只提醒」。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-guard")
ROOT = os.path.join(TMP, "导出防呆")
SHOTS = os.path.join(TMP, "shots")
PORT = 8908
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
    repo = Repo(ROOT).init("导出防呆")
    with open(os.path.join(ROOT, "底板.sldprt"), "wb") as f:
        f.write(b"fake")
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
    errors, posts = [], []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: posts.append(r.url) if "/api/export/step" in r.url else None)
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.click("#timeline .commit")          # 点开一版，右侧才有文件清单
        page.wait_for_selector(".file-bar", timeout=10000)
        page.wait_for_timeout(500)

        # 文件行上的「STEP」按钮
        page.hover(".filerows .frow")
        page.click(".filerows .frow .fstep")
        page.wait_for_selector(".modal .dest", timeout=8000)
        dest = page.input_value(".modal .dest")

        # 1) 填一个项目里面的目录 → 第一次点只提醒，不发请求
        inside = os.path.join(ROOT, "STEP导出")
        page.fill(".modal .dest", inside)
        page.click(".modal .go")
        page.wait_for_timeout(400)
        st = page.evaluate("""() => ({
          warn: !document.querySelector('.modal .dest-warn').hidden,
          btn: document.querySelector('.modal .go').textContent.trim(),
        })""")
        page.screenshot(path=os.path.join(SHOTS, "1-inside.png"))
        check(st["warn"], "点出提醒：这个文件夹在项目里面")
        check("仍然" in st["btn"], "按钮改成「仍然导出到项目里」", st["btn"])
        check(not posts, "第一次点没有发导出请求", str(posts))

        # 2) 改成项目外面 → 提醒消失、按钮复位，点下去真的发请求
        outside = os.path.join(TMP, "STEP-外面")
        page.fill(".modal .dest", outside)
        page.wait_for_timeout(200)
        st2 = page.evaluate("""() => ({
          warn: !document.querySelector('.modal .dest-warn').hidden,
          btn: document.querySelector('.modal .go').textContent.trim(),
        })""")
        check(not st2["warn"], "换到项目外面提醒就消失")
        check("仍" not in st2["btn"], "按钮恢复成正常文字", st2["btn"])
        page.screenshot(path=os.path.join(SHOTS, "2-outside.png"))

        b.close()
    check(not errors, "没有 JS 报错", "; ".join(errors[:3]))
    print("  默认目录：%s" % dest)


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
    print("\n截图：%s" % SHOTS)
    print(("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
