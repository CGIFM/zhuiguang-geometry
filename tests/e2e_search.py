"""跨版本找零件（真实浏览器验证）

用法：python -I tests/e2e_search.py   （需要 playwright + chromium）

三千个零件的项目里翻清单找「那个侧板」，翻到眼酸。这个弹窗把**全部版本**扫一遍：
删掉的老零件也能找到（标「已删除」），点一行直接看它的改动历史。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-search")
ROOT = os.path.join(TMP, "找零件")
SHOTS = os.path.join(TMP, "shots")
PORT = 8909
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def write(rel, text):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p) or ROOT, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("找零件")
    write("结构/侧板.sldprt", "一")
    write("标准件/M3螺丝.sldprt", "一")
    repo.commit("第一版", "测试")
    time.sleep(1.1)
    write("结构/侧板.sldprt", "二")
    os.remove(os.path.join(ROOT, "标准件", "M3螺丝.sldprt"))
    repo.commit("侧板加厚，螺丝取消", "测试")


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
        page.wait_for_timeout(500)

        page.click("#btnSearch")
        page.wait_for_selector("#searchQ", timeout=5000)
        page.fill("#searchQ", "侧板")
        page.wait_for_selector(".search-row", timeout=20000)   # 第一次要建索引
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "1-search.png"))
        hit = page.evaluate("""() => {
          const r = document.querySelector('.search-row');
          return {name: r.querySelector('.fn').textContent,
                  meta: r.querySelectorAll('.fsize')[0].textContent,
                  dead: !!r.querySelector('.pill.dead'),
                  hint: document.querySelector('#searchHint').textContent};
        }""")
        check(hit["name"] == "侧板.sldprt", "按名字找到侧板.sldprt", hit["name"])
        check("2 次改动" in hit["meta"], "说清改过几次", hit["meta"])
        check(not hit["dead"], "还在的零件不标已删除")
        check("扫过" in hit["hint"], "告诉用户扫了多少文件", hit["hint"])

        # 键盘：↓ 选中、回车打开（打着字不该非得去摸鼠标）
        page.fill("#searchQ", "sldprt")
        page.wait_for_timeout(1200)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(200)
        k = page.evaluate("""() => {
          const rows = [...document.querySelectorAll('.search-row')];
          return {n: rows.length,
                  sel: rows.findIndex((r) => r.classList.contains('sel'))};
        }""")
        check(k["n"] >= 2 and k["sel"] == 1, "↓ 把选中挪到第二行", str(k))
        page.keyboard.press("Enter")
        page.wait_for_selector(".modal", timeout=8000)
        page.wait_for_timeout(400)
        kt = page.evaluate("() => (document.querySelector('.modal h3') || {}).textContent || ''")
        check("改动历史" in kt, "回车直接打开选中的那个零件", kt)
        page.keyboard.press("Escape")
        page.wait_for_selector(".modal", state="detached", timeout=5000)
        page.click("#btnSearch")
        page.wait_for_selector("#searchQ", timeout=5000)

        # 删掉的零件也要能找到，并且明确标出来
        page.fill("#searchQ", "螺丝")
        page.wait_for_timeout(900)
        dead = page.evaluate("""() => {
          const r = document.querySelector('.search-row');
          return {name: r.querySelector('.fn').textContent,
                  dead: !!r.querySelector('.pill.dead'),
                  text: (r.querySelector('.pill.dead') || {}).textContent};
        }""")
        check(dead["name"] == "M3螺丝.sldprt", "删掉的老零件也找得到", dead["name"])
        check(dead["dead"] and "已删除" in dead["text"], "明确标「已删除」", str(dead))

        # 点一行 → 直接看这个零件的改动历史
        page.click(".search-row")
        page.wait_for_selector(".modal", timeout=8000)
        page.wait_for_timeout(600)
        title = page.evaluate("() => (document.querySelector('.modal h3') || {}).textContent || ''")
        rows = page.evaluate("() => document.querySelectorAll('.modal .frow').length")
        check("改动历史" in title, "点一行打开它的改动历史", title)
        check(rows >= 1, "历史里有改动记录", str(rows))
        page.screenshot(path=os.path.join(SHOTS, "2-history.png"))
        page.click(".modal .cancel")
        page.wait_for_selector(".modal", state="detached", timeout=5000)
        page.wait_for_timeout(300)

        # 搜不到就直说，不要空白一片
        page.click("#btnSearch")
        page.wait_for_selector("#searchQ", timeout=5000)
        page.fill("#searchQ", "根本不存在的零件xyz")
        page.wait_for_timeout(1200)
        hint = page.evaluate("() => document.querySelector('#searchHint').textContent")
        check("没有匹配" in hint, "搜不到时明说没有匹配", hint)
        page.screenshot(path=os.path.join(SHOTS, "3-empty.png"))

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
