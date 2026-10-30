/* 文件预览：Markdown / DXF / DWG / STEP / STL / 图片
 *
 * 引擎（都随程序内置，离线可用）：
 *   Markdown  marked 18 + DOMPurify 3
 *   DXF/DWG   dxf-viewer 1.0.50（WebGL，作者 vagran）+ LibreDWG（DWG 由服务端转 DXF）
 *   STEP/STP  occt-import-js 0.0.23（OpenCascade WASM）
 *   STL       three.js STLLoader
 */
import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { STLLoader } from './vendor/three/STLLoader.js';
import { DxfViewer } from './vendor/dxf-viewer/index.js';
import { marked } from './vendor/deps/marked.esm.js';
import DOMPurify from './vendor/deps/purify.es.mjs';

const $ = (sel, root) => (root || document).querySelector(sel);
const extOf = (p) => (String(p || '').match(/\.[^./\\]+$/) || [''])[0].toLowerCase();
const enc = encodeURIComponent;

let cur = null;               // 当前预览的清理函数
let occtPromise = null;

function cleanup() {
  if (cur) {
    try { cur(); } catch (e) { /* 忽略 */ }
    cur = null;
  }
  const old = $('#pvOverlay');
  if (old) old.remove();
  delete window.__zgrDxf;      // 别把整个图纸场景挂在全局上
}

function srcUrl(path, file, oid, api) {
  return `${api}?path=${enc(path || '')}&` +
    (oid ? `oid=${enc(oid)}` : `file=${enc(file || '')}`);
}

/* 有对象号就按对象号取（看的就是这个历史版本），否则读工作区当前文件 */
function rawUrl(path, file, oid) {
  return oid ? `/api/file?path=${enc(path || '')}&oid=${enc(oid)}`
             : `/api/workfile?path=${enc(path || '')}&file=${enc(file || '')}`;
}

async function fetchBytes(path, file, oid) {
  let r = null;
  if (oid) r = await fetch(rawUrl(path, file, oid));
  if (!r || !r.ok) r = await fetch(srcUrl(path, file, '', '/api/workfile'));
  if (!r.ok) throw new Error('读取文件失败：HTTP ' + r.status);
  return await r.arrayBuffer();
}

async function fetchText(path, file, oid) {
  const buf = await fetchBytes(path, file, oid);
  const u8 = new Uint8Array(buf);
  let text = new TextDecoder('utf-8', { fatal: false }).decode(u8);
  // 老图纸/说明常是 GBK：UTF-8 解出来替换符很多时改用 GBK
  if ((text.match(/�/g) || []).length > 3) {
    try { text = new TextDecoder('gbk').decode(u8); } catch (e) { /* 保留 utf-8 结果 */ }
  }
  return text;
}

/* --------------------------------------------------------------- 3D 查看器 */
function createThreeViewer(host) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(host.clientWidth || 800, host.clientHeight || 600);
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#f4f6fa');

  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1e6);
  camera.position.set(3, 3, 3);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.15));
  const dir = new THREE.DirectionalLight(0xffffff, 2.2);
  dir.position.set(1, 2, 1.5);
  scene.add(dir);
  const dir2 = new THREE.DirectionalLight(0xffffff, 0.9);
  dir2.position.set(-1.5, -1, -1);
  scene.add(dir2);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  const group = new THREE.Group();
  scene.add(group);

  let raf = 0;
  const tick = () => { raf = requestAnimationFrame(tick); controls.update(); renderer.render(scene, camera); };
  tick();

  const ro = new ResizeObserver(() => {
    const w = host.clientWidth, h = host.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  });
  ro.observe(host);

  function fit() {
    const box = new THREE.Box3().setFromObject(group);
    if (box.isEmpty()) return null;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const r = Math.max(size.x, size.y, size.z) * 0.9 + 1e-6;
    const dist = r / Math.tan((camera.fov * Math.PI / 180) / 2) * 0.9;
    camera.position.set(center.x + dist * 0.6, center.y + dist * 0.6, center.z + dist);
    camera.near = Math.max(dist / 1000, 0.01);
    camera.far = dist * 100;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
    return { size, center };
  }

  function addMesh(geometry, material) {
    const mesh = new THREE.Mesh(geometry, material);
    group.add(mesh);
    return mesh;
  }

  function dispose() {
    cancelAnimationFrame(raf);
    ro.disconnect();
    controls.dispose();
    group.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
    });
    renderer.dispose();
  }

  return { scene, camera, controls, group, fit, addMesh, dispose };
}

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
    s.onerror = () => reject(new Error('WASM 加载失败'));
    document.head.appendChild(s);
  });
  return occtPromise;
}

/* --------------------------------------------------------------- 各类预览 */
function pvFrame() {
  const overlay = document.createElement('div');
  overlay.className = 'pv-overlay';
  overlay.id = 'pvOverlay';
  overlay.innerHTML = `
    <div class="pv-box">
      <div class="pv-head">
        <div class="pv-title"></div>
        <div class="pv-tools"></div>
      </div>
      <div class="pv-body"></div>
      <div class="pv-foot"><span class="pv-note"></span><span class="pv-spacer"></span></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) cleanup(); });
  return overlay;
}

function closeButton(overlay) {
  const b = document.createElement('button');
  b.className = 'pv-close';
  b.textContent = '关闭';
  b.onclick = cleanup;
  return b;
}

function showError(overlay, msg) {
  $('.pv-body', overlay).innerHTML =
    `<div class="pv-msg"><b>打不开这个预览</b><div>${escapeHtml(msg)}</div></div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderMarkdown(overlay, text, file, path) {
  const body = $('.pv-body', overlay);
  const tabs = document.createElement('div');
  tabs.className = 'pv-tabs';
  tabs.innerHTML = '<button class="on" data-v="html">预览</button><button data-v="raw">源码</button>';
  const pane = document.createElement('div');
  pane.className = 'pv-md';
  body.appendChild(tabs);
  body.appendChild(pane);

  const html = DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: false }), {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target'],
  });
  // 文档里常见「相对路径截图」：把同目录下的图片换成本项目的取文件接口
  const baseDir = file.includes('/') ? file.slice(0, file.lastIndexOf('/') + 1) : '';
  const show = (v) => {
    tabs.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.v === v));
    pane.className = v === 'html' ? 'pv-md' : 'pv-raw';
    pane.innerHTML = v === 'html' ? html : `<pre>${escapeHtml(text)}</pre>`;
    if (v === 'html') {
      pane.querySelectorAll('a[href]').forEach((a) => { a.target = '_blank'; a.rel = 'noreferrer'; });
      pane.querySelectorAll('img[src]').forEach((img) => {
        const src = img.getAttribute('src') || '';
        if (/^(https?:|data:|blob:|\/)/i.test(src)) return;
        // marked 会把中文文件名转成 %XX，这里先还原再统一编码，避免二次编码
        let raw = src;
        try { raw = decodeURIComponent(src); } catch (e) { /* 保持原样 */ }
        const rel = (baseDir + raw.replace(/^\.\//, '')).split('#')[0].split('?')[0];
        img.src = `/api/workfile?path=${enc(path || '')}&file=${enc(rel)}`;
        img.loading = 'lazy';
      });
    }
  };
  tabs.onclick = (e) => { const b = e.target.closest('button'); if (b) show(b.dataset.v); };
  show('html');
  $( '.pv-note', overlay).textContent = `Markdown · ${text.length} 字`;
  return () => {};
}

/* 图纸编码：R2007 以前的中文 DXF 多数是 GBK/ANSI_936，按 UTF-8 解会全是“?”。
   dxf-viewer 只会用 UTF-8，所以这里先转成文本再用 blob 交给它。 */
function dxfCodepage(u8) {
  const head = new TextDecoder('latin1').decode(u8.subarray(0, 262144));
  const m = head.match(/\$DWGCODEPAGE\s*[\r\n]+\s*3\s*[\r\n]+\s*([A-Za-z0-9_\-]+)/);
  const cp = (m ? m[1] : '').toUpperCase();
  return {
    ANSI_936: 'gbk', ANSI_950: 'big5', ANSI_932: 'shift_jis',
    ANSI_949: 'euc-kr', ANSI_1251: 'windows-1251',
  }[cp] || '';
}

function decodeDxf(buf) {
  const u8 = new Uint8Array(buf);
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(u8);
  } catch (e) { /* 不是合法 UTF-8，按图纸里的代码页再试 */ }
  for (const cp of [dxfCodepage(u8), 'gbk']) {
    if (!cp) continue;
    try { return new TextDecoder(cp).decode(u8); } catch (e) { /* 换下一个 */ }
  }
  return new TextDecoder('utf-8').decode(u8);
}

async function renderDxf(overlay, buf, label) {
  const body = $('.pv-body', overlay);
  const host = document.createElement('div');
  host.className = 'pv-canvas';
  body.appendChild(host);
  const note = $('.pv-note', overlay);
  note.textContent = label + ' · 正在解析…';

  const text = decodeDxf(buf);
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));

  const viewer = new DxfViewer(host, {
    autoResize: true,
    clearColor: new THREE.Color('#ffffff'),
    blackWhiteInversion: true,
    antialias: true,
  });
  if (!viewer.HasRenderer()) {
    URL.revokeObjectURL(url);
    throw new Error('浏览器不支持 WebGL');
  }
  await viewer.Load({
    url,
    fonts: ['/api/font?name=simhei.ttf'],
  });
  // 预览要「打开就能看」：有些图纸（尤其 LibreDWG 转出来的）所有图层都带关闭标记，
  // 原样显示会是一片空白，这里全部打开
  const layers = viewer.GetLayers();
  const hidden = layers.filter((l) => !l.visible);
  let extra = '';
  if (layers.length && hidden.length === layers.length) {
    for (const l of hidden) viewer.ShowLayer(l.name, true);
    extra = ' · 已打开全部图层';
  }
  note.textContent = label + ' · 滚轮缩放，拖动平移，双击适配' + extra;
  // 给测试/排查留一眼：图形范围和原点
  window.__zgrDxf = { bounds: viewer.GetBounds(), origin: viewer.GetOrigin(), viewer };
  const dbl = () => {
    const b = viewer.GetBounds();
    if (b) viewer.FitView(b.minX - viewer.GetOrigin().x, b.maxX - viewer.GetOrigin().x,
                          b.minY - viewer.GetOrigin().y, b.maxY - viewer.GetOrigin().y);
  };
  host.addEventListener('dblclick', dbl);
  return () => {
    host.removeEventListener('dblclick', dbl);
    viewer.Destroy();
    URL.revokeObjectURL(url);
  };
}

async function renderStep(overlay, buf, label) {
  const body = $('.pv-body', overlay);
  const host = document.createElement('div');
  host.className = 'pv-canvas';
  body.appendChild(host);
  const note = $('.pv-note', overlay);
  note.textContent = label + ' · 正在加载 OpenCascade…';

  const occt = await loadOcct();
  note.textContent = label + ' · 正在解析模型…';
  const res = occt.ReadStepFile(new Uint8Array(buf), null);
  if (!res || !res.success || !res.meshes.length) throw new Error('模型里没有可显示的面');

  const v = createThreeViewer(host);
  for (const m of res.meshes) {
    /* occt-import-js 给的是普通数组，three 要 TypedArray */
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
    const mat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(c[0], c[1], c[2]),
      metalness: 0.15, roughness: 0.62, side: THREE.DoubleSide,
    });
    v.addMesh(geo, mat);
  }
  v.fit();
  note.textContent = label + ` · ${res.meshes.length} 个部件 · 拖动旋转，滚轮缩放`;
  return () => v.dispose();
}

async function renderStl(overlay, buf, label) {
  const body = $('.pv-body', overlay);
  const host = document.createElement('div');
  host.className = 'pv-canvas';
  body.appendChild(host);
  const note = $('.pv-note', overlay);
  const geo = new STLLoader().parse(buf);
  geo.computeVertexNormals();
  const v = createThreeViewer(host);
  v.addMesh(geo, new THREE.MeshStandardMaterial({
    color: 0x8fa3bf, metalness: 0.2, roughness: 0.6, side: THREE.DoubleSide,
    flatShading: false,
  }));
  v.fit();
  note.textContent = label + ' · STL · 拖动旋转，滚轮缩放';
  return () => v.dispose();
}

function renderImage(overlay, url, label) {
  const body = $('.pv-body', overlay);
  body.innerHTML = `<div class="pv-imgwrap"><img src="${url}" alt=""></div>`;
  const img = $('img', body);
  $('.pv-note', overlay).textContent = label + ' · 图片预览';
  return () => { img.src = ''; };
}

/* --------------------------------------------------------------- 入口 */
export async function openFilePreview(opts) {
  cleanup();
  const { path, file, oid, oldOid, st, onDiff, onOpen, onDownload } = opts || {};
  const ext = extOf(file);
  const overlay = pvFrame();
  $('.pv-title', overlay).textContent = file;

  const tools = $('.pv-tools', overlay);
  if (onDiff && st !== 'D') {
    const b = document.createElement('button');
    b.className = 'pv-btn';
    b.textContent = '与上一版对比';
    b.onclick = () => { cleanup(); onDiff(); };
    tools.appendChild(b);
  }
  if (onOpen) {
    const b = document.createElement('button');
    b.className = 'pv-btn';
    b.textContent = '用本机程序打开';
    b.onclick = onOpen;
    tools.appendChild(b);
  }
  tools.appendChild(closeButton(overlay));

  const note = $('.pv-note', overlay);
  note.textContent = '正在读取…';

  try {
    if (st === 'D' && !oid) throw new Error('这个文件在工作区里已被删除');

    if (ext === '.md' || ext === '.markdown') {
      const text = await fetchText(path, file, oid);
      cur = renderMarkdown(overlay, text, file, path);
    } else if (ext === '.dxf') {
      cur = await renderDxf(overlay, await fetchBytes(path, file, oid), 'DXF');
    } else if (ext === '.dwg') {
      const r = await fetch(srcUrl(path, file, oid, '/api/dwg2dxf'));
      if (!r.ok) throw new Error('DWG 转换失败：HTTP ' + r.status);
      cur = await renderDxf(overlay, await r.arrayBuffer(), 'DWG（服务端已转 DXF）');
    } else if (['.step', '.stp', '.iges', '.igs', '.brep'].includes(ext)) {
      const buf = await fetchBytes(path, file, oid);
      cur = await renderStep(overlay, buf, ext.slice(1).toUpperCase());
    } else if (ext === '.stl') {
      const buf = await fetchBytes(path, file, oid);
      cur = await renderStl(overlay, buf, 'STL');
    } else if (['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.ico'].includes(ext)) {
      cur = renderImage(overlay, rawUrl(path, file, oid), ext.slice(1).toUpperCase());
    } else {
      const text = await fetchText(path, file, oid);
      const body = $('.pv-body', overlay);
      body.innerHTML = `<pre class="pv-raw">${escapeHtml(text.slice(0, 400000))}</pre>`;
      note.textContent = '文本预览（最多 40 万字）';
      cur = () => {};
    }
  } catch (e) {
    showError(overlay, e && e.message ? e.message : String(e));
    note.textContent = '';
  }
}

window.openFilePreview = openFilePreview;
window.closeFilePreview = cleanup;
