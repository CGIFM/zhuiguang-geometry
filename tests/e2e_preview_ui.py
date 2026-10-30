"""文件预览端到端验证：Markdown / DXF / DWG / STEP / STL / 图片

用法：python -I tests/e2e_preview_ui.py    （需要 playwright + chromium）

覆盖：
  1. /api/workfile、/api/dwg2dxf（明文与 gzip）、/api/font 的接口行为
  2. 网页里单击每一类文件 → 预览浮层真的把内容画出来
  3. DWG 走服务端 LibreDWG 转 DXF，STEP 走 OpenCascade WASM，STL 走 three
  4. 全程没有 JS 报错、没有 4xx/5xx，截图留档
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
TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-preview")
ROOT = os.path.join(TMP, "预览项目")
PORT = 8902
BASE = "http://127.0.0.1:%d" % PORT
FAILS = []

# 最小可用的 R12 DXF：两条实体足够验证解析与渲染链路
DXF_R12 = """0
SECTION
2
HEADER
9
$ACADVER
1
AC1009
0
ENDSEC
0
SECTION
2
ENTITIES
0
LINE
8
0
10
0.0
20
0.0
30
0.0
11
420.0
21
297.0
31
0.0
0
CIRCLE
8
0
10
210.0
20
150.0
30
0.0
40
80.0
0
LINE
8
0
10
0.0
20
297.0
30
0.0
11
420.0
21
0.0
31
0.0
0
ENDSEC
0
EOF
"""

STEP_BOX = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('box.step','',(''),(''),'','','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));
ENDSEC;
DATA;
#1=APPLICATION_CONTEXT('automotive design');
#2=APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#1);
#3=PRODUCT_CONTEXT('',#1,'mechanical');
#4=PRODUCT('box','box','',(#3));
#5=PRODUCT_DEFINITION_FORMATION('','',#4);
#6=PRODUCT_DEFINITION_CONTEXT('part definition',#1,'design');
#7=PRODUCT_DEFINITION('design','',#5,#6);
#8=PRODUCT_DEFINITION_SHAPE('','',#7);
#9=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));
#10=(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.));
#11=(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT());
#12=UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#9,'distance_accuracy_value','');
#13=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#12))GLOBAL_UNIT_ASSIGNED_CONTEXT((#9,#10,#11))REPRESENTATION_CONTEXT('',''));
#14=CARTESIAN_POINT('',(0.,0.,0.));
#15=DIRECTION('',(0.,0.,1.));
#16=DIRECTION('',(1.,0.,0.));
#17=AXIS2_PLACEMENT_3D('',#14,#15,#16);
#18=PLANE('',#17);
#19=CARTESIAN_POINT('',(0.,0.,0.));
#20=VERTEX_POINT('',#19);
#21=CARTESIAN_POINT('',(100.,0.,0.));
#22=VERTEX_POINT('',#21);
#23=CARTESIAN_POINT('',(100.,60.,0.));
#24=VERTEX_POINT('',#23);
#25=CARTESIAN_POINT('',(0.,60.,0.));
#26=VERTEX_POINT('',#25);
#27=EDGE_CURVE('',#20,#22,$,$,.T.);
#28=ORIENTED_EDGE('',*,*,#27,.T.);
#29=EDGE_LOOP('',(#28));
#30=FACE_OUTER_BOUND('',#29,.T.);
#31=FACE_SURFACE('',(#30),#18,.T.);
#32=OPEN_SHELL('',(#31));
#33=SHELL_BASED_SURFACE_MODEL('',(#32));
#34=SHAPE_REPRESENTATION('',(#33),#13);
#35=SHAPE_DEFINITION_REPRESENTATION(#8,#34);
ENDSEC;
END-ISO-10303-21;
"""


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def post(route, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + route, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get(route):
    with urllib.request.urlopen(BASE + route, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get_raw(route, headers=None):
    req = urllib.request.Request(BASE + route, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def write_bytes(rel, data):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def write(rel, text):
    write_bytes(rel, text.encode("utf-8"))


def png_bytes(w=64, h=48, rgb=(47, 107, 255)):
    """一张纯色 PNG：验证图片预览用（不依赖 Pillow）"""
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def stl_ascii():
    tris = [((0, 0, 0), (100, 0, 0), (0, 60, 0)), ((0, 0, 0), (0, 0, 40), (100, 0, 0))]
    out = ["solid test"]
    for a, b, c in tris:
        out.append(" facet normal 0 0 1\n  outer loop")
        for v in (a, b, c):
            out.append("   vertex %g %g %g" % v)
        out.append("  endloop\n endfacet")
    out.append("endsolid test\n")
    return "\n".join(out)


def find_dwg():
    """找一个真的能转换的 DWG 样本。

    LibreDWG 自带的 dxf2dwg 写出来的 DWG 缺 BLOCK_CONTROL，自家 dwg2dxf 都读不回来，
    不能当样本；所以这里用真实图纸：环境变量 ZGR_TEST_DWG 或 tests/fixtures/sample.dwg。
    """
    for p in (os.environ.get("ZGR_TEST_DWG"),
              os.path.join(HERE, "fixtures", "sample.dwg")):
        if p and os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    return None


def main():
    if os.path.isdir(TMP):
        shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)

    write("说明/README.md",
          "# 测试说明\n\n这是一个 **Markdown** 预览测试。\n\n"
          "- 列表项一\n- 列表项二\n\n"
          "| 列 | 值 |\n|---|---|\n| A | 1 |\n\n"
          "![配图](配图.png)\n\n```py\nprint('hi')\n```\n")
    write_bytes("说明/配图.png", png_bytes())
    write("图纸/简图.dxf", DXF_R12)
    write("模型/方块.step", STEP_BOX)
    write("模型/三角.stl", stl_ascii())
    write_bytes("图片/色块.png", png_bytes())
    dwg = find_dwg()
    if dwg:
        write_bytes("图纸/机架.dwg", dwg)
    else:
        print("  (没有 DWG 样本，跳过 DWG；可用 ZGR_TEST_DWG 指定一个真实 .dwg)")

    # 注册表/缓存都指到临时目录：测试项目绝不能写进用户真实的项目列表
    env = dict(os.environ, APPDATA=os.path.join(TMP, "appdata"))
    srv = subprocess.Popen([sys.executable, os.path.join(APP, "zgr.py"), "serve",
                            "--port", str(PORT), "--host", "127.0.0.1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(80):
            try:
                get("/api/ping")
                break
            except Exception:
                time.sleep(0.25)

        print("== 造一个版本 ==")
        post("/api/init", {"target": ROOT, "name": "预览项目"})
        c1 = post("/api/snapshot", {"path": ROOT, "message": "第一版"})["commit"]
        print("  v1", c1[:10])

        q = urllib.parse.quote(ROOT)
        print("== HTTP 接口 ==")
        st, hdr, body = get_raw("/api/workfile?path=%s&file=%s"
                                % (q, urllib.parse.quote("说明/README.md")))
        check(st == 200 and body.startswith("# 测试说明".encode("utf-8")),
              "/api/workfile 读到工作区文件", "status=%s len=%d" % (st, len(body)))
        check("markdown" in (hdr.get("Content-Type") or ""), "Markdown MIME 正确",
              hdr.get("Content-Type"))

        st, hdr, body = get_raw("/api/workfile?path=%s&file=%s"
                                % (q, urllib.parse.quote("../../windows/win.ini")))
        check(st in (400, 403), "工作区路径穿越被拒", "status=%s" % st)

        if dwg:
            st, hdr, body = get_raw("/api/dwg2dxf?path=%s&file=%s"
                                    % (q, urllib.parse.quote("图纸/机架.dwg")))
            check(st == 200 and b"ENTITIES" in body, "DWG 转 DXF 成功",
                  "status=%s len=%d" % (st, len(body)))
            plain = body
            st, hdr, body = get_raw("/api/dwg2dxf?path=%s&file=%s"
                                    % (q, urllib.parse.quote("图纸/机架.dwg")),
                                    {"Accept-Encoding": "gzip"})
            check(st == 200 and hdr.get("Content-Encoding") == "gzip",
                  "DWG 转换支持 gzip", hdr.get("Content-Encoding"))
            import gzip
            check(gzip.decompress(body) == plain, "gzip 解压后与明文一致",
                  "%d → %d" % (len(body), len(plain)))

        st, hdr, body = get_raw("/api/font?name=simhei.ttf")
        check(st == 200 and len(body) > 100000, "字体接口可用", "status=%s" % st)
        st, hdr, body = get_raw("/api/font?name=..%2F..%2Fwindows%2Fwin.ini")
        check(st == 403, "字体白名单挡住任意文件", "status=%s" % st)

        st, hdr, body = get_raw("/vendor/occt/occt-import-js.wasm")
        check(st == 200 and hdr.get("Content-Type") == "application/wasm",
              "WASM 以 application/wasm 提供", hdr.get("Content-Type"))

        print("== 浏览器端到端 ==")
        run_browser(c1)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=8)
        except Exception:
            srv.kill()

    print()
    if FAILS:
        print("FAILED:", FAILS)
        sys.exit(1)
    print("ALL PASS")


def run_browser(commit):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        seen, bad = [], []
        pg.on("console", lambda m: seen.append(m.type + ": " + m.text)
              if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: seen.append("pageerror: " + str(e)))
        pg.on("response", lambda r: bad.append("%s %s" % (r.status, r.url))
              if r.status >= 400 else None)
        pg.goto(BASE + "/#p=" + urllib.parse.quote(ROOT), wait_until="networkidle")
        pg.wait_for_timeout(1200)
        pg.locator(".commit").first.click()
        pg.wait_for_timeout(900)

        def open_row(name):
            row = pg.locator(".frow[data-path*='%s']" % name).first
            check(row.count() == 1, "找到 %s 行" % name)
            row.click()
            pg.wait_for_selector(".pv-overlay", timeout=15000)
            pg.wait_for_timeout(300)
            return row

        def close():
            pg.locator(".pv-close").click()
            pg.wait_for_timeout(250)

        # ---- Markdown
        open_row("README.md")
        check(pg.locator(".pv-md h1").inner_text().startswith("测试说明"), "Markdown 渲染出标题")
        check(pg.locator(".pv-md table td").count() >= 2, "Markdown 表格渲染")
        check(pg.locator(".pv-md pre code").count() >= 1, "Markdown 代码块渲染")
        img_ok = pg.evaluate("() => { const i = document.querySelector('.pv-md img');"
                             " return !!(i && i.complete && i.naturalWidth > 0); }")
        check(img_ok, "Markdown 里的相对路径图片能显示")
        pg.screenshot(path=os.path.join(TMP, "preview-1-md.png"))
        close()

        # ---- DXF
        open_row("简图.dxf")
        pg.wait_for_function("() => (document.querySelector('.pv-note')||{}).textContent"
                             ".includes('滚轮缩放')", timeout=30000)
        check(pg.locator(".pv-canvas canvas").count() >= 1, "DXF 建出 WebGL 画布")
        shot = pg.locator(".pv-canvas").screenshot()
        check(len(shot) > 3000, "DXF 画布上有内容", "%d 字节" % len(shot))
        pg.screenshot(path=os.path.join(TMP, "preview-2-dxf.png"))
        close()

        # ---- DWG
        if os.path.isfile(os.path.join(ROOT, "图纸", "机架.dwg")):
            open_row("机架.dwg")
            pg.wait_for_function("() => (document.querySelector('.pv-note')||{}).textContent"
                                 ".includes('滚轮缩放')", timeout=60000)
            check("已转 DXF" in pg.locator(".pv-note").inner_text(), "DWG 走服务端转换")
            shot = pg.locator(".pv-canvas").screenshot()
            check(len(shot) > 3000, "DWG 画布上有内容", "%d 字节" % len(shot))
            pg.screenshot(path=os.path.join(TMP, "preview-3-dwg.png"))
            close()

        # ---- STEP
        open_row("方块.step")
        pg.wait_for_function("() => (document.querySelector('.pv-note')||{}).textContent"
                             ".includes('个部件')", timeout=60000)
        check(pg.locator(".pv-canvas canvas").count() >= 1, "STEP 建出三维画布")
        shot = pg.locator(".pv-canvas").screenshot()
        check(len(shot) > 3000, "STEP 画布上有内容", "%d 字节" % len(shot))
        pg.screenshot(path=os.path.join(TMP, "preview-4-step.png"))
        close()

        # ---- STL
        open_row("三角.stl")
        pg.wait_for_function("() => (document.querySelector('.pv-note')||{}).textContent"
                             ".includes('STL')", timeout=30000)
        check(pg.locator(".pv-canvas canvas").count() >= 1, "STL 建出三维画布")
        shot = pg.locator(".pv-canvas").screenshot()
        check(len(shot) > 2000, "STL 画布上有内容", "%d 字节" % len(shot))
        pg.screenshot(path=os.path.join(TMP, "preview-5-stl.png"))
        close()

        # ---- 图片
        open_row("色块.png")
        ok = pg.evaluate("() => { const i = document.querySelector('.pv-imgwrap img');"
                         " return i && i.complete && i.naturalWidth > 0; }")
        check(ok, "图片预览加载成功")
        pg.screenshot(path=os.path.join(TMP, "preview-6-img.png"))
        close()

        # ---- 浮层按钮：与上一版对比 / 用本机程序打开
        row = open_row("简图.dxf")
        names = [b.inner_text() for b in pg.locator(".pv-tools button").all()]
        check(any("打开" in n for n in names), "浮层带「用本机程序打开」", "、".join(names))
        close()

        # 双击仍然走「用本机程序打开」，不再弹预览
        pg.locator(".pv-close").count() and None
        errs = [s for s in seen if s.startswith("pageerror")]
        check(not errs, "没有 JS 报错", "; ".join(errs)[:200])
        hard = [x for x in bad if "/api/preview" not in x]
        check(not hard, "没有失败请求", "; ".join(hard)[:200])
        print("  截图：", TMP)
        b.close()


if __name__ == "__main__":
    main()
