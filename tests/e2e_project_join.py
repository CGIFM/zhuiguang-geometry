"""用链接打开的项目，左边要看得见、并且一键就能加进列表（真实浏览器）

以前这种项目在左侧完全隐形：顶上开着「示例项目项目」，左边写着「还没有项目」，
用户以为没打开或者列表坏了。现在它单独摆出来（虚线框 +「还没加入列表」），
点「加入项目列表」才真的进去——不偷偷加，也不能让人找不到出口。

用法：python -I tests/e2e_project_join.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-join")
ROOT = os.path.join(TMP, "示例项目项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8923
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
    repo = Repo(ROOT).init("示例项目")
    for name in ("侧板", "底板"):
        with open(os.path.join(ROOT, name + ".sldprt"), "w", encoding="utf-8") as f:
            f.write(name)
    repo.commit("起始版本", "测试")


def serve():
    env = dict(os.environ)
    env["APPDATA"] = os.path.join(TMP, "appdata")     # 干净的项目列表：里面什么都没有
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
        page.wait_for_timeout(1200)

        box = page.evaluate("() => document.getElementById('projList').textContent")
        check("示例项目项目" in box, "左侧看得见当前项目", box[:40])
        check("还没加入列表" in box, "并说明它还没进列表", box[:60])
        check(page.locator(".proj-join").count() == 1, "有「加入项目列表」的按钮")
        page.screenshot(path=os.path.join(SHOTS, "1-not-joined.png"))

        page.click(".proj-join")
        page.wait_for_timeout(1500)
        box2 = page.evaluate("() => document.getElementById('projList').textContent")
        check("还没加入列表" not in box2, "加进去之后提示消失", box2[:60])
        check(page.locator(".proj-join").count() == 0, "按钮也收起来")
        check(page.locator(".proj").count() == 1, "列表里正好这一条")
        cnt = page.evaluate("() => document.getElementById('projCount').textContent")
        check("1 个" in cnt, "计数跟着更新", cnt)
        page.screenshot(path=os.path.join(SHOTS, "2-joined.png"))

        # 刷新之后仍然是「已加入」——不能只改个界面骗人
        page.reload()
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1200)
        box3 = page.evaluate("() => document.getElementById('projList').textContent")
        check("还没加入列表" not in box3, "刷新后确实已经登记", box3[:60])
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
