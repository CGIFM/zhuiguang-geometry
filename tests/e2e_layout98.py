"""第 97 轮界面改动验收（真实浏览器）

三件事：
1. 左边「🛠 工具」菜单默认收起，点开/点外面收起；
2. 窗口变矮时项目列表还在（不被底部按钮挤没）；
3. 详情面板中间的分隔条能拖动，拖动后零件清单变高，双击复位。

用法：python -I tests/e2e_layout98.py
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

from zgr.store import Repo, RepoError   # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-layout98")
ROOT = os.path.join(TMP, "布局项目")
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
    repo = Repo(ROOT).init("布局")
    for i in range(3):
        for name in ("侧板.sldprt", "底板.sldprt", "顶板.sldprt", "支架.sldasm"):
            with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
                f.write("v%d-%s" % (i, name))
        repo.commit("第 %d 版" % i, "测试")


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
        page = b.new_page(viewport={"width": 1100, "height": 620})   # 故意用矮窗口
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)

        # 1) 工具菜单
        check(page.evaluate("() => document.getElementById('toolsMenu').hidden") is True,
              "工具菜单默认收起")
        page.click("#btnTools")
        page.wait_for_timeout(150)
        check(page.evaluate("() => document.getElementById('toolsMenu').hidden") is False,
              "点「工具」后菜单展开")
        n = page.evaluate("() => document.querySelectorAll('#toolsMenu button').length")
        check(n == 5, "菜单里 5 个工具", str(n))
        # 矮窗口里菜单必须整块看得见（之前往下展开，后两项被切掉）
        clipped = page.evaluate("""() => {
            const vh = innerHeight, out = [];
            for (const b of document.querySelectorAll('#toolsMenu button')) {
              const r = b.getBoundingClientRect();
              if (r.top < 0 || r.bottom > vh) out.push(b.textContent.trim());
            }
            return out;
        }""")
        check(not clipped, "5 个工具都在窗口里", "被切：" + "、".join(clipped))
        page.screenshot(path=os.path.join(SHOTS, "1-tools-open.png"))
        page.click("#btnDoctor")                 # 菜单里的第一个工具（体检）
        page.wait_for_selector("#modalRoot .modal", timeout=10000)
        check(True, "体检面板能打开")
        title = page.evaluate("() => (document.querySelector('#modalRoot .modal-head h3')||{}).textContent || ''")
        check("体检" in title, "打开的是体检", title.strip())
        page.screenshot(path=os.path.join(SHOTS, "2-doctor.png"))
        page.wait_for_timeout(150)
        check(page.evaluate("() => document.getElementById('toolsMenu').hidden") is True,
              "选完自动收起")
        page.click("#modalRoot .modal-x")
        page.wait_for_timeout(200)

        # 2) 矮窗口下项目列表还在
        h = page.evaluate("() => { const e = document.querySelector('.proj-list');"
                          " return e ? e.getBoundingClientRect().height : 0 }")
        check(h >= 60, "矮窗口里项目列表没被挤没", "%d px" % h)

        # 3) 分隔条拖动
        page.click("#timeline .commit")
        page.wait_for_selector(".detail-splitter", timeout=10000)
        before = page.evaluate("() => document.querySelector('.filerows').getBoundingClientRect().height")
        box = page.locator(".detail-splitter").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 - 90, steps=8)
        page.mouse.up()
        page.wait_for_timeout(200)
        after = page.evaluate("() => document.querySelector('.filerows').getBoundingClientRect().height")
        check(after > before + 40, "往上拖之后零件清单变高", "%.0f → %.0f px" % (before, after))
        page.screenshot(path=os.path.join(SHOTS, "3-splitter.png"))

        page.dblclick(".detail-splitter")
        page.wait_for_timeout(200)
        reset = page.evaluate("() => document.querySelector('.filerows').getBoundingClientRect().height")
        check(abs(reset - before) < 12, "双击复位", "%.0f → %.0f px" % (after, reset))

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
