/* 缩略图补全：把「服务端出不了图」的模型在浏览器里渲成 PNG，再回存到服务端缓存。
 *
 * 为什么要这么做：
 *   .sldprt/.sldasm/.dwg 是 OLE 复合文档，服务端能直接抠出内嵌预览图，或者借
 *   Windows 外壳缩略图处理器出图；但 STEP/STP/IGES/STL/OBJ 这些是纯几何数据，
 *   服务端既没有解析器、Windows 也没注册缩略图处理器，只能靠浏览器里已有的
 *   occt-import-js（STEP/IGES）和 three STLLoader（STL）来渲染。
 *
 * 渲染完按 oid 存回服务端（POST /api/preview/put），于是：
 *   同一个零件在任何版本、任何机器上都是同一张图，而且只需要生成一次。
 */
import * as THREE from './vendor/three/three.module.min.js';
import { STLLoader } from './vendor/three/STLLoader.js';

const RENDER = 512;          // 生成时按 512 渲
const SMALL = 128;           // 列表用的尺寸
const SIZES = [512, 256, 128];  // 与服务端 thumbs.SIZES 对齐，三个都回存
const MAX_BYTES = 120 << 20; // 超大装配体不碰
const MAX_PER_SCAN = 600;    // 一次扫描最多补这么多，别把机器占满

const STEP_EXT = ['.step', '.stp', '.iges', '.igs', '.brep'];
const STL_EXT = ['.stl'];
// SolidWorks 零件里没存预览图的那批，服务端抠不出图、外壳处理器也只会给张空白。
// 但服务端的 3D 预览能把它导出成网格（/api/mesh，靠装了 SolidWorks 的那台机器），
// 网格照样能在这里渲成缩略图，而且一次生成两台机器共用。
const SW_EXT = ['.sldprt', '.sldasm'];

const extOf = (p) => {
  const m = /(\.[^./\\]+)$/.exec(String(p || ''));
  return m ? m[1].toLowerCase() : '';
};

function canRender(name) {
  const e = extOf(name);
  return STEP_EXT.includes(e) || STL_EXT.includes(e) || SW_EXT.includes(e);
}

/* 这些状态是「过一会儿就好」，不是「这个零件永远没图」 */
function isTransient(status) {
  return status === 409 || status === 429 || status >= 500;
}

function withToken(url, token) {
  if (!token) return url;
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
}

/* ------------------------------------------------------------------ occt */
let occtPromise = null;
function loadOcct() {
  if (occtPromise) return occtPromise;
  occtPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'vendor/occt/occt-import-js.js';
    s.onload = () => {
      try {
        Promise.resolve(window.occtimportjs({ locateFile: (f) => 'vendor/occt/' + f }))
          .then(resolve, reject);
      } catch (e) { reject(e); }
    };
    s.onerror = () => reject(new Error('OpenCascade 加载失败'));
    document.head.appendChild(s);
  });
  // 失败后允许下次重试，别把一次偶发失败固化下来
  occtPromise.catch(() => { occtPromise = null; });
  return occtPromise;
}

/* --------------------------------------------------------------- 离屏渲染器 */
let rig = null;
function getRig() {
  if (rig) {
    if (!rig.host.isConnected && document.body) document.body.appendChild(rig.host);
    return rig;
  }
  const canvas = document.createElement('canvas');
  canvas.width = RENDER;
  canvas.height = RENDER;
  const host = document.createElement('div');
  host.setAttribute('aria-hidden', 'true');
  // 必须真的在 DOM 里，WebGL 上下文才稳定；放屏幕外不占版面
  host.style.cssText = 'position:fixed;left:-10000px;top:0;width:1px;height:1px;' +
    'overflow:hidden;pointer-events:none;opacity:0';
  host.appendChild(canvas);
  if (document.body) document.body.appendChild(host);

  const renderer = new THREE.WebGLRenderer({
    canvas, antialias: true, preserveDrawingBuffer: true, powerPreference: 'low-power',
  });
  renderer.setPixelRatio(1);
  renderer.setSize(RENDER, RENDER, false);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#f4f6fa');
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.25));
  const key = new THREE.DirectionalLight(0xffffff, 2.1);
  key.position.set(0.6, 0.9, 0.8);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.85);
  fill.position.set(-0.8, -0.3, -0.6);
  scene.add(fill);

  const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 1e7);
  rig = { host, canvas, renderer, scene, camera };
  return rig;
}

/* 把一组网格摆正、框满、渲染，返回 canvas */
function shoot(group) {
  const r = getRig();
  const { renderer, scene, camera, canvas } = r;
  scene.add(group);
  try {
    const sphere = new THREE.Box3().setFromObject(group).getBoundingSphere(new THREE.Sphere());
    if (!sphere || !isFinite(sphere.radius) || sphere.radius <= 0) {
      throw new Error('模型没有可见的几何');
    }
    const dist = sphere.radius / Math.sin((camera.fov * Math.PI / 180) / 2) * 1.06;
    const dir = new THREE.Vector3(0.62, 0.46, 1).normalize();
    camera.position.copy(sphere.center).addScaledVector(dir, dist);
    camera.near = Math.max(dist / 2000, 1e-4);
    camera.far = dist * 50;
    camera.updateProjectionMatrix();
    camera.lookAt(sphere.center);
    renderer.render(scene, camera);
    return canvas;
  } finally {
    scene.remove(group);
    group.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
      }
    });
  }
}

/* 大图缩成列表用的小图 */
function shrink(canvas, size) {
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const g = c.getContext('2d');
  g.imageSmoothingEnabled = true;
  g.imageSmoothingQuality = 'high';
  g.fillStyle = '#f4f6fa';
  g.fillRect(0, 0, size, size);
  g.drawImage(canvas, 0, 0, size, size);
  return c;
}

/* ------------------------------------------------------------- 各格式 → three 网格 */
async function buildStep(buf) {
  const occt = await loadOcct();
  const res = occt.ReadStepFile(new Uint8Array(buf), null);
  if (!res || !res.success || !res.meshes || !res.meshes.length) {
    throw new Error('模型里没有可显示的面');
  }
  const group = new THREE.Group();
  for (const m of res.meshes) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position',
      new THREE.BufferAttribute(new Float32Array(m.attributes.position.array), 3));
    if (m.attributes.normal) {
      geo.setAttribute('normal',
        new THREE.BufferAttribute(new Float32Array(m.attributes.normal.array), 3));
    } else {
      geo.computeVertexNormals();
    }
    if (m.index) geo.setIndex(new THREE.BufferAttribute(new Uint32Array(m.index.array), 1));
    const c = m.color || [0.55, 0.62, 0.72];
    group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
      color: new THREE.Color(c[0], c[1], c[2]),
      metalness: 0.15, roughness: 0.62, side: THREE.DoubleSide,
    })));
  }
  return group;
}

function buildStl(buf) {
  const geo = new STLLoader().parse(buf);
  geo.computeVertexNormals();
  const group = new THREE.Group();
  group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
    color: 0x8fa3bf, metalness: 0.2, roughness: 0.6, side: THREE.DoubleSide,
  })));
  return group;
}

/* 服务端的 ZGM1 网格 → three 几何（格式同 web/meshview.js） */
function buildZgm(buf) {
  const dv = new DataView(buf);
  if (buf.byteLength < 12 ||
      String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3)) !== 'ZGM1') {
    throw new Error('网格格式不认识');
  }
  const nv = dv.getUint32(4, true);
  const nt = dv.getUint32(8, true);
  if (!nv || !nt || buf.byteLength < 12 + nv * 12 + nt * 12) throw new Error('网格是空的');
  const pos = new Float32Array(buf, 12, nv * 3);
  const idx = new Uint32Array(buf, 12 + nv * 12, nt * 3);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setIndex(new THREE.BufferAttribute(idx, 1));
  geo.computeVertexNormals();
  const group = new THREE.Group();
  group.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
    color: 0x9aa7b8, metalness: 0.18, roughness: 0.6, side: THREE.DoubleSide,
  })));
  return group;
}

/* ------------------------------------------------------------------ 队列 */
const queue = new Map();      // key -> job
const skipped = new Set();    // 试过但失败的，同一个对象不反复试
const done = new Set();       // 已经成功的，绝不再排队（否则图片刷新失败会无限循环）
// 「现在不行」和「这个对象根本渲不出来」是两回事：
//   服务端 409（那台机器没装 SolidWorks / SolidWorks 正忙）、429（生成太频繁）、
//   5xx、网络断了 —— 这些都是过一会儿就好的。以前一律当成永久的 skipped，
//   于是一次「正忙」就让这个零件这一整次会话都没有图；更糟的是 page reload 后又
//   重来一遍，机器忙的时候两边的请求互相把对方顶掉。
// 现在这些状态按指数退避重排（2s → 4s → 8s …，最多 5 次），真失败才进 skipped。
const retryAt = new Map();    // key -> {t: 时间戳, n: 次数}
const RETRY_MAX = 5;
let draining = false;
let paused = false;
let timer = null;

function ctxOf(it) {
  const ctx = it.ctx || {};
  return { path: ctx.path || '', token: ctx.token || '' };
}

/* 历史版本里的对象（有内容指纹，两台机器共用一张图） */
function backoffWait(key) {
  const r = retryAt.get(key);
  if (!r) return 0;
  return Math.max(0, r.t - Date.now());
}

export function want(oid, name, ctx) {
  if (!oid || oid.length !== 64 || !canRender(name)) return;
  const key = 'o:' + oid;
  if (done.has(key) || skipped.has(key) || queue.has(key)) return;
  if (backoffWait(key) > 0) return;
  queue.set(key, { mode: 'oid', oid, name: name || '', ctx: ctx || {} });
  pump();
}

/* 工作区里还没记录的文件的（没有对象号，按 路径+mtime 存） */
export function wantFile(rel, name, ctx, v) {
  if (!rel || !canRender(name || rel)) return;
  const key = 'f:' + rel + '|' + (v || '');
  if (done.has(key) || skipped.has(key) || queue.has(key)) return;
  if (backoffWait(key) > 0) return;
  queue.set(key, { mode: 'file', rel, name: name || rel, v, ctx: ctx || {} });
  pump();
}

export function pause(on) { paused = !!on; if (!paused) pump(); }

// 窗口最小化/切走时别在后台啃 CPU
document.addEventListener('visibilitychange', () => pause(document.hidden));

async function pump() {
  if (draining || paused) return;
  draining = true;
  try {
    while (queue.size && !paused) {
      const [key, it] = queue.entries().next().value;
      queue.delete(key);
      try {
        await generate(it);
        done.add(key);
        retryAt.delete(key);
      } catch (e) {
        if (e && e.transient) {
          // 过一会儿再试；连着几次都不行才算真失败
          const n = ((retryAt.get(key) || {}).n || 0) + 1;
          if (n > RETRY_MAX) {
            skipped.add(key);
            retryAt.delete(key);
          } else {
            const wait = Math.min(60000, 2000 * Math.pow(2, n - 1));
            retryAt.set(key, { t: Date.now() + wait, n, job: it });
            if (!timer) {
              timer = setTimeout(() => {
                timer = null;
                const now = Date.now();
                for (const [k, r] of retryAt.entries()) {
                  if (r.t <= now) {
                    retryAt.delete(k);
                    if (r.job) queue.set(k, r.job);
                  }
                }
                pump();
              }, Math.max(500, wait));
            }
          }
        } else {
          skipped.add(key);    // 这个对象根本渲不出来（格式不支持/数据坏了）
        }
      }
      // 让出一帧，别把界面卡住
      await new Promise((r) => setTimeout(r, 16));
    }
  } finally {
    draining = false;
  }
}

async function generate(it) {
  const { path, token } = ctxOf(it);
  const name = it.name || '';
  const isOid = it.mode === 'oid';
  const ext = extOf(name);

  let group;
  if (isOid && SW_EXT.includes(ext)) {
    // 走服务端的 3D 网格：那台装了 SolidWorks 的机器现导出一次，之后一直用缓存
    const url = withToken('/api/mesh?' + new URLSearchParams({
      path, file: it.rel || name, oid: it.oid,
    }).toString(), token);
    const mr = await fetch(url, { cache: 'force-cache' });
    if (!mr.ok) {
      let msg = 'HTTP ' + mr.status;
      try { const j = await mr.json(); if (j.error) msg = j.error; } catch (e) { /* 非 JSON */ }
      const err = new Error(msg);
      err.transient = isTransient(mr.status);
      throw err;
    }
    group = buildZgm(await mr.arrayBuffer());
  } else {
    const src = isOid
      ? withToken('/api/file?' + new URLSearchParams({ path, oid: it.oid }).toString(), token)
      : withToken('/api/workfile?' + new URLSearchParams({ path, file: it.rel }).toString(), token);
    const res = await fetch(src, { cache: 'force-cache' });
    if (!res.ok) {
      const err = new Error('取文件失败：HTTP ' + res.status);
      err.transient = isTransient(res.status);
      throw err;
    }
    const buf = await res.arrayBuffer();
    if (buf.byteLength > MAX_BYTES) throw new Error('文件太大');
    group = STEP_EXT.includes(ext) ? await buildStep(buf) : buildStl(buf);
  }
  const big = shoot(group);
  const endpoint = isOid ? '/api/preview/put' : '/api/preview/putfile';

  // 三个尺寸都要存：列表要 128、未保存网格要 256、悬浮放大要 512
  for (const size of SIZES) {
    const png = size === RENDER ? big.toDataURL('image/png')
      : shrink(big, size).toDataURL('image/png');
    const u = withToken(endpoint + '?' + new URLSearchParams({
      path, size: String(size),
    }).toString(), token);
    const r = await fetch(u, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // path 必须同时放在 body 里：POST 处理读的是 body.path，不是查询串
      body: JSON.stringify(isOid
        ? { path, oid: it.oid, png, size }
        : { path, file: it.rel, v: it.v, png, size }),
    });
    if (!r.ok) {
      const err = new Error('回存失败：HTTP ' + r.status);
      err.transient = isTransient(r.status);
      throw err;
    }
  }
  window.dispatchEvent(new CustomEvent('zgr-thumb', {
    detail: isOid ? { oid: it.oid } : { file: it.rel },
  }));
}

/* 打开项目后扫一遍：把还没有图的模型排进队列 */
let scanning = false;
export async function scan(ctx) {
  const { path, token } = ctx || {};
  if (!path || scanning) return;
  scanning = true;
  try {
    let d;
    try {
      const r = await fetch(withToken('/api/preview/missing?' +
        new URLSearchParams({ path, size: String(SMALL) }).toString(), token),
        { cache: 'no-store' });
      if (!r.ok) return;
      d = await r.json();
    } catch (e) {
      return;
    }
    if (!d || !d.ok) return;
    let n = 0;
    for (const it of (d.need || [])) {
      if (n >= MAX_PER_SCAN) break;
      if (!canRender(it.name)) continue;   // 图片/SolidWorks 由服务端负责，这里只补几何格式
      const before = queue.size;
      want(it.oid, it.name, { path, token });
      if (queue.size > before) n++;
    }
    return { total: d.total || 0, queued: n };
  } finally {
    scanning = false;
  }
}

export function stats() {
  return { queued: queue.size, skipped: skipped.size, draining };
}

window.zgrThumbs = { want, wantFile, scan, stats, pause, canRender };
