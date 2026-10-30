"""把「这一版改了什么」导成 CSV（真实浏览器验证）

用法：python -I tests/e2e_change_csv.py   （需要 playwright + chromium）

评审会、回复客户、交接班，都需要一张「这次改了哪几个文件」的表——
界面上有改动清单，但截图发出去不能筛选、不能统计。
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

TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-chgcsv")
ROOT = os.path.join(TMP, "改动项目")
SHOTS = os.path.join(TMP, "shots")
PORT = 8912
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def write(rel, text):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p) or ROOT, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT)
    os.makedirs(SHOTS)
    repo = Repo(ROOT).init("改动清单")
    write("底板.sldprt", "一")
    write("侧板.sldprt", "一二三")
    write("说明.txt", "旧")
    write("=sum.sldprt", "一")      # 名字长得像公式：CSV 里必须当文本
    repo.commit("第一版", "测试")
    time.sleep(1.1)
    write("侧板.sldprt", "一二三四五")          # 改大
    write("新零件.sldprt", "新")                # 新增
    write("=sum.sldprt", "一二")                # 改了一个「像公式」的文件名
    os.remove(os.path.join(ROOT, "说明.txt"))   # 删除
    repo.commit("第二版：加厚侧板，去掉说明", "测试")


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
        ctx = b.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#timeline .commit", timeout=15000)
        page.click("#timeline .commit")            # 最新一版（第二版）
        page.wait_for_selector(".act-csv", timeout=8000)
        page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(SHOTS, "1-detail.png"))
        with page.expect_download() as dl:
            page.click(".act-csv")
        d = dl.value
        path = d.path()
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        lines = text.splitlines()
        shutil.copyfile(path, os.path.join(SHOTS, "2-changes.csv"))
        check(d.suggested_filename.startswith("改动清单-") and d.suggested_filename.endswith(".csv"),
              "下载的是「改动清单-<版本>.csv」", d.suggested_filename)
        check(lines[-1].split(",")[0] == "文件路径" or "文件路径" in text, "有表头")
        body = [x for x in lines if x and not x.startswith(("版本,", "说明,", "记录人,",
                                                           "时间,", "统计,", "文件路径,"))]
        joined = "\n".join(body)
        check("新零件.sldprt,新增" in joined, "新增的文件在表里", joined[:120])
        check("侧板.sldprt,修改" in joined, "改过的文件在表里")
        check("说明.txt,删除" in joined, "删掉的文件在表里")
        check("底板.sldprt" not in joined, "没改的文件不占篇幅")
        check("侧板.sldprt,修改,9,15,6" in joined, "写了大小变化（旧/新/差异）")
        check("说明.txt,删除,3,,-3" in joined, "删掉的文件差异是负数")
        # 公式注入：`=sum.sldprt` 这种名字双击打开就会让 Excel 去算/去执行，
        # 导出的表里必须是文本（前面加一个单引号），不能原样落进首列
        check("'=sum.sldprt" in joined, "像公式的文件名被当文本", joined[:150])
        check("=sum.sldprt" not in joined.split("'")[0], "没有未转义的公式开头")
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
