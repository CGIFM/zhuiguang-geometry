"""界面上的四处「看着像坏了」（真实浏览器）

都是把界面真跑起来截图才发现的，光看代码看不出来：

1. 局域网里没有对方机器时，左侧「对方机器上的项目」这一块**还会留一个空表头**
   ——CSS 里 `.peer-box{display:flex}` 盖掉了浏览器对 `[hidden]` 的 `display:none`，
   于是 `hidden` 设了也没用，用户看到一条虚线上方什么都没有。
2. 时间线上第一版（根提交）写「改动 6 +6 ~0 −0」：两个零纯属噪音，
   写全了反而看不出重点。
3. 缩略图要不到时（模型导不出、文件删了），卡片位置**空出一大块灰底**，
   像加载失败，而且重试 4 次要 9 秒才肯放弃。
4. 同上，有扩展名牌可显示的地方不该死等那么久。

用法：python -I tests/e2e_ui_clean.py   （需要 playwright + chromium）
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-uiclean")
ROOT = os.path.join(TMP, "界面整洁")
SHOTS = os.path.join(TMP, "shots")
PORT = 8927
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
    repo = Repo(ROOT).init("界面整洁")
    for i in range(6):
        with open(os.path.join(ROOT, "侧板%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("v%d" % i)
    repo.commit("第一批零件", "我")          # 根提交：只有新增
    # 留一个有未记录改动的新文件，好让「未记录变更」里有卡片
    with open(os.path.join(ROOT, "新底板.sldasm"), "w", encoding="utf-8") as f:
        f.write("asm")


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

        # 局域网里就是没有对方：接口立刻回空，别让界面一直转
        page.route("**/api/peer/projects", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"ok": True, "projects": [], "peers": []})))

        page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.wait_for_timeout(1500)

        # 1. 没有对方项目时，左侧那块不该占着位置
        visible = page.evaluate("""() => {
          const el = document.getElementById('peerBox');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {hidden: el.hidden, h: r.height, display: getComputedStyle(el).display};
        }""")
        check(visible is not None, "左侧有「对方机器上的项目」这一块")
        check(visible and visible["display"] == "none",
              "没有对方机器时整块收起来", str(visible))
        check(visible and visible["h"] == 0, "不再留一个空表头", "高度 %s" % (visible or {}).get("h"))

        # 2. 根提交不该写「~0」「−0」
        cards = page.evaluate("() => [...document.querySelectorAll('#timeline .commit')]"
                              ".map(e => e.innerText)")
        root = [t for t in cards if "第一批零件" in t]
        check(len(root) == 1, "找到那一版", str(len(root)))
        text = (root[0] if root else "").replace("\n", " ")
        check("~0" not in text, "没有「~0」这种噪音", text)
        check("−0" not in text, "没有「−0」这种噪音", text)
        check("+6" in text, "新增数照写", text)

        det = page.evaluate("() => (document.getElementById('detail') || {}).textContent || ''")
        det = " ".join(det.split())
        check("修改 0" not in det, "右侧详情里也不印「修改 0」", det[:60])
        check("删除 0" not in det, "右侧详情里也不印「删除 0」", det[:60])

        # 3. 缩略图要不到时，卡片位置上写扩展名，而不是留一块灰底
        page.click('[data-tab="pending"]')
        page.wait_for_selector("#pendingBody .file-card", timeout=8000)
        # 把缩略图请求全部掐掉：等价于「这台机器导不出这张图」
        seen_requests = []

        def note_req(req):
            if "/api/preview" in req.url:
                seen_requests.append(req.url)

        page.on("request", note_req)
        page.route("**/api/preview*", lambda r: r.abort())
        page.evaluate("""() => {
          document.querySelectorAll('#pendingBody img.lazy').forEach((img) => {
            img.dataset.wired = '1';
            try { thumbEnqueue(img); } catch (e) {}
          });
        }""")
        # 第一次失败 → 退避 0.9 秒再试一次 → 第二次失败才收手
        page.wait_for_timeout(2500)
        state = page.evaluate("""() => {
          const card = document.querySelector('#pendingBody .file-card');
          if (!card) return null;
          const box = card.querySelector('.fc-img');
          const img = box.querySelector('img');
          const span = box.querySelector('.ext-badge');
          return {
            img: !!img,
            imgShown: !!(img && !img.classList.contains('broken')),
            badge: span ? span.textContent.trim() : '',
            text: box.textContent.trim(),
          };
        }""")
        check(state is not None, "待记录里有卡片")
        check(not (state or {}).get("imgShown"), "坏图不再占着那一块", str(state))
        check((state or {}).get("badge") == "SLDASM",
              "改成写扩展名", str((state or {}).get("badge")))
        # 404 不等于「永远没有」：图要留在 DOM 里等下一轮重试，不能被换掉
        check((state or {}).get("img"), "图还留着等重试（不判死刑）", str(state))

        # 而且不是摆着看的：过 20 秒要真的**再去要这一张**
        # （以前这里把 state 留成 done，入队函数见 done 直接返回；
        #  后来「在不在屏幕里」又按被 display:none 的 img 自己量，永远判成不在）
        src = page.evaluate("""() => {
          const img = document.querySelector('#pendingBody .file-card img');
          return img ? (img.dataset.src || '') : '';
        }""")
        # 这张卡的工作区文件没有对象号，用整条查询串认它（含文件名，逐张不同）
        key = (src.split("?", 1)[-1] if "?" in (src or "") else (src or ""))[:300]
        before = len([u for u in seen_requests if key and key in u])
        page.wait_for_timeout(22500)
        after = len([u for u in seen_requests if key and key in u])
        check(after > before, "过一会儿自己会再要这一张", "%d → %d  %s" % (before, after, key))
        page.screenshot(path=os.path.join(SHOTS, "1-pending.png"))

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
