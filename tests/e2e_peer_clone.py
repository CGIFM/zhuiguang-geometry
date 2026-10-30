"""对方机器上的项目，左侧直接看到、一键拉过来（真实浏览器验证）

用法：python -I tests/e2e_peer_clone.py   （需要 playwright + chromium）

两台「机器」各起一个服务：
  A（先开）有项目「活塞支架」，已经有两版历史
  B（后开）本机只有一个别的项目
在 B 的网页上：
  1. 左侧出现「对方机器上的项目」，名字/文件数/最后记录都对
  2. 点它 → 弹确认框，写明来自哪台、多大、最新一版是谁记的
  3. 「浏览…」选父文件夹 → 自动拼成 <父文件夹>\\活塞支架，可以再改
  4. 点「开始拉取」→ 进度条真的在走 → 完成后左侧出现这个项目并自动选中
  5. 磁盘上的文件、分支指针都对得上

局域网扫描在这台测试机上找不到监听 127.0.0.1 的对方，所以只把
「问对方有哪些项目」这一个 GET 改成带 url 的真实请求（服务端逻辑照跑）。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-peer")
SRC = os.path.join(TMP, "A", "活塞支架")
BSIDE = os.path.join(TMP, "B", "连杆夹具")
SHOTS = os.path.join(TMP, "shots")
PORT_A = 8915
PORT_B = 8916
A = "http://127.0.0.1:%d" % PORT_A
B = "http://127.0.0.1:%d" % PORT_B
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def http(url, body=None):
    if body is None:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def write(root, name, text):
    p = os.path.join(root, *name.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(SHOTS)
    repo = Repo(SRC).init("活塞支架")
    write(SRC, "总装.sldasm", "版本一")
    write(SRC, "零件/活塞.sldprt", "活塞 v1")
    repo.commit("起点", "测试")
    write(SRC, "零件/活塞.sldprt", "活塞 v2（加倒角）")
    repo.commit("活塞加倒角", "测试")

    other = Repo(BSIDE).init("连杆夹具")
    write(BSIDE, "夹具.sldprt", "本机原有的项目")
    other.commit("本机起点", "测试")


def serve(port, name):
    import socket
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))          # 端口被上次残留的服务占着就直说，
    except OSError:                              # 不然会连到旧进程、看到旧界面
        raise RuntimeError("端口 %d 已被占用，先关掉残留的测试服务" % port) from None
    finally:
        probe.close()
    env = dict(os.environ)
    env["APPDATA"] = os.path.join(TMP, name, "appdata")
    os.makedirs(env["APPDATA"], exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-I", "zgr.py", "serve", "--port", str(port),
                             "--host", "127.0.0.1"], cwd=APP, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(120):
        try:
            urllib.request.urlopen(base + "/api/ping", timeout=1).read()
            return proc
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("服务没起来：%d" % port)


def main():
    build()
    pa = serve(PORT_A, "A")
    pb = serve(PORT_B, "B")
    errors = []
    try:
        http(A + "/api/add", {"target": SRC})
        http(B + "/api/add", {"target": BSIDE})

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1440, "height": 940})
            pg.on("pageerror", lambda e: errors.append(str(e)))

            # 局域网里找不到 127.0.0.1 的对方：把「问对方有哪些项目」改成带 url 的真请求
            def on_peers(route):
                u = route.request.url
                if "url=" not in u:
                    u += ("&" if "?" in u else "?") + "url=" + urllib.parse.quote(A, safe="")
                try:
                    with urllib.request.urlopen(u, timeout=20) as r:
                        data = r.read()
                    route.fulfill(status=200, content_type="application/json", body=data)
                except Exception as e:                             # noqa: BLE001
                    route.fulfill(status=500, content_type="application/json",
                                  body=json.dumps({"ok": False, "error": str(e)}))

            pg.route("**/api/peer/projects*", on_peers)

            parent = os.path.join(TMP, "B", "下载")
            os.makedirs(parent)

            def on_pick(route):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "paths": [parent],
                                               "canceled": False, "kind": "folder"}))
            pg.route("**/api/pick", on_pick)

            pg.goto(B + "/#p=" + urllib.parse.quote(BSIDE))
            pg.wait_for_selector("#timeline .commit", timeout=20000)

            # ---- 1. 左侧看到对方的新项目 ----
            pg.wait_for_selector("#peerBox:not([hidden]) .proj.peer", timeout=15000)
            name = pg.inner_text("#peerList .proj.peer .proj-name").strip()
            sub = pg.inner_text("#peerList .proj.peer .proj-sub")
            check(name == "活塞支架", "左侧出现对方的新项目", name)
            check("来自" in sub and "文件" in sub, "写着来自哪台、多少个文件", sub)
            check(pg.evaluate("document.querySelectorAll('#projList .proj').length") == 1,
                  "本机项目列表没有被混进去")
            pg.screenshot(path=os.path.join(SHOTS, "1-peer-list.png"))

            # ---- 2. 点开确认框 ----
            pg.click("#peerList .proj.peer")
            pg.wait_for_selector(".modal .clone-info", timeout=5000)
            info = pg.inner_text(".modal .clone-info")
            check("活塞支架" in info and "mian" not in info, "确认框写明项目信息", info)
            check("2" in info, "写着文件数/大小", info)
            pg.screenshot(path=os.path.join(SHOTS, "2-clone-dialog.png"))

            # ---- 3. 浏览 → 自动拼出 <父文件夹>\\活塞支架 ----
            pg.click(".modal .browse")
            pg.wait_for_timeout(600)
            dest = pg.input_value(".modal .dest")
            check(dest.rstrip("\\/").endswith("活塞支架"), "自动拼上项目名", dest)
            check(os.path.dirname(dest.rstrip("\\/")) == parent, "父文件夹是刚才选的", dest)

            # ---- 4. 开始拉取 → 进度 → 完成 ----
            pg.click(".modal .go")
            pg.wait_for_selector(".modal .cp:not([hidden])", timeout=5000)
            done = False
            for _ in range(200):
                pg.wait_for_timeout(150)
                txt = pg.inner_text(".modal .cp-text") if pg.query_selector(".modal .cp-text") else ""
                if "出错" in txt:
                    check(False, "拉取过程没有出错", txt)
                    break
                if pg.query_selector(".modal") is None or pg.inner_text(".modal .go") == "完成":
                    done = True
                    break
            check(done, "拉取结束")
            pg.wait_for_timeout(1200)

            # ---- 5. 结果核对 ----
            got = os.path.join(parent, "活塞支架")
            check(os.path.isfile(os.path.join(got, "总装.sldasm")), "文件铺到了本地", got)
            with open(os.path.join(got, "零件", "活塞.sldprt"), encoding="utf-8") as f:
                check(f.read() == "活塞 v2（加倒角）", "拿到的是对方最新一版")
            src_head = Repo(SRC).head_oid("main")
            check(Repo(got).head_oid("main") == src_head, "分支指针和对方一致")
            check(not os.path.exists(os.path.join(got, ".zgr", "PULL_NEEDED")),
                  "没有残留「待更新」标记")

            names = pg.inner_text("#projList")
            check("活塞支架" in names and "连杆夹具" in names, "左侧栏两个项目都在", names)
            listed = http(B + "/api/projects")["projects"]
            mine = [p for p in listed if p["name"] == "活塞支架"]
            check(len(mine) == 1 and mine[0]["path"] == got, "已登记成本机项目",
                  json.dumps(mine, ensure_ascii=False)[:200])
            check(bool(mine and mine[0].get("live")), "拉完自动开了实时同步")
            check(pg.query_selector("#peerList .proj.peer") is None, "对方那个已经不在「没有的项目」里了")
            pg.screenshot(path=os.path.join(SHOTS, "3-cloned.png"))
            b.close()
        check(not errors, "没有 JS 报错", "; ".join(errors[:3]))
    finally:
        for p in (pb, pa):
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                p.kill()
    print("\n截图：%s" % SHOTS)
    print(("全部通过" if not FAILS else "失败 %d 项：%s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
