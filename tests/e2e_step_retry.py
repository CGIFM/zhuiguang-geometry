"""STEP 导出失败之后的两件事（真实浏览器验证）

用法：python -I tests/e2e_step_retry.py   （不需要 SolidWorks）

真实场景：两千多个零件里 12 个没导成（缺引用件、SolidWorks 正忙）。
以前只能看到一串文件名，然后自己去清单里一个个找、一个个勾。
现在失败清单下面直接给两个按钮：**只重试这 N 个**、**导出清单 CSV**。
"""
import os
import shutil
import subprocess
import sys
import tempfile
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-stepretry")
ROOT = os.path.join(TMP, "导出项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8911
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
    repo = Repo(ROOT).init("导出")
    with open(os.path.join(ROOT, "底板.sldprt"), "w", encoding="utf-8") as f:
        f.write("x")
    repo.commit("第一版", "测试")


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
            pass
        import time
        time.sleep(0.25)
    raise RuntimeError("服务没起来")


def run_ui():
    """这个功能全在前端：把导出接口换成剧本，就能稳定复现「部分失败」"""
    from playwright.sync_api import sync_playwright
    errors = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    job = {"id": "j1", "state": "running", "phase": "导出", "done": 1, "total": 2,
           "detail": "", "error": "", "elapsed": 1.0}
    done = dict(job, state="done", done=2, phase="完成", result={
        "ok": [{"rel": "底板.sldprt", "out": "C:/out/底板.step"}],
        "failed": [{"rel": "侧板.sldprt", "out": "", "error": "引用丢失（缺子零件）"}],
        "dest": "C:/out", "files": 1, "seconds": 1.2,
    })
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)

        page.route("**/api/export/step**", lambda r: r.fulfill(
            json={"ok": True, "job": job}) if "status" not in r.request.url
            else r.fulfill(json={"ok": True, "job": done}))

        page.evaluate("() => exportStep(['底板.sldprt', '侧板.sldprt'])")
        page.wait_for_selector(".modal .go", timeout=8000)
        page.fill(".modal .dest", os.path.join(TMP, "out"))
        page.click(".modal .go")
        page.wait_for_selector(".step-fail .sf-act", timeout=30000)
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "1-fail.png"))
        info = page.evaluate("""() => ({
          title: (document.querySelector('.step-fail .sf-title') || {}).textContent || '',
          retry: !!(document.querySelector('.step-fail .sf-retry')),
          csv: !!(document.querySelector('.step-fail .sf-csv')),
          retryText: (document.querySelector('.step-fail .sf-retry') || {}).textContent || '',
        })""")
        check("没导成" in info["title"], "说清有几个没导成", info["title"])
        check(info["retry"], "有「只重试这 N 个」按钮")
        check("1" in info["retryText"], "按钮上写明要重试几个", info["retryText"])
        check(info["csv"], "有「导出清单 CSV」按钮")

        with page.expect_download() as dl:
            page.click(".step-fail .sf-csv")
        d = dl.value
        path = d.path()
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        lines = text.splitlines()
        check(d.suggested_filename.endswith(".csv"), "下载的是 csv 文件", d.suggested_filename)
        check(lines[0].strip() == "零件,结果,输出文件,说明", "表头是四列", lines[0])
        check(any("成功" in x and "底板.sldprt" in x for x in lines), "成功项也在清单里")
        check(any("失败" in x and "侧板.sldprt" in x and "引用丢失" in x for x in lines),
              "失败项带原因", " | ".join(lines[1:]))
        shutil.copyfile(path, os.path.join(SHOTS, "2-list.csv"))

        # 点「只重试」要二次确认，且只把失败的那几个带回去
        page.once("dialog", lambda dlg: dlg.accept())
        page.click(".step-fail .sf-retry")
        page.wait_for_timeout(800)
        reopened = page.evaluate("() => (document.querySelector('.modal .ci-name') || {}).textContent || ''")
        check("1 个零件导出成 STEP" in reopened, "重试只带失败的那一个", reopened)
        page.screenshot(path=os.path.join(SHOTS, "3-retry.png"))
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
