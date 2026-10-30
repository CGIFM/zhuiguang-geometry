"""一人一条线时，时间线上要看得出「哪一版是对方记的」（真实浏览器）

以前两边的版本在时间线上长得一模一样，只有点开右侧详情才有一行小字「对方保存」。
扫一眼根本分不清哪些是同事存的、哪些是自己存的——而这正是分叉、覆盖、要不要
合并这些判断的前提。现在卡片上直接挂一个「对方」小标。

用法：python -I tests/e2e_peer_badge.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-peerbadge")
ROOT = os.path.join(TMP, "两条线")
SHOTS = os.path.join(TMP, "shots")
PORT = 8925
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
    repo = Repo(ROOT).init("两条线")
    cfg = repo.config
    cfg["author"] = "jlc"                      # 这台机器的记录人
    repo._write_json("config.json", cfg)
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("我改的", "jlc")
    with open(os.path.join(ROOT, "底板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("对方改的", "张三")            # 对方推过来的那一版
    with open(os.path.join(ROOT, "顶板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("我又改的", "jlc")
    # 另一台机器存成大写、或者名字后面多了空格：还是同一个人，不该标「对方」
    with open(os.path.join(ROOT, "面板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("本机记的（大写）", " JLC ")
    with open(os.path.join(ROOT, "门板.sldprt"), "w", encoding="utf-8") as f:
        f.write("v1")
    repo.commit("本机记的（小写）", "Jlc")


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

        info = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('#timeline .commit').forEach((el) => {
            out.push({
              text: el.textContent.slice(0, 40),
              peer: el.classList.contains('peer'),
              chip: !!el.querySelector('.peer-chip'),
            });
          });
          return out;
        }""")
        peer = [x for x in info if x["peer"]]
        mine = [x for x in info if not x["peer"]]
        check(len(info) == 5, "五版都铺出来了", str(len(info)))
        check(len(peer) == 1, "只有对方那一版带「对方」标记", str(len(peer)))
        check(all(x["chip"] for x in peer), "对方那一版有「对方」小标")
        check("对方改的" in (peer[0]["text"] if peer else ""), "标对了是哪一版",
              (peer[0]["text"] if peer else ""))
        check(all(not x["chip"] for x in mine), "自己记的版本不带标")
        caps = [x for x in mine if "大写" in x["text"] or "小写" in x["text"]]
        check(len(caps) == 2, "大小写 / 首尾空格不同也算自己记的",
              " | ".join(x["text"][:12] for x in caps))
        page.screenshot(path=os.path.join(SHOTS, "1-badge.png"))

        # 「只看我的」：一人一条线之后最常问「我这边改到哪了」。
        # 开着的时候必须看得出来是开着的，否则只看到 4 版会以为版本丢了
        page.click("#btnMineOnly")
        page.wait_for_timeout(700)
        view = page.evaluate("""() => ({
          n: document.querySelectorAll('#timeline .commit').length,
          on: document.getElementById('btnMineOnly').classList.contains('on'),
          text: document.getElementById('btnMineOnly').textContent,
          meta: document.getElementById('timelineMeta').textContent,
        })""")
        check(view["on"] and "✓" in view["text"], "开关看得出是开着的", str(view["text"]))
        check(view["n"] == 4, "只剩我记的 4 版（对方那版筛掉）", str(view["n"]))
        check("只看" in view["meta"], "抬头写清楚在按人筛", " ".join(view["meta"].split())[:50])
        # 对方那一版被藏起来了，得说出来：不然会以为对方没推上来
        check("另有 1 个是别人记的" in view["meta"],
              "并说明藏了几个别人的版本", " ".join(view["meta"].split())[:60])
        page.screenshot(path=os.path.join(SHOTS, "3-mine-only.png"))

        page.click("#btnMineOnly")
        page.wait_for_timeout(500)
        n2 = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(n2 == 5, "再点一下回到全部版本", str(n2))

        # 点开那一版：详情里的「对方保存」也还在，而且说的是同一个记录人
        page.evaluate("""() => {
          const el = document.querySelector('#timeline .commit.peer');
          if (el) el.click();
        }""")
        page.wait_for_timeout(1200)
        body = page.evaluate("() => document.getElementById('detail').textContent")
        check("对方保存" in body, "详情里仍然写「对方保存」")
        check("张三" in body, "并说出是谁记的", body[:80])
        page.screenshot(path=os.path.join(SHOTS, "2-detail.png"))
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
