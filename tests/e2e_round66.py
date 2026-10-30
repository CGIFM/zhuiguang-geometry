"""第 66 轮的防呆两件事（真实浏览器 + 接口剧本，不需要真的另一台机器）

1. 记录失败时不要连输入框一起关掉：多数的失败原因是「有文件还在被写入」，
   等它写完再点一次就好 —— 刚敲的那句说明得还在。
2. 「选择性合并」只列 300 行，剩下的按默认规则一起合进去。以前界面上只写
   「还有 N 个，显示全部」，用户根本不知道默认规则是什么。

用法：python -I tests/e2e_round66.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-r66")
ROOT = os.path.join(TMP, "防呆项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8918
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
    repo = Repo(ROOT).init("防呆")
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一")
    repo.commit("第一版", "测试")
    time.sleep(1.1)
    with open(os.path.join(ROOT, "侧板.sldprt"), "w", encoding="utf-8") as f:
        f.write("一二三")


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
    state = {"fail_next": True}

    def snapshot_route(route):
        if state["fail_next"]:
            state["fail_next"] = False
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"ok": False,
                                           "error": "有 1 个文件还在被写入（多半是刚点了保存）：侧板.sldprt。"
                                                    "等它写完再记录"}, ensure_ascii=False))
        else:
            route.continue_()

    def plan_route(route):
        entries = []
        for i in range(400):
            entries.append({"path": "零件/件%03d.sldprt" % i, "status": "theirs_only",
                            "size": 1024, "auto": True, "pick": "theirs",
                            "ours": None, "theirs": {"oid": "0" * 64}})
        d = {"ok": True, "entries": entries, "conflicts": [], "theirs_log": [],
             "counts": {"theirs": 400}, "remote_name": "隔壁机器",
             "ours": "a" * 64, "theirs": "b" * 64}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(d, ensure_ascii=False))

    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(400)

        # ---- 1. 记录失败：窗不关、字还在
        page.route("**/api/snapshot", snapshot_route)
        page.keyboard.press("Control+Enter")
        page.wait_for_selector(".modal .msg", timeout=5000)
        page.fill(".modal .msg", "客户确认的第三版")
        page.click(".modal .go")
        page.wait_for_timeout(1200)
        still = page.evaluate("() => !!document.querySelector('.modal .msg')")
        check(still, "记录失败时弹窗没有关掉")
        val = page.evaluate("() => { const el = document.querySelector('.modal .msg');"
                            " return el ? el.value : ''; }")
        check(val == "客户确认的第三版", "失败的弹窗里说明还在", val)
        btn = page.evaluate("""() => {
          const b = document.querySelector('.modal .go');
          return b ? b.textContent.trim() : '';
        }""")
        check("重试" in btn, "按钮变成「重试记录」", btn)
        err = page.evaluate("""() => {
          const el = document.querySelector('.snap-err');
          return el ? el.textContent.trim() : '';
        }""")
        check("还在被写入" in err, "错误原因写在框里", err[:50])
        page.screenshot(path=os.path.join(SHOTS, "1-snapshot-retry.png"))

        before = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        page.click(".modal .go")                     # 这次不拦，真记录
        page.wait_for_selector(".modal", state="detached", timeout=20000)
        page.wait_for_timeout(1500)
        after = page.evaluate("() => document.querySelectorAll('#timeline .commit').length")
        check(after == before + 1, "重试之后确实记下了一版", "%d → %d" % (before, after))

        # ---- 2. 合并面板：默认规则要说出来
        page.route("**/api/merge/plan", plan_route)
        page.click('button[data-tab="sync"]')
        page.wait_for_selector("#syncUrl", timeout=5000)
        page.fill("#syncUrl", "http://127.0.0.1:9")
        page.click("#btnPlanMerge")
        page.wait_for_selector(".merge-more-note", timeout=10000)
        note = page.evaluate("""() => {
          const el = document.querySelector('.merge-more-note');
          return el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
        }""")
        check("还有 100 个" in note, "说清还有多少个没列出来", note[:60])
        check("保留本机" in note and "采用对方" in note, "写清没列出来的按什么规则合")
        more = page.evaluate("""() => {
          const b = document.querySelector('#mergeBody .more');
          return b ? b.textContent.trim() : '';
        }""")
        check("剩下" in more, "按钮写明显示多少个", more)
        page.screenshot(path=os.path.join(SHOTS, "2-merge-note.png"))
        n1 = page.evaluate("() => document.querySelectorAll('.merge-row').length")
        page.click("#mergeBody .more")
        page.wait_for_timeout(300)
        n2 = page.evaluate("() => document.querySelectorAll('.merge-row').length")
        check(n1 == 300 and n2 == 400, "点「显示全部」后行数从 300 变 400", "%d → %d" % (n1, n2))

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
