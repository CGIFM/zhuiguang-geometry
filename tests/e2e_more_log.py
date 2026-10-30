"""时间线只加载最近 N 个版本时，必须说「更早的还没加载」（真实浏览器）

老项目上千个版本，全量取会让首屏等好几秒，所以只取最近 400 个。
问题是以前**一个字都不说**：用户以为项目历史就这么长，翻不到某个版本就以为丢了。
这个软件的全部价值就是「历史都在」，看不见和不存在在用户眼里是一回事。

用法：python -I tests/e2e_more_log.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-morelog")
ROOT = os.path.join(TMP, "老项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8919
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
N = 5                       # 5 个版本；页面上一次只取 2 个


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("老项目")
    for i in range(N):
        with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
            f.write("第 %d 版" % i)
        time.sleep(1.05)
        repo.commit("第 %d 版" % (i + 1), "测试")


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
        # 把「一次取多少个」调成 2，模拟真实的长历史
        page.add_init_script("window.__zgrLogLimit = 2;")
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(800)

        meta = page.evaluate("() => document.getElementById('timelineMeta').textContent")
        check("更早的还没加载" in meta, "写明「更早的还没加载」", meta[:60])
        n = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(n == 2, "只画了取回来的 2 个版本", str(n))
        has = page.evaluate("() => !!document.getElementById('btnMoreLog')")
        check(has, "有「加载更早的版本」按钮")
        page.screenshot(path=os.path.join(SHOTS, "1-partial.png"))

        page.click("#btnMoreLog")
        page.wait_for_timeout(1500)
        n2 = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(n2 == N, "点了之后把全部 %d 个版本都加载出来" % N, str(n2))
        meta2 = page.evaluate("() => document.getElementById('timelineMeta').textContent")
        check("更早的还没加载" not in meta2, "加载完就不再提示", meta2[:60])
        check(not page.evaluate("() => !!document.getElementById('btnMoreLog')"),
              "加载完按钮也收起来")
        page.screenshot(path=os.path.join(SHOTS, "2-all.png"))
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
