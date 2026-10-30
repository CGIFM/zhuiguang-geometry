"""右侧零件清单：按类型筛选、按修改/创建日期排序、勾选一键导出 STEP（真浏览器）

用法：python -I tests/e2e_step_export.py   （需要 playwright + chromium）

导出要驱动 SolidWorks，这台测试机没有，所以 /api/export/step 和它的状态接口
在浏览器层被拦下来返回固定结果——验证的是「前端接线」：勾选谁、发给谁、
进度怎么走、失败怎么列。真正的 SolidWorks 转换在 167/174 上验。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-step")
ROOT = os.path.join(TMP, "项目")
SHOTS = os.path.join(TMP, "shots")
DEST = os.path.join(TMP, "STEP导出")
PORT = 8917
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []

FILES = [
    ("零件/底板.sldprt", 1700100000),
    ("零件/侧板.sldprt", 1700200000),
    ("零件/顶板.sldprt", 1700300000),
    ("总装.sldasm", 1700400000),
    ("说明.txt", 1700500000),
    ("图纸.pdf", 1700600000),
]


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(SHOTS)
    os.makedirs(DEST)
    repo = Repo(ROOT).init("导出测试")
    for rel, ts in FILES:
        p = os.path.join(ROOT, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("内容 " + rel)
        os.utime(p, (ts, ts))
    repo.commit("起点", "测试")


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
    sends = []
    try:
        req = urllib.request.Request(
            BASE + "/api/add", data=json.dumps({"target": ROOT}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1500, "height": 940})
            pg.on("pageerror", lambda e: errors.append(str(e)))

            job = {"state": "running", "phase": "导出中", "done": 0, "total": 3,
                   "detail": "", "error": "", "dest": DEST, "result": None}

            def on_step(route):
                sends.append(json.loads(route.request.post_data or "{}"))
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "job": {
                                  "id": "job1", "state": "running", "phase": "准备",
                                  "done": 0, "total": 3}}))

            def on_status(route):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "job": job}))

            def on_pick(route):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "paths": [DEST], "canceled": False}))

            pg.route("**/api/export/step", on_step)
            pg.route("**/api/export/step/status*", on_status)
            pg.route("**/api/pick", on_pick)

            pg.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
            pg.wait_for_selector("#timeline .commit", timeout=20000)
            pg.click("#timeline .commit")            # 点开一版，右侧才有文件清单
            pg.wait_for_selector(".file-bar", timeout=10000)

            def rows():
                return pg.eval_on_selector_all(".filerows .frow .fn",
                                               "els => els.map(e => e.textContent)")

            def wait_rows(n):
                for _ in range(60):
                    if len(rows()) == n:
                        return True
                    pg.wait_for_timeout(100)
                return False

            check(wait_rows(6), "清单先把 6 个文件都铺出来", str(rows()))
            opts = pg.eval_on_selector_all(".file-ext option",
                                           "els => els.map(e => e.value)")
            check("SLDPRT" in opts and "PDF" in opts, "类型下拉里列出了实际存在的类型", str(opts))
            pg.screenshot(path=os.path.join(SHOTS, "1-list.png"))

            # ---- 1. 按类型筛选 ----
            pg.select_option(".file-ext", "SLDPRT")
            check(wait_rows(3), "选 SLDPRT 只剩 3 个零件", str(rows()))
            pg.select_option(".file-ext", "")
            check(wait_rows(6), "清掉类型筛选又回 6 个")

            # ---- 2. 按修改日期排序 ----
            pg.select_option(".file-sort", "mtime")
            pg.wait_for_timeout(900)                 # 现取时间
            asc = rows()
            check(asc and asc[0] == "底板.sldprt", "升序：最早的修改时间排在前面", str(asc))
            pg.click(".sort-dir")
            pg.wait_for_timeout(400)
            desc = rows()
            check(desc and desc[0] == "图纸.pdf", "点 ↓ 变降序，最晚的排前面", str(desc))
            pg.select_option(".file-sort", "name")
            pg.wait_for_timeout(300)
            check(rows()[0] == "底板.sldprt" or rows()[0] != desc[0], "按文件名排序生效")
            pg.screenshot(path=os.path.join(SHOTS, "2-sorted.png"))

            # ---- 3. 勾选 + 批量导出 ----
            pg.select_option(".file-sort", "default")
            pg.wait_for_timeout(300)
            pg.click(".sel-all")
            pg.wait_for_timeout(300)
            check("已选 6 个" in pg.inner_text(".selinfo"),
                  "全选后计数正确", pg.inner_text(".selinfo"))
            check("导出选中的 4 个" in pg.inner_text(".btn-step"),
                  "只把 SolidWorks 文件算进 STEP", pg.inner_text(".btn-step"))

            pg.click(".btn-step")
            pg.wait_for_selector(".modal .clone-info", timeout=5000)
            info = pg.inner_text(".modal .clone-info")
            check("把 4 个零件导出成 STEP" in info, "弹框写明要导几个", info)
            check(pg.input_value(".modal .dest").startswith(os.path.dirname(ROOT)),
                  "默认放在项目旁边的 STEP 文件夹", pg.input_value(".modal .dest"))
            pg.screenshot(path=os.path.join(SHOTS, "3-export-dialog.png"))

            pg.click(".modal .browse")
            pg.wait_for_timeout(500)
            check(pg.input_value(".modal .dest") == DEST, "「浏览…」换了目标文件夹")

            pg.click(".modal .go")
            pg.wait_for_selector(".modal .cp:not([hidden])", timeout=5000)
            pg.wait_for_timeout(400)
            check(bool(sends), "点了开始会 POST /api/export/step")
            sent = sends[-1] if sends else {}
            check(sorted(sent.get("files") or []) == sorted(
                [f for f, _ in FILES if f.lower().endswith((".sldprt", ".sldasm"))]),
                "发过去的就是勾上的那 4 个", str(sent.get("files")))
            check(sent.get("dest") == DEST, "目标文件夹也对", str(sent.get("dest")))

            # 收尾：把状态改成完成（带一个失败），看结果区
            job.update({"state": "done", "done": 4, "total": 4, "phase": "完成",
                        "result": {"ok": ["零件/底板.sldprt", "零件/侧板.sldprt",
                                          "零件/顶板.sldprt"],
                                   "failed": [{"rel": "总装.sldasm",
                                               "error": "缺引用件的零件", "out": "总装.step"}],
                                   "dest": DEST, "files": 3, "seconds": 12.0}})
            for _ in range(60):
                pg.wait_for_timeout(150)
                if pg.query_selector(".step-fail") and not pg.get_attribute(
                        ".step-fail", "hidden"):
                    break
            txt = pg.inner_text(".step-fail") if pg.query_selector(".step-fail") else ""
            check("总装.sldasm" in txt and "缺引用件" in txt, "失败的文件单独列出来", txt)
            check(pg.inner_text(".modal .go") == "完成", "按钮变「完成」")
            check(bool(pg.query_selector(".modal .openfolder")), "可以一键打开导出目录")
            pg.screenshot(path=os.path.join(SHOTS, "4-export-done.png"))
            pg.click(".modal .cancel")

            # ---- 4. 单个零件直接导 ----
            sends.clear()
            job.update({"state": "running", "done": 0, "total": 1,
                        "result": None, "phase": "导出中", "error": ""})
            pg.hover(".filerows .frow")
            pg.click(".filerows .frow .fstep")
            pg.wait_for_selector(".modal .clone-info", timeout=5000)
            check("把 1 个零件导出成 STEP" in pg.inner_text(".modal .clone-info"),
                  "行上的 STEP 只导这一个")
            pg.click(".modal .go")
            pg.wait_for_timeout(500)
            check(sends and len(sends[-1].get("files") or []) == 1,
                  "只发 1 个文件", str(sends[-1].get("files") if sends else None))
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
