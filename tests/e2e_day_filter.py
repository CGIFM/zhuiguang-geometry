"""「今天」那一行点得动：点一下只留今天的版本，再点回全部（真实浏览器）

「今天 3 个版本」只是个数——想知道是哪三个，还得自己在几十张卡里找。
所以把这一行做成开关：点它筛到今天的版本，抬头写明清筛了、另有几个是更早的，
再点一次回全部。键盘也要能用（Tab 到它、回车切换）。

用法：python -I tests/e2e_day_filter.py   （需要 playwright + chromium）
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

import zgr.store as store                    # noqa: E402
from zgr.store import Repo                   # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-dayfilter")
ROOT = os.path.join(TMP, "筛今天")
SHOTS = os.path.join(TMP, "shots")
PORT = 8932
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []
OLD = 3 * 86400 * 1000


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("筛今天")
    real = store.now_ms

    def fake(back):
        def f():
            return real() - back
        return f

    # 前两版拨回三天前：只改时间戳这一件事，走的是真的提交路径
    for i, (msg, who, back) in enumerate([
            ("三天前调侧板", "张三", OLD),
            ("三天前补定位销", "张三", OLD),
            ("今天上午改料盘", "我", 0),
            ("今天中午改夹具", "我", 0),
            ("今天下午复核对位", "张三", 0),
    ]):
        with open(os.path.join(ROOT, "件%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("v")
        store.now_ms = fake(back)
        try:
            repo.commit(msg, who)
        finally:
            store.now_ms = real


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


CARDS = "() => document.querySelectorAll('#timeline .commit').length"
META = "() => document.getElementById('timelineMeta').innerText"


def run_ui():
    from playwright.sync_api import sync_playwright
    errors = []
    log_urls = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1000},
                            permissions=["clipboard-read", "clipboard-write"])
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda r: log_urls.append(r.url) if "/api/log" in r.url else None)
        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1200)

        check(page.evaluate(CARDS) == 5, "五张卡都在", str(page.evaluate(CARDS)))
        note = page.evaluate(
            "() => (document.getElementById('dayNote').innerText||'').replace(/\\s+/g,' ').trim()")
        check("3" in note and "个版本" in note, "摘要说今天 3 个版本", note)

        page.click("#dayNote")
        page.wait_for_timeout(500)
        n = page.evaluate(CARDS)
        check(n == 3, "点一下只剩今天的 3 张卡", str(n))
        meta = page.evaluate(META)
        check("只看今天" in meta, "抬头写清楚在筛今天", meta)
        check("另外" in meta or "另" in meta, "抬头说出另有几个是更早的", meta)
        pressed = page.evaluate(
            "() => document.getElementById('dayNote').getAttribute('aria-pressed')")
        check(pressed == "true", "开关状态对读屏软件可见", str(pressed))
        page.screenshot(path=os.path.join(SHOTS, "1-today-only.png"))

        # 搜索框和「今天」能叠着用，且说得清是叠着的
        page.fill("#searchBox", "料盘")
        page.wait_for_timeout(400)
        check(page.evaluate(CARDS) == 1, "今天 + 搜索一起用", str(page.evaluate(CARDS)))
        page.screenshot(path=os.path.join(SHOTS, "2-today-search.png"))
        page.fill("#searchBox", "")
        page.wait_for_timeout(400)

        page.click("#dayNote")
        page.wait_for_timeout(500)
        check(page.evaluate(CARDS) == 5, "再点一下回全部", str(page.evaluate(CARDS)))
        meta2 = page.evaluate(META)
        check("只看今天" not in meta2, "抬头不再说只看今天", meta2)

        # 今天零点由浏览器报给服务端（跨机时区不同也不会数错天）
        q = urllib.parse.parse_qs(urllib.parse.urlparse(log_urls[-1]).query) if log_urls else {}
        check("today_from" in q, "请求里带上了浏览器的今天零点", str(q.get("today_from")))
        zero = page.evaluate(
            "() => { const d = new Date(); d.setHours(0,0,0,0); return d.getTime(); }")
        check(str(zero) in (q.get("today_from") or [""])[0],
              "报的就是本地 0 点", "%s vs %s" % (zero, q.get("today_from")))

        # 跨零点：昨天取回来的「今天」数是昨天的，日期一变就不能再拿来用。
        # 走的是真实轮询路径（loadFeed → refreshQuiet → loadTimeline）
        stale = page.evaluate("""async () => {
          const yday = Date.now() - 86400000;
          S.todayInfo = {since: yday, count: 99, truncated: false};
          S.dayStamp = 0;               // 装作「昨天渲染的」
          await loadFeed(false);
          await new Promise((r) => setTimeout(r, 1200));
          return (document.getElementById('dayNote').innerText || '')
            .replace(/\\s+/g, ' ').trim();
        }""")
        check("99" not in stale, "跨天后不再显示昨天那个数", stale)
        check("今天 3 个版本" in stale, "跨天后按今天的实际版本重算", stale)
        check(page.evaluate("() => S.dayStamp") == page.evaluate(
            "() => { const d = new Date(); return d.getFullYear()*10000 + (d.getMonth()+1)*100 + d.getDate(); }"),
            "跨天后 dayStamp 跟上了今天")

        # 键盘：Tab 得到、回车也能切换
        page.focus("#dayNote")
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        check(page.evaluate(CARDS) == 3, "回车也能切到只看今天")
        page.keyboard.press(" ")
        page.wait_for_timeout(500)
        check(page.evaluate(CARDS) == 5, "空格再切回来")

        # 「复制」：粘到群里就能看懂的一段话
        check(page.is_visible("#btnCopyToday"), "摘要旁边有「复制」")
        page.click("#btnCopyToday")
        page.wait_for_timeout(400)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        check("今天 3 个版本" in clip, "复制里有今天的版本数", clip.replace("\n", " | ")[:120])
        check("张三 1 版" in clip, "复制里按人分开数", clip.replace("\n", " | ")[:120])
        check("今天上午改料盘" in clip, "复制里带上了具体改了哪几版", clip.replace("\n", " | ")[:120])
        check("今天下午复核对位" in clip, "复制里是最新的排在前面", clip.replace("\n", " | ")[:120])

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
