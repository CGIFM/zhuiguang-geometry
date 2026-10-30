"""搜「客户确认」要能搜到，哪怕那一版早就翻过去了（真实浏览器）

时间线一次只取 400 个版本（老项目几千个）。搜索框以前只过滤**已经加载的这一屏**：
用户搜一个三个月前的说明，屏幕里没有就一片空白——看起来像「这个版本不存在」，
实际只是还没加载。现在同时去服务端翻全部历史，并说清楚它不在这一屏。

这个用例把「一次只取 2 个版本」打开，把要找的那一版放在很后面。

用法：python -I tests/e2e_find_all.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-findall")
ROOT = os.path.join(TMP, "翻旧账")
SHOTS = os.path.join(TMP, "shots")
PORT = 8928
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
TARGET = "客户确认的第三版"


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("翻旧账")
    msgs = ["最早的一版", TARGET, "改料盘", "调尺寸", "补标注", "最后一版"]
    for i, m in enumerate(msgs):
        with open(os.path.join(ROOT, "侧板%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("v%d" % i)
        repo.commit(m, "我")


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
        # 只取 2 个版本：模拟老项目「更早的还没加载」
        page.add_init_script("window.__zgrLogLimit = 2;")
        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1200)

        n = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(n == 2, "这一屏只加载了 2 个版本", str(n))

        page.fill("#searchBox", "客户确认")
        page.wait_for_selector("#findBox .find-row", timeout=10000)

        box = page.evaluate("() => document.getElementById('findBox').innerText")
        check("找到" in box, "说找到了几个", " ".join(box.split())[:80])
        check("不在下面这一屏" in box, "说清楚它不在当前这一屏", " ".join(box.split())[:80])
        rows = page.evaluate("""() => [...document.querySelectorAll('#findBox .find-row')]
                                 .map(e => e.innerText)""")
        check(len(rows) >= 1, "结果里列出来了", str(len(rows)))
        check(any(TARGET in r for r in rows), "就是那一版", " | ".join(r[:30] for r in rows))
        marks = page.evaluate(
            "() => [...document.querySelectorAll('#findBox .find-row mark')]"
            ".map(e => e.textContent)")
        check(any("客户确认" in m for m in marks), "命中的词标出来了", " | ".join(marks))
        below = page.evaluate("() => document.getElementById('timeline').innerText")
        check("全历史里找到了" in below, "下面那屏的空态不误导成「没有」", " ".join(below.split())[:60])
        page.screenshot(path=os.path.join(SHOTS, "1-find.png"))

        # 键盘也能用：↓ 进结果，回车打开（跟「找零件」那套一致）
        page.focus("#searchBox")
        page.keyboard.press("ArrowDown")
        focused = page.evaluate("() => document.activeElement.className")
        check("find-row" in focused, "↓ 键能进到结果里", focused)

        # 回车：右侧直接打开这一版的详情（不是只给个版本号）
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        title = page.evaluate("() => (document.getElementById('detail') || {}).innerText || ''")
        check(TARGET in title, "点开就是那一版的详情", " ".join(title.split())[:60])
        page.screenshot(path=os.path.join(SHOTS, "2-open.png"))

        # 按「记录人」也能找：说明里根本没有这个名字，结果得标出是靠哪一项对上的
        page.fill("#searchBox", "我")
        page.wait_for_selector("#findBox .find-row", timeout=10000)
        page.wait_for_timeout(700)
        rows2 = page.evaluate("""() => [...document.querySelectorAll('#findBox .find-row')]
                                  .map(e => e.innerText)""")
        check(len(rows2) == 6, "记录人对上也算命中（6 个版本都是「我」记的）",
              str(len(rows2)))
        tags = page.evaluate(
            "() => document.querySelectorAll('#findBox .fh-why').length")
        check(tags == 6, "结果里标出「按记录人」", str(tags))
        page.screenshot(path=os.path.join(SHOTS, "3-by-author.png"))

        # 搜一个真没有的：明确说「都没有」，而不是留空白
        page.fill("#searchBox", "压根不存在的说法")
        page.wait_for_selector("#findBox .find-note", timeout=10000)
        page.wait_for_timeout(800)
        note = page.evaluate("() => document.getElementById('findBox').innerText")
        check("都没有提到" in note, "搜不到时也把话说清楚", " ".join(note.split())[:80])
        check("6" in note, "并说明一共翻了多少个版本", " ".join(note.split())[:80])

        # 清空搜索：这块要收起来，不能一直挂着上次的结果
        page.fill("#searchBox", "")
        page.wait_for_timeout(600)
        hidden = page.evaluate("() => document.getElementById('findBox').hidden")
        check(hidden, "清空搜索后结果收起来")

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
