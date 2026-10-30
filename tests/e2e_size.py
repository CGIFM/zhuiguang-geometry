"""项目体积面板（真实浏览器验证）

用法：python -I tests/e2e_size.py   （需要 playwright + chromium）

点「📦 体积」要能说清楚：图纸占多少、历史占多少、回收站占多少，
而且要能把人领到「清理」上去——否则这个面板只是好看。
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

from zgr.store import Repo                  # noqa: E402

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-size")
ROOT = os.path.join(TMP, "体积项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8910
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
    repo = Repo(ROOT).init("体积")
    for i in range(6):
        with open(os.path.join(ROOT, "件%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("x" * 4000)
    repo.commit("第一版", "测试")
    time.sleep(1.1)
    for i in range(6):
        with open(os.path.join(ROOT, "件%d.sldprt" % i), "w", encoding="utf-8") as f:
            f.write("y" * 4000)
    repo.commit("第二版", "测试")
    repo.put("blob", b"z" * 3000)          # 一个没人引用的对象
    # 造点预览缓存（缩略图 / DWG 预览），验证它单独一行、还能一键清掉
    pv = os.path.join(ROOT, ".zgr", "previews", "aa")
    os.makedirs(pv, exist_ok=True)
    with open(os.path.join(pv, "deadbeef-512.png"), "wb") as f:
        f.write(b"P" * 5000)
    # 网格缓存：一个 3 MB 的「死」网格，外加把上限压到 1 MB，
    # 这样面板上才会出现「清理缓存到上限」——这条路径平时根本看不见
    ms = os.path.join(ROOT, ".zgr", "meshes")
    os.makedirs(ms, exist_ok=True)
    mp = os.path.join(ms, "a" * 64 + ".zgm")
    with open(mp, "wb") as f:
        f.write(b"M" * (3 << 20))
    # 伪装成「一小时前生成的」：10 分钟内刚生成的文件按策略不许删
    old_t = time.time() - 3600
    os.utime(mp, (old_t, old_t))
    cfg = repo.config
    cfg.update({"cache_max_mb": 1, "mesh_max_mb": 1})
    repo._write_json("config.json", cfg)


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
    url = BASE + "/#p=" + urllib.parse.quote(ROOT)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.click("#btnSize")
        page.wait_for_selector(".size-table", timeout=20000)
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "1-size.png"))
        info = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('.size-table tr'))
            .map(tr => [tr.querySelector('th').textContent,
                        tr.querySelector('td b').textContent]);
          const bars = Array.from(document.querySelectorAll('.size-table td.pct span'))
            .map(s => s.style.width);
          return {rows: Object.fromEntries(rows), bars,
                  text: (document.querySelector('.modal') || {}).innerText || ''};
        }""")
        print("  体积：%s" % info["rows"])
        for k in ("图纸文件", "版本历史", "回收站", "BOM 快照"):
            check(k in info["rows"], "列出「%s」" % k)
        check("合计" in info["text"], "给出合计")
        check(len(info["bars"]) >= 5, "每项都有占比条", str(len(info["bars"])))
        check("清理" in info["text"], "指路人去「清理」", "")
        check("预览缓存" in info["rows"] and info["rows"]["预览缓存"] != "0 B",
              "预览缓存单独列出来", str(info["rows"].get("预览缓存")))

        # 一键清掉预览缓存：图纸和历史一个字节都不能少
        before = page.evaluate("""() => document.querySelector('.modal-foot .clean-pv')
          .textContent""")
        check("5.0 KB" in before or "KB" in before, "按钮上写着能清多少", before)
        page.on("dialog", lambda d: d.accept())
        page.click(".modal-foot .clean-pv")
        page.wait_for_timeout(2500)
        after = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('.size-table tr'))
            .map(tr => [tr.querySelector('th').textContent,
                        tr.querySelector('td b').textContent]);
          return Object.fromEntries(rows);
        }""")
        check(after.get("预览缓存") == "0 B", "预览缓存清成了 0", str(after.get("预览缓存")))
        check(after.get("图纸文件") == info["rows"]["图纸文件"], "图纸文件没被动",
              str(after.get("图纸文件")))
        check(after.get("版本历史") == info["rows"]["版本历史"], "版本历史没被动",
              str(after.get("版本历史")))
        # 网格缓存单独一行，并写明「超过 1 MB 会自动清最旧的」
        mesh_txt = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('.size-table tr'));
          const r = rows.find(tr => tr.querySelector('th').textContent.includes('网格缓存'));
          return r ? r.innerText : '';
        }""")
        check("网格缓存" in mesh_txt, "列出「网格缓存」",
              " ".join(mesh_txt.splitlines())[:40])
        check("3.0 MB" in mesh_txt, "网格缓存体积正确",
              " ".join(mesh_txt.splitlines())[:40])
        check("超过 1 MB 会自动清最旧的" in info["text"], "写明自动清理策略")

        # 超上限时「清理缓存到上限」才出现，点了要真的删到上限以内
        shown = page.evaluate("""() => {
          const b = document.querySelector('.modal-foot .clean-cache');
          return b ? !b.hidden : false;
        }""")
        check(shown, "超上限时出现「清理缓存到上限」")
        page.screenshot(path=os.path.join(SHOTS, "2-over.png"))
        page.on("dialog", lambda d: d.accept())
        page.click(".modal-foot .clean-cache")
        page.wait_for_timeout(2500)
        mesh_after = page.evaluate("""() => {
          const rows = Array.from(document.querySelectorAll('.size-table tr'));
          const r = rows.find(tr => tr.querySelector('th').textContent.includes('网格缓存'));
          return r ? r.querySelector('td b').textContent : '';
        }""")
        check(mesh_after in ("0 B",), "网格缓存被清到上限以内", mesh_after)
        check(os.path.isfile(os.path.join(ROOT, "件0.sldprt")), "图纸文件还在")
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
