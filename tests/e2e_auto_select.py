"""打开项目，右侧就该有东西看——默认选中最新一版（真实浏览器）

以前第一次打开项目，右边是一大片空白加一句「从左侧时间线选择一个版本查看详情」。
用户得先猜一句「要我自己点一下」。最新一版正是最该看的：这一版改了什么、多大、谁记的。
现在打开就自动选中它；只有一条版本都没有的新项目才显示空状态，而且话说得明白。

用法：python -I tests/e2e_auto_select.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-autoselect")
ROOT = os.path.join(TMP, "自动选中")
EMPTY = os.path.join(TMP, "空项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8924
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(EMPTY)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("自动选中")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("起始版本", "测试")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v2")
    repo.commit("改侧板", "测试")
    with open(os.path.join(ROOT, "新件.sldprt"), "w", encoding="utf-8") as f:
        f.write("new")
    repo.commit("加一个新件", "测试")
    Repo(EMPTY).init("空项目")                # 一条版本都没有


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
        page.wait_for_selector("#detail .detail-inner", timeout=15000)
        page.wait_for_timeout(800)

        title = page.evaluate("() => (document.querySelector('#detail h2')||{}).textContent || ''")
        check("加一个新件" in title, "右侧直接显示最新一版", title[:40])
        active = page.evaluate("""() => {
          const el = document.querySelector('#timeline .commit.active');
          return el ? el.textContent.slice(0, 40) : '';
        }""")
        check("加一个新件" in active, "时间线上最新一版是选中状态", active)
        body = page.evaluate("() => document.getElementById('detail').textContent")
        check("从左侧时间线选择" not in body, "不再显示「请自己选一个」的空状态")
        rows = page.evaluate("() => document.querySelectorAll('#detail .frow').length")
        check(rows >= 1, "清单已经铺出来（能看到这一版动了哪些文件）", str(rows))
        page.screenshot(path=os.path.join(SHOTS, "1-auto.png"))

        # 点更早的一版：右键仍然跟着换
        page.evaluate("""() => {
          const list = document.querySelectorAll('#timeline .commit');
          if (list.length > 1) list[list.length - 1].click();
        }""")
        page.wait_for_timeout(1200)
        title2 = page.evaluate("() => (document.querySelector('#detail h2')||{}).textContent || ''")
        check("起始版本" in title2, "点老版本还是能切过去", title2[:40])

        # 一条版本都没有的新项目：空状态要说得明白，不是「请自己选」
        page.goto(BASE + "/#p=" + urllib.parse.quote(EMPTY))
        page.wait_for_timeout(2500)
        empty = page.evaluate("() => document.getElementById('detail').textContent")
        check("还没有" in empty or "立即记录" in empty, "空项目给的是「还没有版本」的说法", empty[:60])
        page.screenshot(path=os.path.join(SHOTS, "2-empty.png"))
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
