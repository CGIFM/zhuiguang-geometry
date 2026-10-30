"""3D 预览端到端验证（不需要 SolidWorks：直接写入合成 ZGM 网格）

用法：python -I tests/e2e_mesh_ui.py   （需要 playwright + chromium）

覆盖：
  1. /api/mesh/info、/api/mesh 正常返回 + 响应头
  2. 非模型文件 / 非法对象号 → 409 且带中文原因
  3. 网页点模型行 → 3D 弹窗；画布真的画出了像素
  4. 改动标红（顶点数 > 0）、叠加上一版、容差滑条、关红/关叠加
  5. 无 JS 报错、截图留档
"""
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
TMP = os.path.join(tempfile.gettempdir(), "zgr-e2e-mesh")
ROOT = os.path.join(TMP, "示例项目")
PORT = 8901
BASE = "http://127.0.0.1:%d" % PORT

FAILS = []


def check(cond, label, extra=""):
    print(("  OK   " if cond else "  FAIL ") + label + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(label)


def post(route, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + route, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def get(route):
    with urllib.request.urlopen(BASE + route, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def get_raw(route):
    try:
        with urllib.request.urlopen(BASE + route, timeout=120) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def write(rel, text):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- 合成网格
def plate(nx=12, ny=8, w=100.0, h=60.0, d=10.0, bump=None):
    """一块 nx*ny 网格的板：上下面各 nx*ny*2 个三角 + 四周侧面"""
    verts = []
    top0 = len(verts) // 3

    def z_at(x, y):
        return d + (bump(x, y) if bump else 0.0)

    for j in range(ny + 1):
        for i in range(nx + 1):
            x = w * i / nx
            y = h * j / ny
            verts += [x, y, z_at(x, y)]
    for j in range(ny + 1):
        for i in range(nx + 1):
            x = w * i / nx
            y = h * j / ny
            verts += [x, y, 0.0]

    tris = []
    stride = nx + 1
    for j in range(ny):
        for i in range(nx):
            a = top0 + j * stride + i
            b = a + 1
            c = a + stride
            e = c + 1
            tris += [a, b, e, a, e, c]          # 上表面朝 +Z
    off = top0 + stride * (ny + 1)
    for j in range(ny):
        for i in range(nx):
            a = off + j * stride + i
            b = a + 1
            c = a + stride
            e = c + 1
            tris += [a, e, b, a, c, e]          # 下表面朝 -Z
    # 四周侧面（只需外观，绕序按外侧）
    ring = ([top0 + i for i in range(stride)]
            + [top0 + j * stride + nx for j in range(1, ny + 1)]
            + [top0 + ny * stride + i for i in range(nx - 1, -1, -1)]
            + [top0 + j * stride for j in range(ny - 1, 0, -1)])
    oring = [v + off - top0 for v in ring]
    for k in range(len(ring)):
        a = ring[k]
        b = ring[(k + 1) % len(ring)]
        c = oring[k]
        e = oring[(k + 1) % len(oring)]
        tris += [a, b, e, a, e, c]
    return verts, tris


def zgm_bytes(verts, tris, nverts, ntris):
    head = b"ZGM1" + struct.pack("<II", nverts, ntris)
    return head + struct.pack("<%df" % len(verts), *verts) + struct.pack("<%dI" % len(tris), *tris)


def save_mesh(oid, verts, tris, rel, kind="part"):
    md = os.path.join(ROOT, ".zgr", "meshes")
    os.makedirs(md, exist_ok=True)
    data = zgm_bytes(verts, tris, len(verts) // 3, len(tris) // 3)
    with open(os.path.join(md, oid + ".zgm"), "wb") as f:
        f.write(data)
    meta = {"oid": oid, "file": rel, "kind": kind, "verts": len(verts) // 3,
            "tris": len(tris) // 3, "bytes": len(data), "objects": 1,
            "source": "working", "seconds": 0.0, "exported": int(time.time() * 1000)}
    with open(os.path.join(md, oid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return len(data)


# ---------------------------------------------------------------- 造项目
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(ROOT)
write("总装/示例项目总装.sldasm", "ASM v1\n" + "x" * 4000)
write("零件/底座.sldprt", "P1 v1\n" + "y" * 80)
write("零件/立板.sldprt", "P2 v1\n" + "z" * 3000)
write("说明/装配说明.txt", "第一步\n")

# 注册表指到临时目录：测试项目不能写进用户真实的项目列表
_env = dict(os.environ, APPDATA=os.path.join(TMP, "appdata"))
srv = subprocess.Popen([sys.executable, os.path.join(APP, "zgr.py"), "serve",
                        "--port", str(PORT), "--host", "127.0.0.1"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env)
try:
    for _ in range(60):
        try:
            get("/api/ping")
            break
        except Exception:
            time.sleep(0.25)

    print("== 造两个版本 ==")
    post("/api/init", {"target": ROOT, "name": "示例项目"})
    c1 = post("/api/snapshot", {"path": ROOT, "message": "第一版"})["commit"]
    write("零件/底座.sldprt", "P1 v2\n" + "y" * 260)   # 内容变了 → 新对象号
    c2 = post("/api/snapshot", {"path": ROOT, "message": "第二版：底座加高一块"})["commit"]
    print("  v1", c1[:10], " v2", c2[:10])

    files = get("/api/commit?path=%s&id=%s" % (urllib.parse.quote(ROOT), c2))["files"]
    def oid_of(rel):
        if isinstance(files, dict):
            return files[rel]["oid"]
        return next(f["oid"] for f in files if f["path"] == rel)
    oid_new = oid_of("零件/底座.sldprt")
    prev = get("/api/commit?path=%s&id=%s" % (urllib.parse.quote(ROOT), c1))["files"]
    def oid_prev(rel):
        if isinstance(prev, dict):
            return prev[rel]["oid"]
        return next(f["oid"] for f in prev if f["path"] == rel)
    oid_old = oid_prev("零件/底座.sldprt")
    print("  底座 old=%s new=%s" % (oid_old[:10], oid_new[:10]))

    v_old, t_old = plate()
    v_new, t_new = plate(bump=lambda x, y: 12.0 if (x > 55 and y > 30) else 0.0)
    sz_new = save_mesh(oid_new, v_new, t_new, "零件/底座.sldprt")
    save_mesh(oid_old, v_old, t_old, "零件/底座.sldprt")

    print("== HTTP 接口 ==")
    q = urllib.parse.quote(ROOT)
    st, hdr, body = get_raw("/api/mesh/info?path=%s&file=%s&oid=%s"
                            % (q, urllib.parse.quote("零件/底座.sldprt"), oid_new))
    info = json.loads(body.decode("utf-8"))
    check(st == 200 and info.get("cached") is True, "/api/mesh/info 命中缓存", str(info.get("meta", {}).get("tris")))

    st, hdr, body = get_raw("/api/mesh?path=%s&file=%s&oid=%s"
                            % (q, urllib.parse.quote("零件/底座.sldprt"), oid_new))
    check(st == 200, "/api/mesh 返回 200", "status=%s len=%d" % (st, len(body)))
    check(len(body) == sz_new, "网格字节数一致", "%d == %d" % (len(body), sz_new))
    check(hdr.get("Content-Type") == "application/octet-stream", "Content-Type 正确", hdr.get("Content-Type"))
    check(hdr.get("X-ZGR-Tris") == str(len(t_new) // 3), "X-ZGR-Tris 头正确", hdr.get("X-ZGR-Tris"))
    check("immutable" in (hdr.get("Cache-Control") or ""), "缓存头 immutable", hdr.get("Cache-Control"))

    st, hdr, body = get_raw("/api/mesh?path=%s&file=%s&oid=%s"
                            % (q, urllib.parse.quote("说明/装配说明.txt"), oid_new))
    err_txt = json.loads(body.decode("utf-8")).get("error", "") if st != 200 else ""
    check(st == 409, "非模型文件被拒", "status=%s %s" % (st, err_txt))
    check("SolidWorks" in err_txt or "零件" in err_txt, "拒绝原因可读", err_txt)

    st, hdr, body = get_raw("/api/mesh?path=%s&file=%s&oid=%s"
                            % (q, urllib.parse.quote("零件/底座.sldprt"), "zzzz"))
    check(st in (400, 409), "非法对象号被拒", "status=%s %s" % (st, body[:80]))

    st, hdr, body = get_raw("/api/mesh?path=%s&file=%s&oid=%s"
                            % (q, urllib.parse.quote("../../etc/passwd"), oid_new))
    check(st == 409, "路径穿越被拒", "status=%s" % st)

    print("== 浏览器端到端 ==")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        seen = []
        bad = []
        pg.on("console", lambda m: seen.append(m.type + ": " + m.text))
        pg.on("pageerror", lambda e: seen.append("pageerror: " + str(e)))
        pg.on("response", lambda r: bad.append("%s %s" % (r.status, r.url)) if r.status >= 400 else None)
        pg.goto(BASE + "/#p=" + urllib.parse.quote(ROOT), wait_until="networkidle")
        pg.wait_for_timeout(1000)
        pg.locator(".commit").first.click()
        pg.wait_for_timeout(900)
        row = pg.locator(".frow[data-path*='底座.sldprt']").first
        check(row.count() == 1, "版本详情里找到底座行")
        row.click()
        pg.wait_for_selector(".mesh-stage canvas", timeout=15000)
        pg.wait_for_timeout(1800)
        pg.screenshot(path=os.path.join(TMP, "mesh-1-open.png"))

        check(pg.locator(".mesh-modal").count() == 1, "3D 弹窗已打开")
        note = pg.locator(".mesh-note").inner_text()
        check("红色" in note, "改动提示出现", note[:60])
        check("与上一版完全重合" not in note, "确实检测到改动", note[:60])
        import re as _re
        m = _re.search(r"([\d,]+)\s*个顶点", note)
        nchg = int(m.group(1).replace(",", "")) if m else -1
        # 234 个顶点里只有抬高的那块（24 个）该算改动；早先按“重心距离”比会把
        # 没改的粗网格区域全标红（234 个），这里守住不准误报
        check(10 <= nchg <= 40, "改动顶点数不虚高", "changed=%s / 234" % nchg)
        meta_txt = pg.locator(".mesh-meta").inner_text()
        check("面" in meta_txt, "面数显示", meta_txt)

        SAMPLE = """() => {
          const c = document.querySelector('.mesh-stage canvas');
          const gl = c.getContext('webgl2') || c.getContext('webgl');
          if (!gl) return {err: 'no gl'};
          const px = new Uint8Array(c.width * c.height * 4);
          gl.readPixels(0, 0, c.width, c.height, gl.RGBA, gl.UNSIGNED_BYTE, px);
          let solid = 0, red = 0, best = 0, bpx = [0, 0, 0];
          for (let i = 0; i < px.length; i += 4) {
            if (px[i + 3] > 8) solid++;
            if (px[i + 3] > 8) {
              const score = px[i] - Math.max(px[i + 1], px[i + 2]);
              if (score > best) { best = score; bpx = [px[i], px[i + 1], px[i + 2]]; }
              if (score > 25) red++;
            }
          }
          return {w: c.width, h: c.height, solid, red, best, bpx};
        }"""
        gl = pg.evaluate(SAMPLE)
        print("  像素:", gl)
        check(gl.get("w", 0) > 300 and gl.get("h", 0) > 200, "画布尺寸正常",
              "%sx%s" % (gl.get("w"), gl.get("h")))
        check(gl.get("solid", 0) > 200, "画布真的画出了实体", "solid=%s" % gl.get("solid"))
        check(gl.get("red", 0) > 0, "屏幕上能看到红色区域", "red=%s" % gl.get("red"))

        # 关掉叠加
        pg.locator(".m-ghost").uncheck()
        pg.wait_for_timeout(500)
        pg.screenshot(path=os.path.join(TMP, "mesh-2-no-ghost.png"))
        check(pg.locator(".mesh-stage canvas").count() == 1, "关叠加重绘成功")

        # 关掉红色
        pg.locator(".m-red").uncheck()
        pg.wait_for_timeout(400)
        gl2 = pg.evaluate(SAMPLE)
        check(gl2.get("red", -1) == 0, "关掉红色后不再有红像素", "red=%s" % gl2.get("red"))
        check(gl2.get("solid", 0) > 0, "关红后模型还在", "solid=%s" % gl2.get("solid"))
        pg.locator(".m-red").check()
        pg.locator(".m-ghost").check()
        pg.wait_for_timeout(400)

        # 容差拖到最大：改动应减少或不变，但不能崩
        before_note = pg.locator(".mesh-note").inner_text()
        pg.locator(".m-tol").fill("40")
        pg.locator(".m-tol").dispatch_event("input")
        pg.wait_for_timeout(700)
        after_note = pg.locator(".mesh-note").inner_text()
        check(pg.locator(".mesh-tolv, .mesh-side .m-tolv").first.inner_text() == "4.0%", "容差显示 4.0%",
              pg.locator(".m-tolv").first.inner_text())
        check(pg.locator(".mesh-stage canvas").count() == 1, "拖容差后没崩", after_note[:40])
        pg.screenshot(path=os.path.join(TMP, "mesh-3-tol-max.png"))

        # 重置视角 + 拖动旋转
        pg.locator(".m-reset").click()
        pg.wait_for_timeout(300)
        box = pg.locator(".mesh-stage canvas").bounding_box()
        pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        pg.mouse.down()
        pg.mouse.move(box["x"] + box["width"] / 2 + 160, box["y"] + box["height"] / 2 + 40, steps=12)
        pg.mouse.up()
        pg.wait_for_timeout(500)
        pg.screenshot(path=os.path.join(TMP, "mesh-4-rotated.png"))
        check(pg.locator(".mesh-stage canvas").count() == 1, "拖动旋转后仍在")

        # 关闭弹窗
        pg.locator(".mesh-modal .cancel").click()
        pg.wait_for_timeout(400)
        check(pg.locator(".mesh-modal").count() == 0, "弹窗可关闭")

        # 假 .sldprt 的缩略图必然生成不了，404 属预期；只关心其它失败
        errs = [s for s in seen if s.startswith("error") or s.startswith("pageerror")]
        errs = [e for e in errs if "Failed to load resource" not in e]
        print("  >=400 的请求:", [u.split('?')[0] for u in bad][:6])
        real_bad = [u for u in bad if "favicon" not in u and "/api/preview" not in u]
        check(not real_bad, "没有意外的失败请求", str(real_bad[:4]))
        check(not errs, "无 JS 报错", str(errs[:3]))
        b.close()

    print("== 对照：缓存缺失时的行为（本机没 SolidWorks）==")
    write("零件/立板.sldprt", "P2 v2 改一下\n" + "z" * 3200)
    c3 = post("/api/snapshot", {"path": ROOT, "message": "第三版：改立板"})["commit"]
    f3 = get("/api/commit?path=%s&id=%s" % (urllib.parse.quote(ROOT), c3))["files"]
    oid3 = f3["零件/立板.sldprt"]["oid"] if isinstance(f3, dict) else \
        next(f["oid"] for f in f3 if f["path"] == "零件/立板.sldprt")
    st, hdr, body = get_raw("/api/mesh?path=%s&file=%s&oid=%s"
                            % (urllib.parse.quote(ROOT), urllib.parse.quote("零件/立板.sldprt"), oid3))
    err = json.loads(body.decode("utf-8")).get("error", "") if st != 200 else ""
    print("  status=%s err=%s" % (st, err))
    check(st == 409, "没缓存且没 SolidWorks 时给出人话错误")
    check(bool(err), "错误说明非空")
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
