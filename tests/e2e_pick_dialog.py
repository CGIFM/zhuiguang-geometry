"""选文件夹/文件走 Windows 原生对话框（真实浏览器验证，不打真对话框）

用法：python -I tests/e2e_pick_dialog.py   （需要 playwright + chromium）

真实对话框在这里没法点（会把人卡住），所以用 Playwright 把 /api/pick 拦住，
模拟三种回答，验证前端接线是对的：
  1. 正常返回路径 → 「添加已有」直接拿这个路径去 /api/add，不再弹网页目录树
  2. 返回 409（没有可操作桌面）→ 退回网页里的目录树（老行为还在）
  3. 「从文件…」多选 → 用文件所在文件夹填进项目目录，并顺手填项目名
  4. 请求体里 kind / multiple / initial 都对
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-pick")
ROOT = os.path.join(TMP, "现有项目")
TARGET = os.path.join(TMP, "要添加的文件夹")
SHOTS = os.path.join(TMP, "shots")
PORT = 8912
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(TARGET)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("现有项目")
    with open(os.path.join(ROOT, "a.txt"), "w", encoding="utf-8") as f:
        f.write("1")
    repo.commit("v1", "测试")


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


def main():
    build()
    proc = serve()
    errors = []
    picks = []          # 前端发给 /api/pick 的请求体
    adds = []           # 前端发给 /api/add 的请求体
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))

            mode = {"pick": "ok"}

            def on_pick(route):
                body = json.loads(route.request.post_data or "{}")
                picks.append(body)
                if mode["pick"] == "ok":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "paths": [TARGET],
                                                   "canceled": False, "kind": "folder"}))
                elif mode["pick"] == "nope":
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps({"ok": True, "paths": [], "canceled": True}))
                else:
                    route.fulfill(status=409, content_type="application/json",
                                  body=json.dumps({"ok": False, "error": "程序跑在系统会话里"}))

            def on_add(route):
                adds.append(json.loads(route.request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "path": TARGET}))

            pg.route("**/api/pick", on_pick)
            pg.route("**/api/add", on_add)
            pg.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
            pg.wait_for_selector("#timeline .commit", timeout=20000)

            # ---- 1. 「添加已有」走原生对话框 ----
            pg.click("#btnAdd")
            pg.wait_for_timeout(900)
            check(bool(picks), "点「添加已有」会调 /api/pick（系统对话框）")
            check(picks and picks[-1].get("kind") == "folder",
                  "请求的是「选文件夹」", json.dumps(picks[-1] if picks else {}, ensure_ascii=False))
            check(not pg.query_selector(".modal .browser"),
                  "没有弹出网页里的目录树")
            check(bool(adds) and adds[-1].get("target") == TARGET,
                  "选完直接把路径交给 /api/add", json.dumps(adds[-1] if adds else {}, ensure_ascii=False))

            # ---- 2. 用户取消：什么都不做 ----
            mode["pick"] = "nope"
            before = len(adds)
            pg.click("#btnAddFolder")
            pg.wait_for_timeout(800)
            check(len(adds) == before, "取消选择时不会再发 /api/add")

            # ---- 3. 弹不出来（409）→ 退回网页目录树 ----
            mode["pick"] = "fail"
            pg.click("#btnAddFolder")
            pg.wait_for_selector(".modal .browser", timeout=5000)
            check(bool(pg.query_selector(".modal .browser")),
                  "系统对话框不可用时，仍能退回网页目录树")
            pg.screenshot(path=os.path.join(SHOTS, "fallback.png"))
            pg.click(".modal .cancel")
            pg.wait_for_timeout(300)

            # ---- 4. 新建项目：「从文件…」多选 ----
            mode["pick"] = "ok"
            picks.clear()

            def on_pick_files(route):
                body = json.loads(route.request.post_data or "{}")
                picks.append(body)
                files = [os.path.join(TARGET, "料盒.sldprt"), os.path.join(TARGET, "侧板.sldprt")]
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "paths": files, "canceled": False}))
            pg.unroute("**/api/pick", on_pick)
            pg.route("**/api/pick", on_pick_files)
            pg.click("#btnNew")
            pg.wait_for_selector(".modal .p", timeout=5000)
            pg.fill(".modal .p", TARGET)
            pg.click(".modal .fromfile")
            pg.wait_for_timeout(700)
            check(picks and picks[-1].get("kind") == "files" and picks[-1].get("multiple"),
                  "「从文件…」按多选文件请求",
                  json.dumps(picks[-1] if picks else {}, ensure_ascii=False))
            val = pg.input_value(".modal .p")
            name = pg.input_value(".modal .n")
            check(os.path.normcase(val) == os.path.normcase(TARGET),
                  "用文件所在文件夹填了项目目录", val)
            check(name == os.path.basename(TARGET), "项目名按文件夹名自动填好", name)
            pg.screenshot(path=os.path.join(SHOTS, "new-from-file.png"))
            pg.click(".modal .cancel")
            b.close()
        check(not errors, "没有 JS 报错", "; ".join(errors[:3]))
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
    print("\n截图：%s" % SHOTS)
    print(("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
