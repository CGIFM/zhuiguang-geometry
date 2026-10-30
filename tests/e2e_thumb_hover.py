"""缩略图悬停放大：切零件时不能显示成上一个零件的图（真实浏览器验证）

用法：python -I tests/e2e_thumb_hover.py   （需要 playwright + chromium）

为什么单独有这个用例：
  列表里的 128 缩略图是提前缓存的，而悬停放大要 512 —— 服务端要现渲（大零件几秒），
  浏览器在换 <img> 的 src 时会**继续显示旧图**直到新图解码完。
  结果就是：不管悬停到哪个零件，框里一直停在上一个零件的图上（167 上实测到的 bug）。
  所以这里造三个「只有 128、512 要现渲」的零件，逐条盯：

  1. 悬停 A，等它的 512 出来 → 框里是 A
  2. 立刻移到 B（半秒内）→ 框里不能还是 A（底层垫图必须是 B 的小图）
  3. 再过几秒 → B 的 512 自己到位
  4. 很快划过多个零件 → 不会显示别人；全程没有 JS 报错
  5. 服务端：渲好一张 512 会顺手把小尺寸也存下来，不用再渲一遍
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from zgr import thumb3d                     # noqa: E402
from zgr.store import Repo                  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-hover")
ROOT = os.path.join(TMP, "悬停项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8904
BASE = "http://127.0.0.1:%d" % PORT
NAMES = ["A-示例项目.SLDPRT", "B-料盘.SLDPRT", "C-导轨.SLDPRT"]
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def big_mesh(seed, n=200):
    """够大的网格：512 渲一次要一两秒，这样「等大图」的窗口是真实存在的"""
    import array
    verts = array.array("f")
    tris = array.array("I")
    for i in range(n + 1):
        for j in range(n + 1):
            z = ((i * j + seed * 13) % 41) * (0.15 + 0.02 * seed)
            verts.extend((i * 0.4, j * 0.4, z))
    for i in range(n):
        for j in range(n):
            a = i * (n + 1) + j
            b, c, d = a + 1, a + n + 1, a + n + 2
            tris.extend((a, b, d))
            tris.extend((a, d, c))
    return b"ZGM1" + struct.pack("<II", (n + 1) ** 2, len(tris) // 3) + verts.tobytes() + tris.tobytes()


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("悬停测试")
    for i, n in enumerate(NAMES):
        with open(os.path.join(ROOT, n), "wb") as f:
            f.write(("part %d %s" % (i, "x" * 500)).encode())
    head = repo.commit("三个零件", "测试")
    _c, files = repo.files_of(head)

    # 只预置 128（等同真机 warm-sw 的成果），512 留给服务端现渲
    for i, n in enumerate(NAMES):
        oid = files[n]["oid"]
        zgm = big_mesh(i + 1)
        md = os.path.join(repo.meta, "meshes")
        os.makedirs(md, exist_ok=True)
        with open(os.path.join(md, oid + ".zgm"), "wb") as f:
            f.write(zgm)
        pd = os.path.join(repo.meta, "previews", oid[:2])
        os.makedirs(pd, exist_ok=True)
        with open(os.path.join(pd, "%s-128.png" % oid), "wb") as f:
            f.write(thumb3d.render_zgm(zgm, 128))
    return repo, [(n, files[n]["oid"]) for n in NAMES]


def serve():
    env = dict(os.environ)
    env["APPDATA"] = os.path.join(TMP, "appdata")
    os.makedirs(env["APPDATA"], exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-I", "zgr.py", "serve", "--port", str(PORT),
                             "--host", "127.0.0.1"], cwd=APP, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(BASE + "/api/ping", timeout=1).read()
            return proc
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("服务没起来")


STATE_JS = """() => {
  const box = document.getElementById('thumbZoom');
  const im = box && box.querySelector('img');
  const ph = box && box.querySelector('.tz-ph');
  const bg = ph ? (ph.style.backgroundImage || '') : '';
  const m = /oid=([0-9a-f]{64})/.exec(bg);
  return {
    show: box ? box.classList.contains('show') : null,
    bigSrc: im ? (im.getAttribute('src') || '') : '',
    bigShown: im ? im.classList.contains('ready') : null,
    bigW: im ? im.naturalWidth : -1,
    phOid: m ? m[1] : '',
    busy: box ? box.classList.contains('busy') : null,
    nofull: box ? box.classList.contains('nofull') : null,
    name: box ? box.querySelector('.tz-name').textContent : '',
  };
}"""


def box_clip(page):
    box = page.query_selector("#thumbZoom")
    r = box.bounding_box() if box else None
    if not r:
        return {"x": 0, "y": 0, "width": 340, "height": 340}
    return {"x": max(0, r["x"]), "y": max(0, r["y"]),
            "width": min(340, r["width"]), "height": min(340, r["height"])}


def run_ui(oids):
    from playwright.sync_api import sync_playwright
    errors = []
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(url)
        page.wait_for_selector(".commit", timeout=30000)
        page.click(".commit")
        page.wait_for_selector(".frow", timeout=30000)
        page.wait_for_timeout(2500)          # 等 128 小图加载完

        rows = page.query_selector_all(".frow")
        check(len(rows) == len(NAMES), "文件列表列出了 3 个零件", "实际 %d" % len(rows))
        oidA, oidB, oidC = (o for _n, o in oids)

        def hover(i):
            rows[i].query_selector(".fthumb").hover()

        def wait_big(oid, timeout=40):
            t0 = time.time()
            while time.time() - t0 < timeout:
                st = page.evaluate(STATE_JS)
                if st["bigShown"] and oid in st["bigSrc"] and st["bigW"] > 0:
                    return time.time() - t0, st
                page.wait_for_timeout(150)
            return None, page.evaluate(STATE_JS)

        # ① 悬停 A，等 A 的 512
        hover(0)
        sec, st = wait_big(oidA)
        check(sec is not None, "A 的 512 大图生成出来了", "%.1fs" % (sec or -1))
        page.wait_for_timeout(200)
        shotA = page.screenshot(path=os.path.join(SHOTS, "A.png"), clip=box_clip(page))
        page.mouse.move(4, 4)
        page.wait_for_timeout(250)

        # ② 移到 B：半秒内必须已经换成 B 的小图，且不能还挂着 A 的大图
        hover(1)
        page.wait_for_timeout(400)
        st = page.evaluate(STATE_JS)
        check(st["phOid"] == oidB, "框里立刻垫上的是 B 自己的小图")
        check(not (oidA in st["bigSrc"]) and not st["bigShown"],
              "换到 B 之后 A 的大图不会留在框里",
              "bigSrc=%s ready=%s" % (st["bigSrc"][-40:], st["bigShown"]))
        shotB = page.screenshot(path=os.path.join(SHOTS, "B-0.4s.png"), clip=box_clip(page))
        check(shotB != shotA, "截图内容确实变了（不是同一个零件的图）")

        # ③ B 的 512 自己到位
        sec, st = wait_big(oidB)
        check(sec is not None, "B 的 512 大图随后也到位", "%.1fs" % (sec or -1))
        page.screenshot(path=os.path.join(SHOTS, "B-done.png"), clip=box_clip(page))
        page.mouse.move(4, 4)
        page.wait_for_timeout(200)

        # ④ 快速划过三个：每次框里垫底的都是当前这一行，不是别人
        wrong = []
        for i, oid in enumerate((oidA, oidB, oidC)):
            hover(i)
            page.wait_for_timeout(320)
            st = page.evaluate(STATE_JS)
            if st["phOid"] != oid:
                wrong.append(i)
        check(not wrong, "快速切换时框里始终是当前零件", "错的行：%s" % wrong)

        check(not errors, "没有 JS 报错", " / ".join(errors[:3]))
        b.close()
    return errors


def check_server_derive(oids):
    """服务端：渲一次 512 就把小尺寸也存下来，再要就直接命中，不用重渲"""
    repo = Repo(ROOT)
    for n, oid in oids:                     # 只查悬停时真的等到了 512 的那两个
        for size in (128, 256, 512):
            p = os.path.join(repo.meta, "previews", oid[:2], "%s-%d.png" % (oid, size))
            check(os.path.isfile(p), "%s 有 %d 缓存" % (n, size))
        url = BASE + "/api/preview?" + urllib.parse.urlencode(
            {"path": ROOT, "oid": oid, "name": n, "size": 256})
        urllib.request.urlopen(url, timeout=30).read()      # 第一次含连接开销
        t0 = time.time()
        urllib.request.urlopen(url, timeout=30).read()
        check(time.time() - t0 < 0.5, "%s 的 256 走缓存秒回" % n,
              "%.2fs" % (time.time() - t0))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    repo, oids = build()
    proc = serve()
    try:
        run_ui(oids)
        check_server_derive(oids[:2])
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n截图：%s" % SHOTS)
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
