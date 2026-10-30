/* 3D 预览：ZGM1 网格 + three.js 渲染
 *
 * 网格由服务端用 SolidWorks 导出 3MF 后转成二进制：
 *   "ZGM1" | u32 顶点数 | u32 三角数 | f32 x,y,z * n | u32 i0,i1,i2 * m
 *
 * 渲染交给 three.js（MIT，已 vendor 到 web/vendor/three）：
 *   - PBR 材质 + 室内环境光照（RoomEnvironment）+ 色调映射
 *   - OrbitControls 旋转/缩放/平移，带阻尼
 *   - 改动顶点红色高亮（着色器里按顶点标志混合，边界清晰）
 *   - 上一版半透明叠影、剖切平面、折角边线、标准视角、存图
 * 差异判定仍是本文件里的精确点-三角形距离（带哈希网格加速）。
 */
import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';
import { RoomEnvironment } from './vendor/three/RoomEnvironment.js';

const MAGIC = 'ZGM1';
const HOT = 0xe6302a;

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ------------------------------------------------------------ 数据准备 */
function parseZgm(buf) {
  const dv = new DataView(buf);
  if (buf.byteLength < 12) throw new Error('网格数据不完整');
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== MAGIC) throw new Error('网格格式不认识');
  const nv = dv.getUint32(4, true);
  const nt = dv.getUint32(8, true);
  if (nv === 0 || nt === 0) throw new Error('网格是空的');
  const posOff = 12;
  const idxOff = posOff + nv * 12;
  if (buf.byteLength < idxOff + nt * 12) throw new Error('网格数据被截断');
  return {
    pos: new Float32Array(buf, posOff, nv * 3),
    idx: new Uint32Array(buf, idxOff, nt * 3),
    nverts: nv,
    ntris: nt,
    buffer: buf,
    flags: null,
  };
}

function bboxOf(mesh) {
  const p = mesh.pos;
  let x0 = Infinity, y0 = Infinity, z0 = Infinity;
  let x1 = -Infinity, y1 = -Infinity, z1 = -Infinity;
  for (let i = 0; i < p.length; i += 3) {
    const x = p[i], y = p[i + 1], z = p[i + 2];
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
    if (z < z0) z0 = z; if (z > z1) z1 = z;
  }
  if (!isFinite(x0)) { x0 = y0 = z0 = -1; x1 = y1 = z1 = 1; }
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, cz = (z0 + z1) / 2;
  const dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
  const r = Math.max(Math.sqrt(dx * dx + dy * dy + dz * dz) / 2, 1e-6);
  return { cx, cy, cz, r, dx, dy, dz, x0, y0, z0, x1, y1, z1 };
}

/* ------------------------------------------------------------ 改动判定
 * 新网格的顶点到旧表面（三角面）的精确距离超过容差，才算改过。
 * 只比“三角形重心”会把没改的面也标红：粗网格上一个顶点离最近重心往往
 * 好几毫米。这里用点到三角形的精确距离 + 哈希网格加速，几十万面也够快。 */
const GRID_MAX_CELLS = 256;

/* 点到三角形的最短距离平方（Ericson, Real-Time Collision Detection） */
function ptTri2(px, py, pz, ax, ay, az, bx, by, bz, cx, cy, cz) {
  const abx = bx - ax, aby = by - ay, abz = bz - az;
  const acx = cx - ax, acy = cy - ay, acz = cz - az;
  const apx = px - ax, apy = py - ay, apz = pz - az;
  const d1 = abx * apx + aby * apy + abz * apz;
  const d2 = acx * apx + acy * apy + acz * apz;
  if (d1 <= 0 && d2 <= 0) return apx * apx + apy * apy + apz * apz;
  const bpx = px - bx, bpy = py - by, bpz = pz - bz;
  const d3 = abx * bpx + aby * bpy + abz * bpz;
  const d4 = acx * bpx + acy * bpy + acz * bpz;
  if (d3 >= 0 && d4 <= d3) return bpx * bpx + bpy * bpy + bpz * bpz;
  const vc = d1 * d4 - d3 * d2;
  if (vc <= 0 && d1 >= 0 && d3 <= 0) {
    const v = d1 / (d1 - d3);
    const qx = apx - abx * v, qy = apy - aby * v, qz = apz - abz * v;
    return qx * qx + qy * qy + qz * qz;
  }
  const cpx = px - cx, cpy = py - cy, cpz = pz - cz;
  const d5 = abx * cpx + aby * cpy + abz * cpz;
  const d6 = acx * cpx + acy * cpy + acz * cpz;
  if (d6 >= 0 && d5 <= d6) return cpx * cpx + cpy * cpy + cpz * cpz;
  const vb = d5 * d2 - d1 * d6;
  if (vb <= 0 && d2 >= 0 && d6 <= 0) {
    const w = d2 / (d2 - d6);
    const qx = apx - acx * w, qy = apy - acy * w, qz = apz - acz * w;
    return qx * qx + qy * qy + qz * qz;
  }
  const va = d3 * d6 - d5 * d4;
  if (va <= 0 && d4 - d3 >= 0 && d5 - d6 >= 0) {
    const w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
    const rx = px - (bx + (cx - bx) * w);
    const ry = py - (by + (cy - by) * w);
    const rz = pz - (bz + (cz - bz) * w);
    return rx * rx + ry * ry + rz * rz;
  }
  const denom = 1 / (va + vb + vc);
  const v = vb * denom, w = vc * denom;
  const rx = px - (ax + abx * v + acx * w);
  const ry = py - (ay + aby * v + acy * w);
  const rz = pz - (az + abz * v + acz * w);
  return rx * rx + ry * ry + rz * rz;
}

function buildSurfaceIndex(mesh, tol) {
  const pos = mesh.pos, idx = mesh.idx;
  const nt = idx.length / 3;
  // 格子取“三角形平均尺寸”与 2 倍容差的较大者，保证查询只需看周围 27 格
  let sum = 0, cnt = 0;
  const step = Math.max(1, Math.floor(nt / 2000));
  for (let t = 0; t < nt; t += step) {
    const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
    const dx = Math.max(pos[a], pos[b], pos[c]) - Math.min(pos[a], pos[b], pos[c]);
    const dy = Math.max(pos[a + 1], pos[b + 1], pos[c + 1]) - Math.min(pos[a + 1], pos[b + 1], pos[c + 1]);
    const dz = Math.max(pos[a + 2], pos[b + 2], pos[c + 2]) - Math.min(pos[a + 2], pos[b + 2], pos[c + 2]);
    sum += Math.max(dx, dy, dz);
    cnt++;
  }
  const cell = Math.max(2 * tol, cnt ? sum / cnt : 0, 1e-6);
  const inv = 1 / cell;
  const ranges = new Int32Array(nt * 6);
  const isHuge = new Uint8Array(nt);        // 别拿 x1 当哨兵：x1 可能是负数
  const huge = [];
  let total = 0;
  for (let t = 0; t < nt; t++) {
    const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
    const x0 = Math.floor(Math.min(pos[a], pos[b], pos[c]) * inv);
    const y0 = Math.floor(Math.min(pos[a + 1], pos[b + 1], pos[c + 1]) * inv);
    const z0 = Math.floor(Math.min(pos[a + 2], pos[b + 2], pos[c + 2]) * inv);
    const x1 = Math.floor(Math.max(pos[a], pos[b], pos[c]) * inv);
    const y1 = Math.floor(Math.max(pos[a + 1], pos[b + 1], pos[c + 1]) * inv);
    const z1 = Math.floor(Math.max(pos[a + 2], pos[b + 2], pos[c + 2]) * inv);
    const span = (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1);
    ranges[t * 6] = x0; ranges[t * 6 + 1] = y0; ranges[t * 6 + 2] = z0;
    ranges[t * 6 + 3] = x1; ranges[t * 6 + 4] = y1; ranges[t * 6 + 5] = z1;
    if (span > GRID_MAX_CELLS) { isHuge[t] = 1; huge.push(t); }
    else total += span;
  }
  const nb = 1 << Math.ceil(Math.log2(Math.max(1024, nt * 4)));
  const head = new Int32Array(nb).fill(-1);
  const cellOf = new Int32Array(total * 3);
  const triOf = new Int32Array(total);
  const next = new Int32Array(total);
  const hash = (ix, iy, iz) =>
    (Math.imul(ix, 73856093) ^ Math.imul(iy, 19349663) ^ Math.imul(iz, 83492791)) & (nb - 1);
  let n = 0;
  for (let t = 0; t < nt; t++) {
    if (isHuge[t]) continue;
    const x0 = ranges[t * 6], y0 = ranges[t * 6 + 1], z0 = ranges[t * 6 + 2];
    const x1 = ranges[t * 6 + 3], y1 = ranges[t * 6 + 4], z1 = ranges[t * 6 + 5];
    for (let ix = x0; ix <= x1; ix++) {
      for (let iy = y0; iy <= y1; iy++) {
        for (let iz = z0; iz <= z1; iz++) {
          const h = hash(ix, iy, iz);
          cellOf[n * 3] = ix; cellOf[n * 3 + 1] = iy; cellOf[n * 3 + 2] = iz;
          triOf[n] = t; next[n] = head[h]; head[h] = n; n++;
        }
      }
    }
  }
  return { pos, idx, cell, inv, nb, head, cellOf, triOf, next, huge, hash };
}

function surfaceDist2(ix, x, y, z, tol2) {
  const { pos, idx, head, cellOf, triOf, next, huge, hash, inv } = ix;
  const bx = Math.floor(x * inv), by = Math.floor(y * inv), bz = Math.floor(z * inv);
  let best = Infinity;
  for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 1; dy++) {
      for (let dz = -1; dz <= 1; dz++) {
        const gx = bx + dx, gy = by + dy, gz = bz + dz;
        for (let k = head[hash(gx, gy, gz)]; k !== -1; k = next[k]) {
          if (cellOf[k * 3] !== gx || cellOf[k * 3 + 1] !== gy || cellOf[k * 3 + 2] !== gz) continue;
          const t = triOf[k];
          const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
          const d = ptTri2(x, y, z,
            pos[a], pos[a + 1], pos[a + 2],
            pos[b], pos[b + 1], pos[b + 2],
            pos[c], pos[c + 1], pos[c + 2]);
          if (d < best) { best = d; if (best <= tol2) return best; }
        }
      }
    }
  }
  for (let h = 0; h < huge.length; h++) {
    const t = huge[h];
    const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
    const d = ptTri2(x, y, z,
      pos[a], pos[a + 1], pos[a + 2],
      pos[b], pos[b + 1], pos[b + 2],
      pos[c], pos[c + 1], pos[c + 2]);
    if (d < best) { best = d; if (best <= tol2) return best; }
  }
  return best;
}

/* 只在“已经是候选”的点上调用：找最近的那个三角形，用来判内外 */
function nearestTri(ix, x, y, z) {
  const { pos, idx, head, cellOf, triOf, next, huge, hash, inv } = ix;
  const bx = Math.floor(x * inv), by = Math.floor(y * inv), bz = Math.floor(z * inv);
  let best = Infinity, bestT = -1;
  for (let dx = -1; dx <= 1; dx++) {
    for (let dy = -1; dy <= 1; dy++) {
      for (let dz = -1; dz <= 1; dz++) {
        const gx = bx + dx, gy = by + dy, gz = bz + dz;
        for (let k = head[hash(gx, gy, gz)]; k !== -1; k = next[k]) {
          if (cellOf[k * 3] !== gx || cellOf[k * 3 + 1] !== gy || cellOf[k * 3 + 2] !== gz) continue;
          const t = triOf[k];
          const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
          const d = ptTri2(x, y, z,
            pos[a], pos[a + 1], pos[a + 2],
            pos[b], pos[b + 1], pos[b + 2],
            pos[c], pos[c + 1], pos[c + 2]);
          if (d < best) { best = d; bestT = t; }
        }
      }
    }
  }
  for (let h = 0; h < huge.length; h++) {
    const t = huge[h];
    const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
    const d = ptTri2(x, y, z,
      pos[a], pos[a + 1], pos[a + 2],
      pos[b], pos[b + 1], pos[b + 2],
      pos[c], pos[c + 1], pos[c + 2]);
    if (d < best) { best = d; bestT = t; }
  }
  return { d2: best, t: bestT };
}

/* 点是否在这块表面的外侧（用最近三角形的法向判）：
 * 旧表面若被新实体包住，说明只是“材料变厚把旧面盖住了”，不算删除。 */
function outsideOf(mesh, t, x, y, z) {
  if (t < 0) return true;
  const { pos, idx } = mesh;
  const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, c = idx[t * 3 + 2] * 3;
  const abx = pos[b] - pos[a], aby = pos[b + 1] - pos[a + 1], abz = pos[b + 2] - pos[a + 2];
  const acx = pos[c] - pos[a], acy = pos[c + 1] - pos[a + 1], acz = pos[c + 2] - pos[a + 2];
  const nx = aby * acz - abz * acy;
  const ny = abz * acx - abx * acz;
  const nz = abx * acy - aby * acx;
  const len = Math.sqrt(nx * nx + ny * ny + nz * nz);
  if (!len) return true;
  const dot = ((x - pos[a]) * nx + (y - pos[a + 1]) * ny + (z - pos[a + 2]) * nz) / len;
  return dot > 0;
}

function markChanged(mesh, other, tol) {
  const index = buildSurfaceIndex(other, Math.max(tol, 1e-6));
  const { pos } = mesh;
  const flags = new Float32Array(mesh.nverts);
  const tol2 = tol * tol;
  let changed = 0;
  for (let v = 0, i = 0; i < pos.length; v++, i += 3) {
    if (surfaceDist2(index, pos[i], pos[i + 1], pos[i + 2], tol2) > tol2) {
      flags[v] = 1;
      changed++;
    }
  }
  mesh.flags = flags;
  return changed;
}

/* 反向：上一版有、这一版没有的材料。只比新网格顶点会漏掉“整块被删”的区域，
 * 所以还要拿上一版的顶点去量新表面。 */
function markRemoved(oldMesh, newMesh, tol) {
  const index = buildSurfaceIndex(newMesh, Math.max(tol, 1e-6));
  const { pos } = oldMesh;
  const flags = new Float32Array(oldMesh.nverts);
  const tol2 = tol * tol;
  let removed = 0;
  for (let v = 0, i = 0; i < pos.length; v++, i += 3) {
    const x = pos[i], y = pos[i + 1], z = pos[i + 2];
    if (surfaceDist2(index, x, y, z, tol2) <= tol2) continue;
    // 远了还要看方向：被新实体包住的旧内表面不是“删掉的材料”
    const near = nearestTri(index, x, y, z);
    if (!outsideOf(newMesh, near.t, x, y, z)) continue;
    flags[v] = 1;
    removed++;
  }
  oldMesh.flags = flags;
  return removed;
}

/* 只保留标记够数的三角形，做成一块独立的覆盖层。
 * 改动区（红）默认要求三个顶点都标记，边界不外溢；
 * 被删材料（蓝）用 need=1：删掉的特征往往只有一圈边界顶点远离新表面，
 * 要求三个都标记会一个三角形都留不下（2026-10-24 实测：124 个顶点 → 0 个面）。 */
function flaggedGeometry(mesh, flags, need) {
  const min = need || 3;
  const pos = mesh.pos, idx = mesh.idx;
  const nt = idx.length / 3;
  const used = new Int32Array(mesh.nverts).fill(-1);
  const outIdx = [];
  const outPos = [];
  for (let t = 0; t < nt; t++) {
    const a = idx[t * 3], b = idx[t * 3 + 1], c = idx[t * 3 + 2];
    let hit = 0;
    if (flags[a]) hit++;
    if (flags[b]) hit++;
    if (flags[c]) hit++;
    if (hit < min) continue;
    const tri = [];
    for (const v of [a, b, c]) {
      if (used[v] < 0) {
        used[v] = outPos.length / 3;
        outPos.push(pos[v * 3], pos[v * 3 + 1], pos[v * 3 + 2]);
      }
      tri.push(used[v]);
    }
    outIdx.push(tri[0], tri[1], tri[2]);
  }
  if (!outIdx.length) return null;
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(outPos), 3));
  geo.setIndex(new THREE.BufferAttribute(new Uint32Array(outIdx), 1));
  geo.computeVertexNormals();
  geo.computeBoundingSphere();
  return geo;
}

/* ------------------------------------------------------------ three.js 渲染器 */
const EDGE_LIMIT = 300000;      // 超过这个面数不生成折角边线（太慢）
const AUTO_EDGE_LIMIT = 150000; // 这个面数以内默认开边线：薄板靠光影分不清面

function createViewer(canvas) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas, antialias: true, alpha: true, preserveDrawingBuffer: true,
      powerPreference: 'high-performance',
    });
  } catch (e) {
    throw new Error('这个浏览器不能显示 3D（WebGL2 初始化失败）。换新版 Edge / Chrome 再试，或先看图纸。');
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.localClippingEnabled = true;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 1000);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.085;
  controls.rotateSpeed = 0.85;
  controls.zoomSpeed = 0.9;
  controls.panSpeed = 0.8;
  controls.screenSpacePanning = true;

  /* 室内环境光照：比纯手写着色器更能体现曲面和金属质感 */
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.55;
  pmrem.dispose();

  scene.add(new THREE.HemisphereLight(0xffffff, 0x46536b, 0.32));
  const key = new THREE.DirectionalLight(0xffffff, 1.9);
  key.position.set(1.2, 1.8, 1.3);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  scene.add(key);
  scene.add(key.target);
  const fill = new THREE.DirectionalLight(0xdde8ff, 0.5);
  fill.position.set(-1.4, 0.7, -0.8);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.7);
  rim.position.set(-0.3, -1.0, 1.1);
  scene.add(rim);

  /* 模型坐标是 Z 向上（SolidWorks / 3MF），转成 three 的 Y 向上 */
  const group = new THREE.Group();
  group.rotation.x = -Math.PI / 2;
  scene.add(group);

  const hotColor = new THREE.Color(HOT);
  const mainMat = new THREE.MeshStandardMaterial({
    color: 0x9fadc2, metalness: 0.26, roughness: 0.44, side: THREE.DoubleSide,
  });
  mainMat.customProgramCacheKey = () => 'zgr-main-flag';
  mainMat.onBeforeCompile = (sh) => {
    sh.uniforms.uHot = { value: hotColor };
    sh.uniforms.uFlagOn = { value: 1.0 };
    sh.vertexShader = 'attribute float aFlag;\nvarying float vFlag;\n' + sh.vertexShader
      .replace('#include <begin_vertex>', '#include <begin_vertex>\n  vFlag = aFlag;');
    sh.fragmentShader = 'varying float vFlag;\nuniform vec3 uHot;\nuniform float uFlagOn;\n'
      + sh.fragmentShader
        .replace('#include <color_fragment>',
          '#include <color_fragment>\n  float zgrHot = step(0.5, vFlag) * uFlagOn;\n'
          + '  diffuseColor.rgb = mix(diffuseColor.rgb, uHot, zgrHot);')
        .replace('#include <emissivemap_fragment>',
          '#include <emissivemap_fragment>\n  totalEmissiveRadiance += uHot * zgrHot * 0.22;');
    mainMat.userData.shader = sh;
  };
  const ghostMat = new THREE.MeshStandardMaterial({
    color: 0x8892a2, metalness: 0.05, roughness: 0.9,
    transparent: true, opacity: 0.17, depthWrite: false, side: THREE.DoubleSide,
  });
  /* 被删掉的材料：蓝色半透明，和「改动」的红色区分开 */
  const removedMat = new THREE.MeshStandardMaterial({
    color: 0x2f6fdf, emissive: 0x123c8a, emissiveIntensity: 0.25,
    metalness: 0.1, roughness: 0.75, transparent: true, opacity: 0.78,
    depthWrite: false, side: THREE.DoubleSide,
  });
  const edgeMat = new THREE.LineBasicMaterial({ color: 0x2b3644, transparent: true, opacity: 0.5 });

  const clipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1e9);
  mainMat.clippingPlanes = [clipPlane];
  ghostMat.clippingPlanes = [clipPlane];
  removedMat.clippingPlanes = [clipPlane];
  edgeMat.clippingPlanes = [clipPlane];

  let mainMesh = null, ghostMesh = null;
  let mainGeo = null, ghostGeo = null, mainObj = null, ghostObj = null, removedObj = null;
  let edgeObj = null, shadowObj = null, contactObj = null;
  const overlayObjs = [];
  let flagAttr = null;
  let flags = null;
  let box = null;
  let minY = 0, maxY = 1, radius = 1;
  let dirty = true;

  /* 历史版本叠放：每一版一个半透明彩色网格，共用主版本的坐标系 */
  function clearOverlays() {
    while (overlayObjs.length) {
      const o = overlayObjs.pop();
      group.remove(o);
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    }
  }

  function setOverlays(list) {
    clearOverlays();
    (list || []).forEach((it) => {
      if (!it || !it.mesh) return;
      let geo = null;
      try {
        geo = buildGeometry(it.mesh, false);
        const mat = new THREE.MeshStandardMaterial({
          color: it.color != null ? it.color : 0x2f6fdf,
          metalness: 0.05, roughness: 0.88,
          transparent: true, opacity: it.opacity != null ? it.opacity : 0.34,
          depthWrite: false, side: THREE.DoubleSide,
        });
        mat.clippingPlanes = [clipPlane];
        const obj = new THREE.Mesh(geo, mat);
        obj.frustumCulled = false;
        obj.renderOrder = 3;
        group.add(obj);
        overlayObjs.push(obj);
      } catch (e) {
        if (geo) geo.dispose();
      }
    });
    dirty = true;
  }

  function disposeObject(obj) {
    if (!obj) return;
    group.remove(obj);
    if (obj.geometry) obj.geometry.dispose();
  }

  /* 接触阴影自带贴图，得连材质一起放掉 */
  function disposeContact() {
    if (!contactObj) return;
    group.remove(contactObj);
    contactObj.geometry.dispose();
    if (contactObj.material.map) contactObj.material.map.dispose();
    contactObj.material.dispose();
    contactObj = null;
  }

  function buildGeometry(mesh, withFlags) {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(mesh.pos, 3));
    geo.setIndex(new THREE.BufferAttribute(mesh.idx, 1));
    geo.computeVertexNormals();
    if (withFlags) {
      const f = flags || new Float32Array(mesh.nverts);
      flagAttr = new THREE.BufferAttribute(f, 1);
      geo.setAttribute('aFlag', flagAttr);
    }
    geo.computeBoundingBox();
    geo.computeBoundingSphere();
    return geo;
  }

  /* 台面：真实投影（高的装配体用得上）+ 一层柔和接触阴影（薄板贴地时只有它看得见） */
  const shadowMat = new THREE.ShadowMaterial({ opacity: 0.26, color: 0x101827 });

  function groundShadow(bb) {
    const size = Math.max(bb.dx, bb.dy, 1) * 4;
    const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), shadowMat);
    m.position.set(bb.cx, bb.cy, bb.z0 - bb.r * 0.002);
    m.receiveShadow = true;
    m.renderOrder = -1;
    return m;
  }

  function contactShadow(bb) {
    const c = document.createElement('canvas');
    c.width = c.height = 256;
    const g = c.getContext('2d');
    const rg = g.createRadialGradient(128, 128, 8, 128, 128, 126);
    rg.addColorStop(0, 'rgba(16,24,39,0.36)');
    rg.addColorStop(0.5, 'rgba(16,24,39,0.14)');
    rg.addColorStop(1, 'rgba(16,24,39,0)');
    g.fillStyle = rg;
    g.fillRect(0, 0, 256, 256);
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    // 跟着零件长宽比拉伸，细长件的影子才是长条而不是一团圆
    const sx = Math.max(bb.dx, 1) * 1.5;
    const sy = Math.max(bb.dy, 1) * 1.5;
    const m = new THREE.Mesh(
      new THREE.PlaneGeometry(sx, sy),
      new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }),
    );
    m.position.set(bb.cx, bb.cy, bb.z0 - bb.r * 0.0008);
    m.renderOrder = -1;
    return m;
  }

  function load(o) {
    const main = o.main;
    const ghost = o.ghost || null;
    const removed = o.removed || null;
    disposeObject(edgeObj); edgeObj = null;
    disposeObject(mainObj); mainObj = null;
    disposeObject(ghostObj); ghostObj = null;
    disposeObject(removedObj); removedObj = null;
    disposeObject(shadowObj); shadowObj = null;
    disposeContact();
    clearOverlays();
    if (mainGeo) { mainGeo.dispose(); mainGeo = null; }
    if (ghostGeo) { ghostGeo.dispose(); ghostGeo = null; }
    mainMesh = main;
    ghostMesh = ghost;
    flags = o.flags || null;

    mainGeo = buildGeometry(main, true);
    mainObj = new THREE.Mesh(mainGeo, mainMat);
    mainObj.frustumCulled = false;
    mainObj.castShadow = true;
    group.add(mainObj);

    if (ghost) {
      ghostGeo = buildGeometry(ghost, false);
      ghostObj = new THREE.Mesh(ghostGeo, ghostMat);
      ghostObj.frustumCulled = false;
      ghostObj.renderOrder = 2;
      group.add(ghostObj);
    }
    if (removed && removed.flags) {
      const rgeo = flaggedGeometry(removed, removed.flags, 1);
      if (rgeo) {
        removedObj = new THREE.Mesh(rgeo, removedMat);
        removedObj.frustumCulled = false;
        removedObj.renderOrder = 1;
        group.add(removedObj);
      }
    }

    const bb = bboxOf(main);
    box = bb;
    radius = bb.r;
    shadowObj = groundShadow(bb);
    group.add(shadowObj);
    contactObj = contactShadow(bb);
    group.add(contactObj);

    /* 主光跟着模型走：阴影相机要正好罩住它，否则要么没影子要么糊成一团 */
    const cw = new THREE.Vector3(bb.cx, bb.cz, -bb.cy);
    key.position.copy(cw).addScaledVector(new THREE.Vector3(1.2, 1.8, 1.3).normalize(), bb.r * 4);
    key.target.position.copy(cw);
    key.target.updateMatrixWorld();
    const sc = bb.r * 1.45;
    const sc2 = key.shadow.camera;
    sc2.left = -sc; sc2.right = sc; sc2.top = sc; sc2.bottom = -sc;
    sc2.near = Math.max(bb.r * 0.05, 0.01);
    sc2.far = bb.r * 12;
    sc2.updateProjectionMatrix();
    key.shadow.bias = -bb.r * 0.0006;
    key.shadow.normalBias = bb.r * 0.004;

    // 世界坐标里：模型 Z → three Y
    minY = bb.z0;
    maxY = bb.z1;
    clipPlane.constant = maxY + bb.r;    // 默认不切

    controls.minDistance = radius * 0.15;
    controls.maxDistance = radius * 30;
    frame(null, true);
  }

  function frame(dir, instant) {
    if (!box) return;
    const d = (dir || new THREE.Vector3(1.0, 0.85, 1.15)).clone().normalize();
    const center = new THREE.Vector3(box.cx, box.cz, -box.cy);
    /* 按包围盒在屏幕上的实际投影取景：薄板前视/俯视不会被球半径撑得很小 */
    const ax = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), d);
    if (ax.lengthSq() < 1e-8) ax.set(1, 0, 0);
    ax.normalize();
    const ay = new THREE.Vector3().crossVectors(d, ax).normalize();
    const hx = Math.max(box.dx, 1e-6) / 2;
    const hy = Math.max(box.dz, 1e-6) / 2;
    const hz = Math.max(box.dy, 1e-6) / 2;
    let halfW = 0, halfH = 0, halfD = 0;
    for (let sx = -1; sx <= 1; sx += 2) {
      for (let sy = -1; sy <= 1; sy += 2) {
        for (let sz = -1; sz <= 1; sz += 2) {
          const c = new THREE.Vector3(hx * sx, hy * sy, hz * sz);
          halfW = Math.max(halfW, Math.abs(c.dot(ax)));
          halfH = Math.max(halfH, Math.abs(c.dot(ay)));
          halfD = Math.max(halfD, Math.abs(c.dot(d)));
        }
      }
    }
    const tanV = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const aspect = Math.max(camera.aspect || 1, 0.25);
    const dist = Math.max(halfH / tanV, halfW / (tanV * aspect)) * 1.1 + halfD;
    camera.near = Math.max(radius / 2000, 0.001);
    camera.far = Math.max(radius * 200, 10);
    camera.position.copy(center).addScaledVector(d, dist);
    camera.up.set(0, 1, 0);
    camera.lookAt(center);
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
    dirty = true;
  }

  const VIEWS = {
    iso: [1.0, 0.85, 1.15],
    front: [0, 0.12, 1.0],
    top: [0, 1.0, 0.001],
    right: [1.0, 0.12, 0],
  };

  function setView(name) {
    const v = VIEWS[name] || VIEWS.iso;
    frame(new THREE.Vector3(v[0], v[1], v[2]));
  }

  function setFlags(next) {
    flags = next;
    if (!flagAttr) return;
    flagAttr.array.set(next);
    flagAttr.needsUpdate = true;
    dirty = true;
  }

  /* 只重建蓝色删除层，不动相机、不动主网格（调容差时用） */
  function setRemoved(mesh) {
    disposeObject(removedObj);
    removedObj = null;
    if (mesh && mesh.flags) {
      const g = flaggedGeometry(mesh, mesh.flags, 1);
      if (g) {
        removedObj = new THREE.Mesh(g, removedMat);
        removedObj.frustumCulled = false;
        removedObj.renderOrder = 1;
        group.add(removedObj);
      }
    }
    dirty = true;
  }

  function setGhostVisible(on) {
    if (ghostObj) ghostObj.visible = !!on;
    dirty = true;
  }

  function setRemovedVisible(on) {
    if (removedObj) removedObj.visible = !!on;
    dirty = true;
  }

  function setRedVisible(on) {
    const sh = mainMat.userData.shader;
    if (sh && sh.uniforms.uFlagOn) { sh.uniforms.uFlagOn.value = on ? 1 : 0; dirty = true; }
  }

  /* 剖切：0..100，100 = 不切。沿模型 Z 轴从上往下切 */
  function setClip(pct) {
    if (!box) return;
    const p = Math.max(0, Math.min(100, pct));
    if (p >= 99.9) clipPlane.constant = maxY + box.r;
    else clipPlane.constant = minY + (maxY - minY) * (p / 100);
    dirty = true;
  }

  function setEdges(on) {
    if (!mainObj) return { ok: false, reason: '还没有模型' };
    if (!on) {
      if (edgeObj) edgeObj.visible = false;
      dirty = true;
      return { ok: true };
    }
    if (!edgeObj) {
      if (mainMesh.ntris > EDGE_LIMIT) {
        return { ok: false, reason: `面数 ${mainMesh.ntris.toLocaleString()} 超过 ${EDGE_LIMIT.toLocaleString()}，边线太慢，已跳过` };
      }
      const eg = new THREE.EdgesGeometry(mainGeo, 32);
      edgeObj = new THREE.LineSegments(eg, edgeMat);
      edgeObj.frustumCulled = false;
      edgeObj.renderOrder = 1;
      group.add(edgeObj);
    }
    edgeObj.visible = true;
    dirty = true;
    return { ok: true };
  }

  function screenshot() {
    renderer.render(scene, camera);
    return renderer.domElement.toDataURL('image/png');
  }

  function resize() {
    const w = Math.max(1, canvas.clientWidth | 0);
    const h = Math.max(1, canvas.clientHeight | 0);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    dirty = true;
  }
  const ro = new ResizeObserver(resize);
  if (canvas.parentElement) ro.observe(canvas.parentElement);
  resize();

  let alive = true;
  function tick() {
    if (!alive) return;
    requestAnimationFrame(tick);
    const moved = controls.update();
    if (moved || dirty) {
      dirty = false;
      renderer.render(scene, camera);
    }
  }
  requestAnimationFrame(tick);

  function dispose() {
    alive = false;
    ro.disconnect();
    controls.dispose();
    clearOverlays();
    disposeObject(edgeObj); edgeObj = null;
    disposeObject(mainObj); mainObj = null;
    disposeObject(ghostObj); ghostObj = null;
    disposeObject(removedObj); removedObj = null;
    disposeObject(shadowObj); shadowObj = null;
    disposeContact();
    if (mainGeo) mainGeo.dispose();
    if (ghostGeo) ghostGeo.dispose();
    mainMat.dispose(); ghostMat.dispose(); removedMat.dispose();
    edgeMat.dispose(); shadowMat.dispose();
    scene.environment && scene.environment.dispose && scene.environment.dispose();
    renderer.dispose();
  }

  return {
    load, setFlags, setRemoved, setGhostVisible, setRemovedVisible, setRedVisible, setClip, setEdges, setView,
    setOverlays,
    resetView: () => frame(null), screenshot, dispose, resize,
  };
}

/* ------------------------------------------------------------ 弹窗 */
function apiBase(q) {
  const p = new URLSearchParams();
  Object.keys(q).forEach((k) => { if (q[k] != null && q[k] !== '') p.set(k, q[k]); });
  return '/api/mesh?' + p.toString();
}

function humanSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

async function fetchMesh(o) {
  const res = await fetch(apiBase(o), { cache: o.force ? 'no-store' : 'default' });
  if (!res.ok) {
    let msg = 'HTTP ' + res.status;
    try { const j = await res.json(); if (j.error) msg = j.error; } catch (e) { /* 非 JSON */ }
    throw new Error(msg);
  }
  const buf = await res.arrayBuffer();
  const mesh = parseZgm(buf);
  mesh.tris = Number(res.headers.get('X-ZGR-Tris') || mesh.ntris);
  mesh.objects = Number(res.headers.get('X-ZGR-Objects') || 0);
  mesh.source = res.headers.get('X-ZGR-Source') || '';
  try {
    mesh.missing = (res.headers.get('X-ZGR-Missing') || '')
      .split(',').filter(Boolean).map(decodeURIComponent);
  } catch (e) {
    mesh.missing = [];
  }
  return mesh;
}

async function openMeshViewer(opts) {
  const { path, file, before, after, commit, poster } = opts;
  const body = document.createElement('div');
  body.className = 'mesh-body';
  body.innerHTML = `
    <div class="mesh-stage">
      ${poster ? `<img class="mesh-poster" src="${esc(poster)}" alt="">` : ''}
      <canvas></canvas>
      <div class="mesh-tools">
        <button data-v="iso" class="on">等轴测</button>
        <button data-v="front">前视</button>
        <button data-v="top">俯视</button>
        <button data-v="right">右视</button>
      </div>
      <div class="mesh-shot"><button class="m-shot" title="把当前画面存成 PNG">存图</button></div>
      <div class="mesh-load"><div class="mesh-spin"></div><span>准备 3D 模型…</span></div>
    </div>
    <div class="mesh-side">
      <div class="mesh-line"><b class="mesh-name"></b><span class="mesh-meta muted small"></span></div>
      <div class="mesh-note small"></div>
      <div class="mesh-versions" hidden>
        <div class="mv-head">
          <b>历史版本</b>
          <span class="muted small mv-count"></span>
        </div>
        <div class="mv-list"></div>
        <div class="mv-legend muted small">点一行切换主版本；勾选多个版本可叠放在一起对比</div>
      </div>
      <label class="mesh-check m-ghostrow"><input type="checkbox" class="m-ghost" checked>叠加显示上一版（灰色）</label>
      <label class="mesh-check m-redrow"><input type="checkbox" class="m-red" checked>红色标出改动区域</label>
      <label class="mesh-check m-remrow"><input type="checkbox" class="m-removed" checked>蓝色标出被删掉的材料</label>
      <label class="mesh-check"><input type="checkbox" class="m-edge">显示折角边线</label>
      <label class="mesh-row m-tolrow"><span>容差</span><input type="range" class="m-tol" min="1" max="40" value="4"><b class="m-tolv">0.4%</b></label>
      <label class="mesh-row m-cutrow"><span>剖切</span><input type="range" class="m-cut" min="0" max="100" value="100"><b class="m-cutv">关</b></label>
      <div class="mesh-legend">
        <span><i class="lg-new"></i>改动</span>
        <span><i class="lg-del"></i>删除</span>
        <span><i class="lg-same"></i>未改</span>
        <span><i class="lg-old"></i>上一版</span>
      </div>
      <div class="mv-stack small"></div>
      <div class="mesh-tip muted small">左键拖动旋转 · 滚轮缩放 · 右键 / Shift 拖动平移</div>
    </div>`;
  const m = window.openModal({
    title: file + ' · 3D 预览', body, width: 'min(1320px, 96vw)',
    footer: '<button class="m-2d">看图</button><button class="m-reset">重置视角</button><button class="cancel">关闭</button>',
  });
  m.el.classList.add('mesh-modal');

  const canvas = $('canvas', body);
  const loadEl = $('.mesh-load', body);
  let loadTxt = $('.mesh-load span', body);
  const note = $('.mesh-note', body);
  const listEl = $('.mv-list', body);
  const versionsBox = $('.mesh-versions', body);
  const stackEl = $('.mv-stack', body);
  let viewer = null;
  try {
    viewer = createViewer(canvas);
  } catch (e) {
    loadEl.innerHTML = '<span class="mesh-err">' + esc(e.message) + '</span>';
    return;
  }

  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    prefetchStop = true;
    try { viewer.dispose(); } catch (e) { /* 已经没了 */ }
  };
  const origClose = m.close;
  m.close = () => { dispose(); origClose(); };
  $('.cancel', m.foot).onclick = m.close;
  $('.m-2d', m.foot).onclick = () => {
    if (typeof window.showThumbCompare === 'function') window.showThumbCompare(file, before, after);
  };
  $('.m-reset', m.foot).onclick = () => viewer.resetView();
  /* 点右上角 X 或遮罩关闭时也要释放 GPU 资源 */
  const root = document.getElementById('modalRoot');
  const mo = new MutationObserver(() => {
    if (!document.body.contains(m.el)) { dispose(); mo.disconnect(); }
  });
  if (root) mo.observe(root, { childList: true });

  $('.m-shot', body).onclick = () => {
    const a = document.createElement('a');
    a.download = String(file).replace(/[\\/:*?"<>|]+/g, '_') + '-3D.png';
    a.href = viewer.screenshot();
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  /* ---------------------------------------------------------- 版本数据 */
  const COLORS = [0xe6302a, 0x2f6fdf, 0xf0a030, 0x2eb872, 0x9b59d0, 0x00a3a3, 0xd6336c, 0x7a869a];
  let versions = [];
  let histTruncated = false;
  let baseIdx = -1;
  let prevIdx = -1;
  let baseMesh = null;
  let prevMesh = null;
  let changed = 0;
  let removed = 0;
  let diag = 1;
  let loadSeq = 0;
  let prefetchStop = false;
  let prefetchToken = 0;
  const cache = new Map();          // oid -> mesh
  const rowState = new Map();       // index -> '' | loading | cached | err
  const overlaySel = new Set();     // 需要叠放的版本下标

  /* 生成请求排队：SolidWorks 不能同时导两个模型 */
  let queue = Promise.resolve();
  function enqueue(task) {
    const run = queue.then(task, task);
    queue = run.then(() => {}, () => {});
    return run;
  }

  function colorOf(i) { return COLORS[i % COLORS.length]; }
  function hex(c) { return '#' + Number(c).toString(16).padStart(6, '0'); }
  function fmtWhen(ms) {
    if (!ms) return '更早版本';
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    const now = new Date();
    const t = p(d.getHours()) + ':' + p(d.getMinutes());
    return d.toDateString() === now.toDateString()
      ? '今天 ' + t
      : d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + t;
  }
  function shortMsg(s) {
    s = String(s || '').replace(/\s+/g, ' ').trim();
    return s.length > 34 ? s.slice(0, 34) + '…' : s;
  }
  function showLoad(msg) {
    /* 上一次失败时 loadEl 里是错误文字，重试前要把转圈恢复回来 */
    if (!loadTxt || !document.body.contains(loadTxt)) {
      loadEl.innerHTML = '<div class="mesh-spin"></div><span></span>';
      loadTxt = $('.mesh-load span', body);
    }
    loadTxt.textContent = msg;
    loadEl.style.display = '';
  }
  function hideLoad() { loadEl.style.display = 'none'; }
  function failLoad(msg) {
    loadEl.style.display = '';
    loadEl.innerHTML = '<span class="mesh-err">' + esc(msg) + '</span>';
  }

  /* 历史版本都缓存在内存里，点哪版都不要再等 SolidWorks；
     但大装配体一个网格可能上百 MB，缓存超过 512 MB 就从最旧的开始丢 */
  const CACHE_LIMIT = 512 * 1024 * 1024;
  function cachePut(oid, mesh) {
    cache.set(oid, mesh);
    let total = 0;
    cache.forEach((m) => { total += (m.buffer && m.buffer.byteLength) || 0; });
    if (total <= CACHE_LIMIT) return;
    for (const [k, m] of cache) {
      if (total <= CACHE_LIMIT * 0.75) break;
      const idx = versions.findIndex((x) => x.oid === k);
      if (k === oid || idx === baseIdx || overlaySel.has(idx)) continue;
      total -= (m.buffer && m.buffer.byteLength) || 0;
      cache.delete(k);
      rowState.delete(idx);
    }
  }

  async function getMesh(v) {
    if (cache.has(v.oid)) return cache.get(v.oid);
    const mesh = await fetchMesh({ path, file, oid: v.oid, commit: v.commit || '' });
    cachePut(v.oid, mesh);
    return mesh;
  }

  function renderList() {
    if (versions.length < 2) { versionsBox.hidden = true; return; }
    versionsBox.hidden = false;
    $('.mv-count', body).textContent = histTruncated
      ? '最近 ' + versions.length + ' 个（更早的省略）'
      : '共 ' + versions.length + ' 个';
    listEl.innerHTML = versions.map((v, i) => {
      const on = i === baseIdx;
      const st = rowState.get(i) || '';
      const mark = st === 'loading' ? '生成中…' : (st === 'cached' ? '已就绪' : (st === 'err' ? '生成失败' : ''));
      return `<div class="mv-row${on ? ' on' : ''}" data-i="${i}">
        <input type="checkbox" class="mv-ov"${overlaySel.has(i) ? ' checked' : ''}${on ? ' disabled' : ''}
          title="${on ? '主版本已单独显示' : '和主版本叠放对比'}">
        <i class="mv-dot" style="background:${hex(colorOf(i))}"></i>
        <div class="mv-txt">
          <div class="mv-top"><b>${esc(fmtWhen(v.time))}</b>
            <span class="mv-who">${esc(v.author || '')}</span>
            ${on ? '<em class="mv-badge">主</em>' : ''}</div>
          <div class="mv-sub">${v.size ? humanSize(v.size) + ' · ' : ''}${esc(String(v.commit || '').slice(0, 8))}${v.message ? ' · ' + esc(shortMsg(v.message)) : ''}</div>
        </div>
        <span class="mv-state${st ? ' ' + st : ''}"><span>${mark}</span>
          <button class="mv-restore" title="把工作区里的这个文件恢复成这一版（先备份现有文件）">恢复</button>
        </span>
      </div>`;
    }).join('');
  }

  function renderStack() {
    const items = [...overlaySel].sort((a, b) => a - b)
      .filter((i) => i !== baseIdx && cache.has(versions[i].oid));
    if (!items.length) {
      stackEl.innerHTML = '';
      return;
    }
    stackEl.innerHTML = '已叠放：' + items.map((i) => `<span class="mv-chip"><i style="background:${hex(colorOf(i))}"></i>${esc(fmtWhen(versions[i].time))}</span>`).join('');
  }

  function applyOverlays() {
    const list = [];
    [...overlaySel].sort((a, b) => a - b).forEach((i) => {
      if (i === baseIdx) return;
      const mesh = cache.get(versions[i].oid);
      if (mesh) list.push({ mesh, color: colorOf(i), opacity: 0.36 });
    });
    viewer.setOverlays(list);
    renderStack();
  }

  function syncDiffControls(hasPrev) {
    for (const sel of ['.m-ghostrow', '.m-redrow', '.m-remrow', '.m-tolrow', '.m-cutrow']) {
      const el = $(sel, body);
      if (el) el.style.display = hasPrev || sel === '.m-cutrow' ? '' : 'none';
    }
    const lg = $('.mesh-legend', body);
    if (lg) lg.style.display = hasPrev ? '' : 'none';
    $('.m-tol', body).disabled = !hasPrev;
  }

  function paint() {
    if (!baseMesh) return;
    const hasPrev = !!prevMesh;
    const ghost = $('.m-ghost', body).checked && hasPrev;
    const red = $('.m-red', body).checked;
    const rem = $('.m-removed', body).checked && hasPrev;
    viewer.setGhostVisible(ghost);
    viewer.setRemovedVisible(rem);
    viewer.setRedVisible(red);

    let meta = `${baseMesh.ntris.toLocaleString()} 面 · ${baseMesh.nverts.toLocaleString()} 顶点`
      + ` · ${humanSize(baseMesh.buffer.byteLength)}`;
    if (baseMesh.objects > 1) meta += ` · ${baseMesh.objects.toLocaleString()} 个实体`;
    if (String(baseMesh.source || '').startsWith('peer:')) {
      meta += ` · 网格来自 ${baseMesh.source.slice(5)}`;
    }
    $('.mesh-meta', body).textContent = meta;

    if (hasPrev) {
      const parts = [];
      if (changed) parts.push(`<b style="color:#d93a2b">红色 = 改动</b>（${changed.toLocaleString()} 个顶点）`);
      if (removed) parts.push(`<b style="color:#2f6fdf">蓝色 = 被删掉的材料</b>（${removed.toLocaleString()} 个顶点）`);
      const pv = versions[prevIdx] || {};
      note.innerHTML = (parts.length ? parts.join(' · ')
        : '<b style="color:#1f8a4c">与上一版完全重合</b>，没有检测到改动')
        + `<div class="muted small" style="margin-top:3px">对比基准：${esc(fmtWhen(pv.time))}${pv.author ? ' · ' + esc(pv.author) : ''}</div>`;
    } else {
      note.innerHTML = '这是能查到的第一个版本，没有更早的版本可对比。'
        + (overlaySel.size ? '' : '<div class="muted small">可以勾选其它版本，把几个版本叠放在一起看。</div>');
    }
    if (baseMesh.missing && baseMesh.missing.length) {
      note.innerHTML += `<div style="color:#b26a00;margin-top:4px">⚠ 同目录缺少 ${baseMesh.missing.length} 个引用件：`
        + esc(baseMesh.missing.join('、')) + '。SolidWorks 可能只导出了能找到的部分。</div>';
    }
    renderStack();
  }

  async function setBase(i, opts2) {
    if (i < 0 || i >= versions.length) return;
    /* 已经加载好的同一版不重复加载；但如果上一版加载失败，点它可以重试 */
    if (i === baseIdx && baseMesh) return;
    const seq = ++loadSeq;
    prefetchStop = false;
    const v = versions[i];
    /* 这一版打不开时，别把已经看好的那一版也弄丢 */
    const keepBase = baseIdx, keepMesh = baseMesh;
    const keepPrevIdx = prevIdx, keepPrevMesh = prevMesh;
    baseIdx = i;             // 先标上：失败时用户也能看出点的是哪一版
    showLoad(`正在加载 ${fmtWhen(v.time)} 的模型…（首次生成需要几秒，之后秒开）`);
    let mesh = null;
    try {
      rowState.set(i, 'loading');
      renderList();
      mesh = await enqueue(() => getMesh(v));
      rowState.set(i, 'cached');
    } catch (e) {
      rowState.set(i, 'err');
      baseIdx = keepMesh ? keepBase : i;
      baseMesh = keepMesh;
      prevIdx = keepPrevIdx;
      prevMesh = keepPrevMesh;
      renderList();
      if (seq === loadSeq) failLoad('这一版模型生成失败：' + e.message);
      return;
    }
    if (seq !== loadSeq) return;

    /* 上一版：优先按历史里的 old_oid 找；找不到就取列表里的下一个更早版本 */
    let pi = -1;
    if (v.old_oid) pi = versions.findIndex((x, j) => j !== i && x.oid === v.old_oid);
    if (pi < 0 && i + 1 < versions.length) pi = i + 1;
    let pm = null;
    if (pi >= 0) {
      try {
        rowState.set(pi, 'loading'); renderList();
        pm = await enqueue(() => getMesh(versions[pi]));
        rowState.set(pi, 'cached');
      } catch (e) {
        pm = null;
        rowState.set(pi, 'err');
      }
      renderList();
      if (seq !== loadSeq) return;
    }

    baseIdx = i; baseMesh = mesh; prevIdx = pi; prevMesh = pm;
    /* 主版本单独显示，不占叠放名额；否则它会一直勾着还取消不掉 */
    overlaySel.delete(i);
    changed = 0; removed = 0;
    const bb = bboxOf(mesh);
    diag = Math.sqrt(bb.dx * bb.dx + bb.dy * bb.dy + bb.dz * bb.dz) || 1;
    const pct = Number($('.m-tol', body).value) / 10;
    if (pm && mesh.ntris <= 600000 && pm.ntris <= 600000) {
      /* 30 万面要算两秒左右，先让「正在比对」画出来 */
      showLoad('正在比对与上一版的差异…');
      await new Promise((r) => setTimeout(r, 16));
      if (seq !== loadSeq) return;
      changed = markChanged(mesh, pm, diag * pct / 100);
      removed = markRemoved(pm, mesh, diag * pct / 100);
    }
    try {
      viewer.load({ main: mesh, ghost: pm, removed: pm, flags: mesh.flags });
    } catch (e) {
      failLoad('渲染失败：' + e.message);
      return;
    }
    const edgeBox = $('.m-edge', body);
    if (mesh.ntris <= AUTO_EDGE_LIMIT) {
      edgeBox.checked = true;
      viewer.setEdges(true);
    } else {
      edgeBox.checked = false;
      viewer.setEdges(false);
    }
    syncDiffControls(!!pm);
    $('.m-cut', body).value = 100;
    $('.m-cutv', body).textContent = '关';
    viewer.setClip(100);
    applyOverlays();
    renderList();
    hideLoad();
    paint();
    const posterEl = $('.mesh-poster', body);
    if (posterEl) posterEl.remove();
    prefetch();
  }

  const MAX_OVERLAYS = 4;   // 再多叠下去既看不清也吃显存
  async function onOverlay(i, on) {
    if (on) {
      const used = [...overlaySel].filter((k) => k !== baseIdx).length;
      if (used > MAX_OVERLAYS) {
        overlaySel.delete(i);
        renderList();
        note.innerHTML = `<b style="color:#b26a00">最多同时叠放 ${MAX_OVERLAYS} 个版本</b>`
          + '<div class="muted small">先取消一个已勾选的版本，再选新的。</div>';
        return;
      }
      if (!cache.has(versions[i].oid)) {
        rowState.set(i, 'loading'); renderList();
        try { await enqueue(() => getMesh(versions[i])); rowState.set(i, 'cached'); }
        catch (e) {
          overlaySel.delete(i); rowState.set(i, 'err'); renderList();
          note.innerHTML = `<b style="color:#b91c1c">这一版生成失败：</b>${esc(e.message)}`;
          return;
        }
      }
      overlaySel.add(i);
    } else {
      overlaySel.delete(i);
    }
    applyOverlays();
    renderList();
    paint();
  }

  async function postJson(url, data) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(data),
    });
    let j = null;
    try { j = await res.json(); } catch (e) { /* 非 JSON 错误页 */ }
    if (!res.ok || !j || j.ok === false) {
      throw new Error((j && j.error) || ('HTTP ' + res.status));
    }
    return j;
  }

  /* 只恢复当前这一个文件，不碰同目录其它文件，也不直接生成版本 */
  function confirmRestoreVersion(i) {
    if (i < 0 || i >= versions.length) return;
    const v = versions[i];
    const m2 = window.openModal({
      title: '恢复文件到此版本',
      body: `<p>把 <b>${esc(file)}</b> 恢复成 <b>${esc(fmtWhen(v.time))}</b> 的内容。</p>
        <p class="muted small">只改这一个文件，其它文件不动，也不会立即生成新版本；
        现有内容会先备份到 <code>.zgr/backup/</code>。如果开着自动记录，这次改动会被记成一个新版本。</p>`,
      footer: '<button class="go primary">确认恢复</button><button class="cancel">取消</button>',
    });
    $('.cancel', m2.foot).onclick = m2.close;
    $('.go', m2.foot).onclick = async () => {
      const btn = $('.go', m2.foot);
      btn.disabled = true;
      btn.textContent = '恢复中…';
      try {
        const r = await postJson('/api/restore_file',
          { path, file, oid: v.oid, backup: true });
        m2.close();
        note.innerHTML = `<b style="color:#1f8a4c">已恢复到 ${esc(fmtWhen(v.time))}</b>`
          + `<div class="muted small">${r.size ? humanSize(r.size) + ' · ' : ''}`
          + (r.backup ? '原文件备份在 ' + esc(r.backup) : '原文件不存在，已直接写入') + '</div>';
      } catch (e) {
        btn.disabled = false;
        btn.textContent = '确认恢复';
        note.innerHTML = '<b style="color:#b91c1c">恢复失败：</b>' + esc(e.message);
      }
    };
  }

  listEl.addEventListener('click', (e) => {
    const row = e.target.closest('.mv-row');
    if (!row || e.target.classList.contains('mv-ov')) return;
    if (e.target.closest('.mv-restore')) {
      e.stopPropagation();
      confirmRestoreVersion(Number(row.dataset.i));
      return;
    }
    setBase(Number(row.dataset.i));
  });
  listEl.addEventListener('change', (e) => {
    if (!e.target.classList.contains('mv-ov')) return;
    const row = e.target.closest('.mv-row');
    if (!row) return;
    const i = Number(row.dataset.i);
    if (e.target.checked) overlaySel.add(i); else overlaySel.delete(i);
    onOverlay(i, e.target.checked);
  });

  $('.m-ghost', body).onchange = paint;
  $('.m-red', body).onchange = paint;
  $('.m-removed', body).onchange = paint;

  const tools = $('.mesh-tools', body);
  tools.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (!b) return;
    tools.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    viewer.setView(b.dataset.v);
  });

  const edgeBox = $('.m-edge', body);
  edgeBox.onchange = () => {
    const r = viewer.setEdges(edgeBox.checked);
    if (!r.ok) {
      edgeBox.checked = false;
      note.textContent = r.reason;
    }
  };

  const cut = $('.m-cut', body);
  cut.oninput = () => {
    const p = Number(cut.value);
    viewer.setClip(p);
    $('.m-cutv', body).textContent = p >= 100 ? '关' : p + '%';
  };

  const tol = $('.m-tol', body);
  let tolTimer = 0;
  tol.oninput = () => {
    const pct = Number(tol.value) / 10;
    $('.m-tolv', body).textContent = pct.toFixed(1) + '%';
    if (!prevMesh) return;
    clearTimeout(tolTimer);
    tolTimer = setTimeout(() => {
      note.textContent = '正在比对…';
      setTimeout(() => {
        changed = markChanged(baseMesh, prevMesh, diag * pct / 100);
        removed = markRemoved(prevMesh, baseMesh, diag * pct / 100);
        viewer.setFlags(baseMesh.flags);
        viewer.setRemoved(prevMesh);
        paint();
      }, 16);
    }, 160);
  };

  /* 后台按顺序把接下来几个版本先转好：点的时候就不用等 SolidWorks */
  async function prefetch() {
    const token = ++prefetchToken;
    const order = [];
    for (let d = 1; d <= 6 && baseIdx + d < versions.length; d++) order.push(baseIdx + d);
    for (let d = 1; d <= 2 && baseIdx - d >= 0; d++) order.push(baseIdx - d);
    const todo = order.filter((i) => i !== baseIdx && !cache.has(versions[i].oid));
    if (!todo.length) return;
    $('.mv-legend', body).textContent = '正在后台生成更早的版本…（已缓存 ' + (versions.length - todo.length) + '/' + versions.length + '）';
    for (const i of todo) {
      if (prefetchStop || token !== prefetchToken || !document.body.contains(m.el)) {
        $('.mv-legend', body).textContent = '点一行切换主版本；勾选多个版本可叠放在一起对比';
        return;
      }
      rowState.set(i, 'loading');
      renderList();
      try {
        await enqueue(() => getMesh(versions[i]));
        rowState.set(i, 'cached');
      } catch (e) {
        rowState.set(i, 'err');
      }
      renderList();
      await new Promise((r) => setTimeout(r, 20));
    }
    $('.mv-legend', body).textContent = '已提前生成最近几个版本，点一下即可秒开；勾选多个可叠放对比';
  }

  /* ---------------------------------------------------------- 初始化 */
  $('.mesh-name', body).textContent = file;
  syncDiffControls(false);

  try {
    const d = await fetchJson('/api/filelog?path=' + encodeURIComponent(path || '')
      + '&file=' + encodeURIComponent(file) + '&work=0');
    versions = (d.entries || []).filter((e) => e && e.oid);
    histTruncated = !!d.truncated;
  } catch (e) {
    versions = [];
  }
  /* 老接口兜底：至少把 before/after 两个版本显示出来 */
  if (!versions.length) {
    if (after) versions.push({ oid: after, commit: commit || '', time: Date.now(), author: '', message: '', size: 0, st: 'M' });
    if (before && before !== after) versions.push({ oid: before, commit: '', time: 0, author: '', message: '', size: 0, st: 'M' });
  }
  if (!versions.length) {
    failLoad('没有可显示的内容');
    return;
  }
  let start = versions.findIndex((v) => v.oid === after);
  if (start < 0) start = 0;
  renderList();
  await setBase(start, { initial: true });
  if (!versions.length || baseIdx < 0) return;
}

/* 取 JSON 小工具（3D 弹窗自己用） */
async function fetchJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    let msg = 'HTTP ' + res.status;
    try { const j = await res.json(); if (j.error) msg = j.error; } catch (e) { /* 非 JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

window.ZgrMesh = { open: openMeshViewer, parse: parseZgm, markChanged: markChanged };
window.openMeshViewer = openMeshViewer;
