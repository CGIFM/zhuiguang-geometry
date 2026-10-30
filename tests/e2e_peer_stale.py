"""对方掉线时，主界面必须变橙并说「已经多久没联系上」（真实浏览器）

两台机器协作最危险的错觉是「看着在同步」：同步线程明明还活着，只是对方关机了，
以前横幅永远是一句绿色的「实时同步中」。用户以为两台一致，等真发现时已经差了几十版。

用法：python -I tests/e2e_peer_stale.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-peer-stale")
ROOT = os.path.join(TMP, "配对项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8921
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
    repo = Repo(ROOT).init("配对")
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


LIVE = {}


def stub_live(page, minutes_ago, last_error="连不上对端：timed out"):
    """让 /api/project 和 /api/events 都回同一份「线程活着、但很久没对上话」的状态

    两个接口都要拦：界面每几秒就用 /api/events 里的 live 覆盖一次，
    只拦 /api/project 的话，第一次渲染对了、下一轮就被真实状态冲掉。
    """
    LIVE.update({
        "enabled": True, "running": True, "url": "http://10.0.0.9:8795",
        "interval": 15, "mode": "split", "my_branch": "我",
        "peer_author": "对方师傅", "peer_host": "PC-对方",
        "last_check": int(time.time() * 1000),
        "last_ok": int((time.time() - minutes_ago * 60) * 1000),
        "last_error": last_error, "state": "error",
        "auto": True, "auto_pull": True, "auto_push": True,
    })

    def handler(route):
        resp = route.fetch()
        d = resp.json()
        d["live"] = dict(LIVE)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(d, ensure_ascii=False))

    page.route("**/api/project?**", handler)
    page.route("**/api/events?**", handler)


def unstub(page):
    page.unroute("**/api/project?**")
    page.unroute("**/api/events?**")


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ① 掉线 42 分钟：必须变橙、说出多久、给出原因
        stub_live(page, 42)                  # 先挂上剧本，再打开页面
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1500)
        txt = page.evaluate("() => document.getElementById('pairState').textContent")
        cls = page.evaluate("() => document.getElementById('pairState').className")
        check("bad" in cls, "横幅变成警示样式（不是绿的）", cls)
        check("42 分钟前" in txt, "说清「多久没联系上」", txt[:80])
        check("对方师傅" in txt, "写清是哪台/谁", txt[:80])
        check("正在自动重试" in txt, "说明正在自动重试")
        check("timed out" in txt, "把原因也带上", txt[:120])
        # 顶部胶囊：不点任何标签也看得到「对方掉线」
        pill = page.evaluate("""() => {
          const p = document.getElementById('livePill');
          return {text: p.textContent, cls: p.className, hidden: p.hidden};
        }""")
        check(not pill["hidden"], "顶部胶囊可见（不用翻到同步页）")
        check("没联系" in pill["text"], "胶囊直接写「对方…没联系」", pill["text"])
        check("bad" in pill["cls"], "胶囊也变成警示色", pill["cls"])
        page.screenshot(path=os.path.join(SHOTS, "1-stale.png"))

        # ② 刚刚还对得上：恢复正常绿色，不误报
        unstub(page)
        stub_live(page, 0, last_error="")
        page.reload()
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(2000)
        txt2 = page.evaluate("() => document.getElementById('pairState').textContent")
        cls2 = page.evaluate("() => document.getElementById('pairState').className")
        check("bad" not in cls2 and "ok" in cls2, "刚对上时是正常状态", cls2)
        check("实时同步中" in txt2, "正常时仍然说在同步", txt2[:60])
        check("刚刚" in txt2, "并说明最近一次对上的时间", txt2[:80])
        pill2 = page.evaluate("""() => {
          const p = document.getElementById('livePill');
          return {text: p.textContent, cls: p.className};
        }""")
        check("bad" not in pill2["cls"] and "实时同步中" in pill2["text"],
              "恢复后胶囊也恢复正常", pill2["text"])
        page.screenshot(path=os.path.join(SHOTS, "2-ok.png"))
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
