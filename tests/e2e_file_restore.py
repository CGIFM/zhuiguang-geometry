"""只恢复一个文件到历史版本（真实浏览器）

真实场景：客户打回来说「按上周那版侧板重做」，用户只想把侧板退回去，
不想把整个项目（几百个零件）都退回上周——其它人这两天的改动会一起没掉。

用法：python -I tests/e2e_file_restore.py
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-filerestore")
ROOT = os.path.join(TMP, "单文件恢复项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8915
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
    repo = Repo(ROOT).init("单文件")
    put("侧板.sldprt", "第一版：厚度 8mm")
    put("其它零件.txt", "别的文件别动我")
    repo.commit("客户确认的第一版")
    put("侧板.sldprt", "第二版：厚度 10mm")
    put("其它零件.txt", "别的文件后来也改过")
    repo.commit("客户又改了")
    put("侧板.sldprt", "第三版：厚度 12mm")
    repo.commit("现在用的这版")


def put(rel, text):
    p = os.path.join(ROOT, rel)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


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


def row_of(page, msg):
    """找说明是 msg 的那一行，返回它的序号"""
    return page.evaluate("""(msg) => {
      const rows = [...document.querySelectorAll('.modal .frow')];
      return rows.findIndex((r) => (r.querySelector('.fn').textContent || '').trim() === msg);
    }""", msg)


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())        # 确认框直接点「确定」
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(400)

        page.keyboard.press("Control+f")
        page.wait_for_selector("#searchQ", timeout=5000)
        page.fill("#searchQ", "侧板")
        page.wait_for_selector(".search-row", timeout=20000)
        page.keyboard.press("Enter")
        page.wait_for_selector(".modal .frow", timeout=8000)
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "1-history.png"))

        n_btn = page.evaluate("() => document.querySelectorAll('.hist-restore').length")
        check(n_btn >= 3, "每一版的行尾都有「恢复这一版」", str(n_btn))

        i = row_of(page, "客户确认的第一版")
        check(i >= 0, "历史里能找到第一版那一行", str(i))
        page.locator(".modal .frow").nth(i).locator(".hist-restore").click()
        # 等文件真的被写回去
        got = ""
        for _ in range(40):
            try:
                got = read("侧板.sldprt")
            except OSError:
                got = ""
            if "第一版" in got:
                break
            page.wait_for_timeout(250)
        check("第一版" in got, "侧板真的退回了第一版", got[:20])
        check(read("其它零件.txt") == "别的文件后来也改过",
              "其它文件一个都没动", read("其它零件.txt"))

        baks = [d for d in os.listdir(os.path.join(ROOT, ".zgr", "backup"))
                if d.startswith("单文件-")]
        check(bool(baks), "覆盖前先备份了原文件", str(baks[:2]))
        page.wait_for_timeout(500)
        page.screenshot(path=os.path.join(SHOTS, "2-restored.png"))

        # 删掉的零件也能这样找回来
        os.remove(os.path.join(ROOT, "侧板.sldprt"))
        page.wait_for_timeout(600)
        page.keyboard.press("Control+Enter")
        page.wait_for_selector(".modal .msg", timeout=5000)
        page.fill(".modal .msg", "把侧板删了")
        page.click(".modal .go")
        page.wait_for_selector(".modal", state="detached", timeout=20000)
        page.wait_for_timeout(800)

        page.keyboard.press("Control+f")
        page.wait_for_selector("#searchQ", timeout=5000)
        page.fill("#searchQ", "侧板")
        page.wait_for_selector(".search-row", timeout=20000)
        page.keyboard.press("Enter")
        page.wait_for_selector(".modal .frow", timeout=8000)
        page.wait_for_timeout(400)
        i = row_of(page, "客户确认的第一版")
        check(i >= 0, "删掉之后历史还在", str(i))
        page.locator(".modal .frow").nth(i).locator(".hist-restore").click()
        back = ""
        for _ in range(40):
            try:
                back = read("侧板.sldprt")
            except OSError:
                back = ""
            if back:
                break
            page.wait_for_timeout(250)
        check("第一版" in back, "删掉的零件也能单独找回来", back[:20])
        page.screenshot(path=os.path.join(SHOTS, "3-undelete.png"))

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
