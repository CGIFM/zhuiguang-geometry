"""一个版本里几千个零件时，缩略图要「滑到哪才加载哪」（真实浏览器验证）

用法：python -I tests/e2e_lazy_thumbs.py   （需要 playwright + chromium）

以前是每行直接写 src，一打开就是一个版本几百上千个 /api/preview 同时排队，
服务端出图要串行走 SolidWorks，你滑到的那一屏反而排在队尾。
现在改成 IntersectionObserver 按视口附近入队 + 最多 3 个并发。

断言：
  1. 文件行全部铺出来（400 行），但一开始带 src 的缩略图只有屏幕附近那几十个
  2. 往下滚之后，新露出来的行会自己加载（src 数量明显变多）
  3. 滚到底能看到最后一行，且它的缩略图真的出来了
  4. 没有 JS 报错
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from zgr.store import Repo                  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-lazy")
ROOT = os.path.join(TMP, "零件库")
SHOTS = os.path.join(TMP, "shots")
PORT = 8909
BASE = "http://127.0.0.1:%d" % PORT
N = 400
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def png_bytes(w=8, h=8, rgb=(90, 140, 220)):
    """一张极小的合法 PNG（不用 PIL）"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("零件库")
    blob = png_bytes()
    for i in range(N):
        d = os.path.join(ROOT, "零件%02d" % (i // 50))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "零件_%03d.png" % i), "wb") as f:
            f.write(blob)
    return repo.commit("一次导入 %d 个零件" % N, "测试")


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


PROBE = """() => {
  const box = document.querySelector('.filerows');
  const imgs = Array.from(document.querySelectorAll('.filerows img.thumb.lazy'));
  const withSrc = imgs.filter(i => i.getAttribute('src'));
  const loaded = imgs.filter(i => i.dataset.state === 'done');
  const rows = document.querySelectorAll('.filerows .frow');
  const last = rows.length ? rows[rows.length - 1] : null;
  return {
    rows: rows.length,
    imgs: imgs.length,
    src: withSrc.length,
    loaded: loaded.length,
    scrollTop: box ? Math.round(box.scrollTop) : 0,
    scrollH: box ? Math.round(box.scrollHeight) : 0,
    clientH: box ? Math.round(box.clientHeight) : 0,
    lastSrc: last ? (last.querySelector('img.thumb') || {}).src || '' : '',
    lastState: last ? ((last.querySelector('img.thumb') || {}).dataset || {}).state || '' : '',
  };
}"""


def main():
    cid = build()
    proc = serve()
    errors = []
    previews = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: previews.append(r.url)
                    if "/api/preview" in r.url else None)
            page.goto(BASE + "/#p=" + urllib.parse.quote(ROOT))
            page.wait_for_selector("#timeline .commit", timeout=20000)
            page.click("#timeline .commit")
            page.wait_for_selector(".filerows .frow", timeout=20000)
            page.wait_for_timeout(1500)
            st = page.evaluate(PROBE)
            page.screenshot(path=os.path.join(SHOTS, "1-top.png"))
            print("  打开时：%s" % {k: st[k] for k in ("rows", "imgs", "src", "loaded")})
            check(st["rows"] == N, "文件行全部铺出来（%d 行）" % N, "%d 行" % st["rows"])
            check(st["src"] <= 80, "一开始只给屏幕附近发请求，不是 %d 个一起排队" % N,
                  "带 src 的 %d 个" % st["src"])
            check(len(previews) <= 80, "实际发出的 /api/preview 请求也很少", "%d 个" % len(previews))

            # 往下滚：新露出来的行要自己加载
            page.evaluate("""() => {
              const box = document.querySelector('.filerows');
              box.scrollTop = box.scrollHeight;
            }""")
            page.wait_for_timeout(2000)
            st2 = page.evaluate(PROBE)
            page.screenshot(path=os.path.join(SHOTS, "2-bottom.png"))
            print("  滚到底：%s" % {k: st2[k] for k in ("rows", "src", "loaded", "scrollTop",
                                                      "scrollH", "clientH", "lastState")})
            check(st2["scrollTop"] > 100, "列表确实滚下去了", "scrollTop=%s" % st2["scrollTop"])
            check(st2["src"] > st["src"], "滚到的新位置补上了缩略图", "%d → %d" % (st["src"], st2["src"]))
            check(st2["lastState"] in ("done", "loading", "queued"),
                  "最后一行的缩略图已经进队/加载", str(st2["lastState"]))
            page.wait_for_timeout(1500)
            st3 = page.evaluate(PROBE)
            check(st3["lastState"] == "done", "最后一行的缩略图真的加载出来了", str(st3["lastState"]))
            check(st3["src"] < N, "仍然不是全量加载（滑到哪加载哪）",
                  "%d/%d" % (st3["src"], N))
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
