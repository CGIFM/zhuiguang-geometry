"""项目文件夹被搬到别处后，在列表里「重新定位」

真实场景：把项目从 D 盘挪到移动硬盘、或者改了个上层目录名。
以前列表里只会写「目录不存在」，用户只能「从列表移除」再「添加文件夹」——
中间要是选错了，会以为项目丢了（历史其实还在 .zgr 里）。

用法：python -I tests/e2e_relocate.py
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-relocate")
OLD = os.path.join(TMP, "老位置", "图纸库")
NEW = os.path.join(TMP, "新位置", "图纸库")
SHOTS = os.path.join(TMP, "shots")
PORT = 8916
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(OLD)
    os.makedirs(SHOTS)
    repo = Repo(OLD).init("图纸库")
    with open(os.path.join(OLD, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("第一版")


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


def api_post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        # 选文件夹的系统对话框在浏览器里没法点：直接给它答案
        page.route("**/api/pick", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "paths": [NEW]}, ensure_ascii=False)))
        page.goto(BASE + "/")
        page.wait_for_selector(".proj", timeout=15000)
        page.wait_for_timeout(400)

        sub = page.inner_text(".proj .proj-sub")
        check("目录不存在" in sub, "搬走之后列表里明确写着找不到", sub)
        page.screenshot(path=os.path.join(SHOTS, "1-missing.png"))

        page.click(".proj", button="right")
        page.wait_for_selector(".ctx-menu button[data-act=relocate]", timeout=5000)
        check(True, "右键菜单里有「重新定位」")
        page.click(".ctx-menu button[data-act=relocate]")
        page.wait_for_timeout(1500)

        rows = page.evaluate("""() => [...document.querySelectorAll('.proj')].map((el) => ({
          path: el.dataset.path,
          sub: el.querySelector('.proj-sub').textContent,
          name: el.querySelector('.proj-name').textContent.trim()}))""")
        check(len(rows) == 1, "列表里还是一个项目（不是多一个少一个）", str(len(rows)))
        check(os.path.normcase(rows[0]["path"]) == os.path.normcase(NEW),
              "路径已经换成新位置", rows[0]["path"])
        check("目录不存在" not in rows[0]["sub"], "不再提示找不到", rows[0]["sub"])
        page.wait_for_timeout(600)
        page.screenshot(path=os.path.join(SHOTS, "2-relocated.png"))

        # 换个不是追光项目的文件夹：要拒绝，而且旧记录不能丢
        other = os.path.join(TMP, "随便一个文件夹")
        os.makedirs(other, exist_ok=True)
        page.unroute("**/api/pick")
        page.route("**/api/pick", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "paths": [other]}, ensure_ascii=False)))
        page.click(".proj", button="right")
        page.wait_for_selector(".ctx-menu button[data-act=relocate]", timeout=5000)
        page.click(".ctx-menu button[data-act=relocate]")
        page.wait_for_timeout(1200)
        rows2 = page.evaluate("""() => [...document.querySelectorAll('.proj')]
          .map((el) => el.dataset.path)""")
        check(rows2 and os.path.normcase(rows2[0]) == os.path.normcase(NEW),
              "选错文件夹时原记录还在", str(rows2))
        toast = page.evaluate("() => (document.querySelector('.toast') || {}).textContent || ''")
        check("还不是追光项目" in toast or "没删掉" in toast, "并且说清楚为什么", toast[:40])

        check(not errors, "没有 JS 报错", "；".join(errors[:3]))
        b.close()


def main():
    build()
    proc = serve()
    try:
        api_post("/api/add", {"path": OLD, "target": OLD})
        # 整个项目文件夹搬到新位置（.zgr 一起搬，历史不丢）
        os.makedirs(os.path.dirname(NEW), exist_ok=True)
        shutil.move(OLD, NEW)
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
