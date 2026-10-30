/* 追光几何 · 网页界面 */
'use strict';

const S = {
  projects: [],
  path: null,
  project: null,
  commits: [],
  heads: {},
  branchStatus: {},
  branch: '',
  showMerged: false,
  selected: null,
  urls: [],
  tokenRequired: false,
  tab: 'timeline',
  filter: '',
  token: localStorage.getItem('zgr_token') || '',
  plan: null,
  choices: {},
  fileView: 'all',
  fileQuery: '',
  fileExt: '',            // 按类型筛选（'' = 全部）
  fileSort: 'default',    // default / name / mtime / ctime / size / status
  sortDir: 1,             // 1 = 升序，-1 = 降序
  sel: new Set(),         // 勾选的文件（批量导 STEP）
  times: {},              // 提交号 → {路径: {m,c,s}}（修改/创建时间，用到了才取）
  stepDest: localStorage.getItem('zgr_step_dest') || '',
  bomDest: localStorage.getItem('zgr_bom_dest') || '',
  liveSeq: 0,
  liveInit: false,
  liveTimer: 0,
  events: [],
  backups: [],
  pendingBarOpen: false,
  statusBusy: false,
  pairBusy: false,
  peerProjects: [],
  peerHosts: [],
  peerBusy: false,
  logLimit: 400,          // 时间线一次取多少个版本（不够时用户点「加载更早的」再加）
  logHasMore: false,      // 服务端还有更早的版本没取回来
  // 只看「我」记的版本：一人一条线之后最常问的是「我这边改到哪了」。
  // 记住开关，下次打开还是上次的选择（记的是「要不要」，不是某个项目）
  mineOnly: localStorage.getItem('zgr_mine_only') === '1',
  // 只看今天的版本。**故意不记住**：明天打开还停在「只看今天」，
  // 看到的是一片空白，比没有这个开关更糟
  todayOnly: false,
};

// 测试口：把「一次取多少个版本」调小，验证「更早的还没加载」这条提示
// （生产环境没有这个变量，就是 400）
if (window.__zgrLogLimit) S.logLimit = Number(window.__zgrLogLimit) || S.logLimit;

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const MODEL_EXT = /\.(sldprt|sldasm|slddrw|dwg|dxf|step|stp|iges|igs|ipt|iam|prt|catpart|3mf|stl)$/i;
const IMG_EXT = /\.(png|jpe?g|gif|bmp|webp|ico|tiff?)$/i;
const isModel = (p) => MODEL_EXT.test(p || '');
/* 这些格式浏览器里没有可用的开源引擎，只能让本机 SolidWorks 出网格；
   其余 DWG/DXF/STEP/STL 都走内置预览（preview.js） */
const MESH_EXT = /\.(sldprt|sldasm|slddrw|ipt|iam|prt|catpart|3mf)$/i;
const needMesh = (p) => MESH_EXT.test(p || '');
/* 能出缩略图的文件（模型 + 图片） */
const canThumb = (p) => isModel(p) || IMG_EXT.test(p || '');
const extOf = (p) => {
  const m = /\.([^./\\]+)$/.exec(p || '');
  return m ? m[1].toUpperCase() : '文件';
};

function withToken(url) {
  if (!S.token) return url;
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(S.token);
}

function thumbUrl(oid, name, size) {
  return withToken('/api/preview?' + new URLSearchParams({
    path: S.path || '', oid, name: name || '', size: String(size || 256),
  }).toString());
}

function fileThumbUrl(rel, size, force, ver) {
  // ver = 文件 mtime_ns：文件一改 URL 就变，绕开浏览器/中间层对同名路径的旧图缓存
  return withToken('/api/preview?' + new URLSearchParams({
    path: S.path || '', file: rel, size: String(size || 256),
    ...(force ? { force: '1' } : {}),
    ...(ver ? { v: String(ver) } : {}),
  }).toString());
}

/* 缩略图：加载失败就悄悄收起，绝不显示破图 */
function thumbImg(src, cls, alt) {
  return `<img class="thumb ${cls || ''}" loading="lazy" decoding="async"
    src="${esc(src)}" alt="${esc(alt || '')}" onerror="this.classList.add('broken')">`;
}

/* ---------------------------------------------------- 缩略图懒加载（列表用）
   一个版本里几千个零件时，最要命的不是渲染，是几千个 /api/preview 同时排队：
   服务端出图要走 SolidWorks 导出，串行且慢，你滑到的那一屏反而排在最后面。
   所以这里改成：
     · 元素进入视口附近（300px）才排队，离开就撤出队列
     · 同时最多 3 个在飞行，永远先发离视线中心最近的那个
     · 失败（多半是「SolidWorks 正忙」）自动退避重试，不把图钉成破图
*/
const THUMB_MAX = 3;
const THUMB_RETRY = 4;
// 在飞的图用集合记，不用计数器：图被整块重画掉、或者服务端在等 SolidWorks
// 迟迟不回，onload/onerror 可能永远不来。计数器只加不减的话，攒够 3 张
// 全站缩略图就再也不动了——集合配一个看门狗，超时的名额能收回来。
const _thumbInFlight = new Set();
let _thumbWatchdog = 0;
let _thumbQueue = [];

/* 看门狗：90 秒还没回来的图，把名额还回去（图回来时再从集合里删一次，
   删不存在的元素是安全的，不会把计数弄成负数） */
function thumbWatchdog() {
  if (_thumbWatchdog) return;
  _thumbWatchdog = setInterval(() => {
    const now = Date.now();
    _thumbInFlight.forEach((img) => {
      const t0 = Number(img.dataset.t0 || 0);
      if (!t0 || now - t0 > 90000) {
        img.dataset.state = '';
        _thumbInFlight.delete(img);
      }
    });
    if (!_thumbInFlight.size && !_thumbQueue.length) {
      clearInterval(_thumbWatchdog);
      _thumbWatchdog = 0;
    }
  }, 15000);
}
// 慢速重试的预算：一张图要不到，20 秒后悄悄再试，最多三轮。但一个两百个零件
// 的项目里，两百张图一起重试就是两百次请求——服务端每收到一次都可能去叫
// SolidWorks 导出预览，等于把整机拖住。所以一分钟只放这么多次，而且只重试
// 还在屏幕附近的图（用户看得见的那几张）。
const SLOW_RETRY_PER_MIN = 30;
let _slowRetry = { t: 0, n: 0 };

function slowRetryAllowed() {
  const now = Date.now();
  if (now - _slowRetry.t > 60000) { _slowRetry = { t: now, n: 0 }; }
  if (_slowRetry.n >= SLOW_RETRY_PER_MIN) return false;
  _slowRetry.n++;
  return true;
}

function nearViewport(img) {
  try {
    // 坏图是 display:none 的（只是暂时藏起来等重试）：它自己的矩形全是 0，
    // 按它判断会得出「不在屏幕里」，于是重试永远不发生。量它的位置框。
    let el = img;
    const own = img.getBoundingClientRect();
    if ((!own.width && !own.height) && img.parentElement) el = img.parentElement;
    const r = el.getBoundingClientRect();
    return r.bottom > -200 && r.top < (window.innerHeight || 800) + 200
      && r.right > 0 && r.left < (window.innerWidth || 1200);
  } catch (e) {
    return false;
  }
}
let _thumbIO = null;

function thumbPrio(img) {
  const r = img.getBoundingClientRect();
  return Math.abs((r.top + r.bottom) / 2 - window.innerHeight / 2);
}

function thumbObserver() {
  if (_thumbIO) return _thumbIO;
  _thumbIO = new IntersectionObserver((entries) => {
    for (const en of entries) {
      if (en.isIntersecting) thumbEnqueue(en.target);
      else thumbDrop(en.target);
    }
  }, { rootMargin: '300px 0px', threshold: 0 });
  return _thumbIO;
}

function thumbEnqueue(img) {
  const st = img.dataset.state || '';
  if (!img.dataset.src || st === 'queued' || st === 'loading' || st === 'done') return;
  img.dataset.state = 'queued';
  _thumbQueue.push({ img, prio: thumbPrio(img) });
  thumbPump();
}

function thumbDrop(img) {
  if (img.dataset.state !== 'queued') return;
  _thumbQueue = _thumbQueue.filter((j) => j.img !== img);
  img.dataset.state = '';
}

function thumbPump() {
  while (_thumbInFlight.size < THUMB_MAX && _thumbQueue.length) {
    _thumbQueue.sort((a, b) => a.prio - b.prio);
    const job = _thumbQueue.shift();
    const img = job.img;
    img.dataset.state = 'loading';
    img.dataset.t0 = String(Date.now());
    _thumbInFlight.add(img);
    thumbWatchdog();
    img.onload = () => {
      _thumbInFlight.delete(img);
      img.dataset.t0 = '';
      img.dataset.state = 'done';
      img.classList.add('ok');
      img.classList.remove('broken');
      img.dataset.slow = '0';
      // 之前失败时在旁边摆过扩展名牌：图回来了就收起来
      const badge = img.nextElementSibling;
      if (badge && badge.classList.contains('ext-badge')) badge.remove();
      thumbPump();
    };
    img.onerror = () => {
      _thumbInFlight.delete(img);
      img.dataset.t0 = '';
      const tries = Number(img.dataset.tries || 0) + 1;
      img.dataset.tries = String(tries);
      // 服务端已经在前台等过一次生成了，回来还是 404 就基本是「真没有」。
      // 有扩展名牌可显示的地方只再试一次——重试到第 4 次要 9 秒，
      // 这段时间卡片位置一直空着，比直接写「SLDASM」更像界面坏了
      const maxTries = img.dataset.fallback ? 1 : THUMB_RETRY;
      if (tries <= maxTries && img.isConnected) {
        // 服务端这会儿可能在等 SolidWorks 导出，退避一下再来
        img.dataset.state = '';
        setTimeout(() => { if (img.isConnected) thumbEnqueue(img); }, 900 * tries);
      } else {
        img.dataset.state = 'done';
        img.classList.add('broken');
        // 卡片位置别留一大块灰底：先摆个扩展名牌。
        // 但 404 不一定等于「永远没有」——SolidWorks 正忙、前台排队超了预算，
        // 服务端也会回 404（那种情况它不写失败标记）。所以图**不换成**牌子，
        // 只是暂时藏起来，过一会儿悄悄再试；试成了图回来、牌子自动收走。
        const fb = img.dataset.fallback;
        if (fb && img.isConnected) {
          const prev = img.nextElementSibling;
          if (!(prev && prev.classList.contains('ext-badge'))) {
            const span = document.createElement('span');
            span.className = 'ext-badge';
            span.textContent = fb;
            img.parentNode.insertBefore(span, img.nextSibling);
          }
          const slow = Number(img.dataset.slow || 0);
          if (slow < 3) {
            img.dataset.slow = String(slow + 1);
            // 这里必须把 state 清掉：上一行刚把它设成 done，而 thumbEnqueue 见到
            // done 会直接跳过 —— 不清的话这个「过一会儿再试」等于没写
            img.dataset.state = '';
            setTimeout(() => {
              if (!img.isConnected || !nearViewport(img) || !slowRetryAllowed()) return;
              thumbEnqueue(img);
            }, 20000);
          }
        }
      }
      thumbPump();
    };
    img.src = img.dataset.src;
  }
}

function lazyThumb(src, cls, alt, fallback) {
  return `<img class="thumb lazy ${cls || ''}" data-src="${esc(src)}"
    data-fallback="${esc(fallback || '')}" alt="${esc(alt || '')}" decoding="async">`;
}

function wireLazyThumbs(root) {
  const io = thumbObserver();
  $$('img.lazy[data-src]', root).forEach((img) => {
    if (img.dataset.wired) return;
    img.dataset.wired = '1';
    io.observe(img);
  });
}

/* 滚动时把队列里的优先级按当前位置重排，保证「你看的那块」永远排在最前面 */
let _thumbScrollT = 0;
window.addEventListener('scroll', () => {
  if (_thumbScrollT) return;
  _thumbScrollT = setTimeout(() => {
    _thumbScrollT = 0;
    if (!_thumbQueue.length) return;
    _thumbQueue.forEach((j) => { j.prio = thumbPrio(j.img); });
    thumbPump();
  }, 120);
}, true);

/* ---------------------------------------------------------- 缩略图悬停放大 */
/* 列表里的图只有 34px，看不清是哪个零件；停一下就把 512 的大图浮出来。
   大图是服务端现渲的（没实体预览的零件要几秒），所以框里先用**这一行自己的小图**
   垫底：它已经在浏览器缓存里，切过去立刻就是这个零件，绝不会留着上一个零件的图。 */
const zoom = { el: null, anchor: null, timer: 0, seq: 0 };

function zoomBox() {
  if (zoom.el && zoom.el.isConnected) return zoom.el;
  const box = document.createElement('div');
  box.id = 'thumbZoom';
  // 两层：.tz-ph 放这一行的小图（立刻可见），<img> 放大图（加载好才显示）
  box.innerHTML = '<div class="tz-frame"><div class="tz-ph"></div><img alt=""></div>'
    + '<span class="tz-name"></span>';
  document.body.appendChild(box);
  zoom.el = box;
  return box;
}

function zoomHide() {
  clearTimeout(zoom.timer);
  zoom.anchor = null;
  zoom.seq++;                 // 让还在路上的大图作废，别再改框里的内容
  if (zoom.el) zoom.el.classList.remove('show');
}

function zoomPlace(img) {
  const box = zoom.el;
  if (!box) return;
  const r = img.getBoundingClientRect();
  const bw = box.offsetWidth || 300;
  const bh = box.offsetHeight || 300;
  let x = r.right + 14;
  if (x + bw > window.innerWidth - 8) x = Math.max(8, r.left - bw - 14);
  let y = r.top + r.height / 2 - bh / 2;
  y = Math.max(8, Math.min(y, window.innerHeight - bh - 8));
  box.style.left = Math.round(x) + 'px';
  box.style.top = Math.round(y) + 'px';
}

function zoomShow(img, host) {
  const src = img.getAttribute('src') || '';
  if (!src || img.classList.contains('broken')) return;
  const big = src.replace(/([?&])size=\d+/, '$1size=512');
  if (big === src) return;                 // 已经是原尺寸图（详情页里的大图）就别再放大
  const box = zoomBox();
  const im = $('img', box);
  const ph = $('.tz-ph', box);
  const name = img.getAttribute('alt') || '';
  const tz = $('.tz-name', box);
  tz.textContent = name;
  tz.style.display = name ? '' : 'none';

  // ① 先撤掉上一张（浏览器换 src 时会**继续显示旧图**直到新图解码完，
  //    这正是「不管放哪个都显示同一个零件」的根源），改用这一行自己的小图垫底。
  //    小图已经在浏览器缓存里，所以框里立刻就是「当前这个零件」。
  im.removeAttribute('src');
  im.classList.remove('ready');
  ph.style.backgroundImage = 'url("' + src.replace(/"/g, '%22') + '")';

  const seq = ++zoom.seq;
  box.classList.remove('nofull');
  box.classList.add('busy');
  box.classList.add('show');
  zoomPlace(host || img);

  // ② 大图用游离的 Image 预取，加载成功才交给框里的 <img>：
  //    慢一拍或失败都不会把别的零件的图留在框里，也不会显示破图。
  const pre = new Image();
  pre.decoding = 'async';
  pre.onload = () => {
    if (zoom.seq !== seq) return;          // 已经移到别的零件了，这张迟到的图不要
    box.classList.remove('busy');
    im.src = big;
    im.classList.add('ready');
  };
  pre.onerror = () => {
    if (zoom.seq !== seq) return;
    box.classList.remove('busy');
    box.classList.add('nofull');           // 大图没生成出来：保留小图，底下给说明
  };
  pre.src = big;
}

const THUMB_HOST = '.fthumb, .fc-img, .m-thumb, .chg-thumb';

document.addEventListener('mouseover', (ev) => {
  const host = ev.target.closest && ev.target.closest(THUMB_HOST);
  if (!host) return;
  // 扩展名小标签(.fext)盖在图上，指针其实是落在它身上的，所以按容器定位、取容器里的图
  const img = host.querySelector('img.thumb');
  if (!img) return;
  if (zoom.anchor === host) return;
  clearTimeout(zoom.timer);
  zoom.anchor = host;
  zoom.timer = setTimeout(() => { if (zoom.anchor === host) zoomShow(img, host); }, 150);
});

document.addEventListener('mouseout', (ev) => {
  const host = ev.target.closest && ev.target.closest(THUMB_HOST);
  if (!host || !zoom.anchor) return;
  const to = ev.relatedTarget;
  if (to && host.contains(to)) return;
  zoomHide();
});

window.addEventListener('scroll', zoomHide, true);
window.addEventListener('resize', zoomHide);
document.addEventListener('click', zoomHide, true);

/* ------------------------------------------------------ 缩略图缺失自动补齐 */
/* 服务端出不了图的格式（STEP/IGES/STL…）在这里被识别出来，
   交给 thumbgen.js 用内置引擎渲染一张存回去，下次就秒出。 */
function thumbCtx() { return { path: S.path || '', token: S.token || '' }; }

document.addEventListener('error', (ev) => {
  const img = ev.target;
  if (!(img instanceof HTMLImageElement) || !img.classList || !img.classList.contains('thumb')) return;
  if (!window.zgrThumbs) return;
  const src = img.getAttribute('src') || '';
  let q;
  try { q = new URL(src, location.href).searchParams; } catch (e) { return; }

  const oid = /^[0-9a-f]{64}$/.test(q.get('oid') || '') ? q.get('oid') : '';
  if (oid) {
    window.zgrThumbs.want(oid, q.get('name') || '', thumbCtx());
    return;
  }
  // 「未保存」网格里的是工作区文件，没有对象号
  const rel = q.get('file') || '';
  if (rel) window.zgrThumbs.wantFile(rel, rel, thumbCtx(), q.get('v') || '');
}, true);

window.addEventListener('zgr-thumb', (ev) => {
  const d = ev.detail || {};
  if (!d.oid && !d.file) return;
  $$('img.thumb').forEach((img) => {
    const src = img.getAttribute('src') || '';
    const hit = d.oid ? src.indexOf(d.oid) >= 0
      : src.indexOf('file=' + encodeURIComponent(d.file)) >= 0;
    if (!hit) return;
    const base = src.replace(/([?&])t=\d+&?/, '$1').replace(/[?&]$/, '');
    img.classList.remove('broken');
    img.src = base + (base.includes('?') ? '&' : '?') + 't=' + Date.now();
  });
});

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtTime(ms) {
  if (!ms) return '—';
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const t = p(d.getHours()) + ':' + p(d.getMinutes());
  return sameDay ? '今天 ' + t : (d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + t);
}

/* 「多久以前」：同步断没断只看绝对时间没用，人要看的是「已经多久没联系上了」 */
function fmtAgo(ms) {
  if (!ms) return '';
  const sec = Math.max(0, Math.round((Date.now() - Number(ms)) / 1000));
  if (sec < 90) return '刚刚';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  return `${Math.floor(hr / 24)} 天前`;
}

function fmtSize(n) {
  n = Number(n || 0);
  if (n < 1024) return n + ' B';
  const u = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(1) + ' ' + u[i];
}

function toast(msg, ms) {
  let el = $('.toast');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), ms || 2600);
}

async function api(path, params) {
  const q = params ? '?' + new URLSearchParams(params).toString() : '';
  const res = await fetch(withToken(path + q), { cache: 'no-store' });
  const data = await res.json();
  if (!data.ok) throw httpErr(data, res);
  return data;
}

/* 错误带上状态码：前端要靠它区分「系统对话框弹不出来（409）」和别的问题 */
function httpErr(data, res) {
  const e = new Error((data && data.error) || '请求失败');
  e.status = res ? res.status : 0;
  return e;
}

async function post(path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (S.token) headers['X-ZGR-Token'] = S.token;
  const res = await fetch(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!data.ok) throw httpErr(data, res);
  return data;
}

/* ------------------------------------------------------------------ 弹层 */
function openModal(opts) {
  const wrap = document.createElement('div');
  wrap.className = 'overlay';
  wrap.innerHTML = `
    <div class="modal" style="width:${opts.width || 'min(720px,100%)'}">
      <div class="modal-head"><h3>${esc(opts.title || '')}</h3>
        <button class="modal-x">关闭</button></div>
      <div class="modal-body"></div>
      <div class="modal-foot"></div>
    </div>`;
  const body = $('.modal-body', wrap);
  const foot = $('.modal-foot', wrap);
  if (opts.body instanceof Node) body.appendChild(opts.body);
  else if (opts.body) body.innerHTML = opts.body;
  if (opts.footer) {
    if (opts.footer instanceof Node) foot.appendChild(opts.footer);
    else foot.innerHTML = opts.footer;
  }
  const close = () => {
    wrap.remove();
    if (opts.onClose) { try { opts.onClose(); } catch (e) { /* 收尾失败不该挡住关窗 */ } }
  };
  wrap._zgrClose = close;      // 全局 Esc 兜底也要走同一条收尾逻辑（onClose 不能漏）
  $('.modal-x', wrap).onclick = close;
  wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(); });
  // Esc 关最上面那一层（以前只有点 × 或点外面才关得掉，键盘用户被卡在里面）
  wrap.tabIndex = -1;
  wrap.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.stopPropagation(); close(); }
  });
  document.getElementById('modalRoot').appendChild(wrap);
  setTimeout(() => { if (!wrap.querySelector('input,textarea,select')) wrap.focus(); }, 0);
  return { el: wrap, body, foot, close };
}

/* ------------------------------------------------------------------ 目录选择 */
/* 选文件夹：先叫 Windows 自带的对话框——桌面、下载、U 盘、网络位置、快速访问、
   最近使用、新建文件夹全都在，和自己画一棵树完全不是一回事。
   只有在系统对话框弹不出来时（服务在会话 0 / 远程桌面已断开 / 非 Windows），
   才退回网页里那个简易目录树。 */
async function pickFolder(initial) {
  try {
    const d = await post('/api/pick', {
      kind: 'folder', initial: initial || '', title: '选择文件夹',
    });
    return d.paths && d.paths.length ? d.paths[0] : null;   // 取消 = null
  } catch (e) {
    if (e.status === 429) {          // 已经有一个系统对话框开着，别叠第二个
      toast(e.message, 3000);
      return null;
    }
    if (e.status !== 409) toast('系统对话框打不开，改用网页选择：' + e.message, 4000);
    return pickFolderLegacy(initial);
  }
}

/* 选文件（可以多选）：返回绝对路径数组，取消返回 [] */
async function pickFiles(initial, multiple = true, title = '选择文件') {
  try {
    const d = await post('/api/pick', {
      kind: 'files', initial: initial || '', multiple: !!multiple, title,
    });
    return d.paths || [];
  } catch (e) {
    toast('系统对话框打不开：' + e.message, 4500);
    return [];
  }
}

async function pickFolderLegacy(initial) {
  return new Promise((resolve) => {
    let cur = initial || '';
    const m = openModal({
      title: '选择文件夹',
      body: '<div class="browser"><div class="crumb">加载中…</div><div class="items"></div></div>',
      footer: '<button class="choose">选择此文件夹</button><button class="cancel">取消</button>',
    });
    const crumb = $('.crumb', m.body);
    const items = $('.items', m.body);

    async function load(path) {
      try {
        const d = await api('/api/browse', path ? { path } : {});
        cur = d.path;
        crumb.textContent = d.path;
        items.innerHTML = '';
        if (d.drives && d.drives.length) {
          const row = document.createElement('div');
          row.className = 'item';
          row.innerHTML = d.drives.map((x) => `<span class="r">${esc(x)}</span>`).join('  ');
          row.onclick = () => load(d.drives[0]);
          items.appendChild(row);
        }
        if (d.parent) {
          const up = document.createElement('div');
          up.className = 'item';
          up.textContent = '⬆ 上级目录';
          up.onclick = () => load(d.parent);
          items.appendChild(up);
        }
        d.dirs.forEach((dir) => {
          const el = document.createElement('div');
          el.className = 'item';
          el.innerHTML = `<span>📁 ${esc(dir.name)}</span>${dir.repo ? '<span class="r">已是追光项目</span>' : ''}`;
          el.onclick = () => load(dir.path);
          items.appendChild(el);
        });
        if (!d.dirs.length) {
          items.insertAdjacentHTML('beforeend', '<div class="item muted">（没有子文件夹）</div>');
        }
      } catch (e) {
        crumb.textContent = '读取失败：' + e.message;
      }
    }

    $('.choose', m.foot).onclick = () => { m.close(); resolve(cur); };
    $('.cancel', m.foot).onclick = () => { m.close(); resolve(null); };
    load(initial || '');
  });
}

/* ------------------------------------------------------------------ 项目 */
// 第一次用最容易卡在「列表一片空白」：给一个能立刻点开的示例项目。
// 只建在临时目录，绝不碰用户自己的图纸目录；建错了在列表里移除即可。
async function startDemo() {
  const b = document.getElementById('btnDemo');
  if (b) { b.disabled = true; b.textContent = '正在建示例…'; }
  try {
    const r = await post('/api/demo', {});
    await loadProjects();
    await selectProject(r.project.path, true);
    toast('✓ 示例项目建好了：' + r.project.name + '（试完可以在列表里移除，文件不会删）', 7000);
  } catch (e) {
    toast('✗ ' + e.message, 7000);
    if (b) { b.disabled = false; b.textContent = '先看看示例项目'; }
  }
}

async function loadProjects() {
  const d = await api('/api/projects');
  S.projects = d.projects || [];
  S.urls = d.urls || [];
  S.tokenRequired = d.token_required;
  renderProjects();
  renderUrls();
}

/* 左侧项目：右键菜单（打开文件夹 / 切换 / 移除） */
let projMenuEl = null;
function closeProjMenu() {
  if (projMenuEl) {
    projMenuEl.remove();
    projMenuEl = null;
  }
}
function openProjectFolder(path) {
  if (!path) return;
  post('/api/openfolder', { target: path })
    .catch((e) => toast('打开文件夹失败：' + e.message, 4000));
}
function clearProjectSelection() {
  S.path = null;
  S.project = null;
  S.selectedData = null;
  // 动态流是按项目算 seq 的：旧项目的 seq 留在身上，下一个项目的事件永远「都比它小」，
  // 动态区就会一直空着（看起来像这项目从来没同步过）
  S.liveSeq = 0;
  S.liveInit = false;
  S.events = [];
  S.branch = '';
  history.replaceState(null, '', location.pathname);
  $('#projTitle').textContent = '未选择项目';
  $('#btnOpenProject').hidden = true;
  $('#timeline').innerHTML = '';
  $('#detail').innerHTML = '<div class="detail-empty">从左侧时间线选择一个版本查看详情</div>';
  renderProjects();
}
async function removeProject(path) {
  if (!path) return;
  const p = (S.projects || []).find(
    (x) => String(x.path).toLowerCase() === String(path).toLowerCase());
  if (!confirm('从列表移除「' + (p ? p.name : path) + '」？（不会删除任何文件或历史记录）')) return;
  try {
    await post('/api/remove', { path });
    if (S.path && S.path.toLowerCase() === String(path).toLowerCase()) clearProjectSelection();
    await loadProjects();
  } catch (e) {
    toast('移除失败：' + e.message, 4000);
  }
}
function showProjMenu(x, y, p) {
  closeProjMenu();
  const el = document.createElement('div');
  el.className = 'ctx-menu';
  el.innerHTML = '<button data-act="open">打开项目文件夹</button>'
    + '<button data-act="select">切换到该项目</button>'
    + '<button data-act="relocate">重新定位到别的文件夹…</button>'
    + '<div class="ctx-sep"></div>'
    + '<button data-act="remove" class="danger">从列表移除…</button>';
  document.body.appendChild(el);
  const r = el.getBoundingClientRect();
  el.style.left = Math.max(6, Math.min(x, innerWidth - r.width - 8)) + 'px';
  el.style.top = Math.max(6, Math.min(y, innerHeight - r.height - 8)) + 'px';
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    closeProjMenu();
    if (act === 'open') openProjectFolder(p.path);
    else if (act === 'select') selectProject(p.path);
    else if (act === 'relocate') relocateProject(p);
    else if (act === 'remove') removeProject(p.path);
  });
  projMenuEl = el;
}
document.addEventListener('mousedown', (e) => {
  if (projMenuEl && !projMenuEl.contains(e.target)) closeProjMenu();
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeProjMenu(); });
window.addEventListener('blur', closeProjMenu);
window.addEventListener('resize', closeProjMenu);
document.addEventListener('scroll', closeProjMenu, true);

/* 项目文件夹被搬到别处（换硬盘、改目录名、U 盘换了盘符）：在列表里直接改路径。
   以前只能「从列表移除」再「添加文件夹」——中间那一步要是选错了，用户会以为项目没了。 */
async function relocateProject(p) {
  const dir = await pickFolder(p.exists ? p.path : '');
  if (!dir) return;                                        // 取消了
  if (dir.toLowerCase() === String(p.path).toLowerCase()) return;
  try {
    await post('/api/add', { path: dir, target: dir, name: p.name });
  } catch (e) {
    toast('这个文件夹还不是追光项目（里面要有 .zgr）：' + (e.message || e), 8000);
    return;
  }
  try {
    await post('/api/remove', { path: p.path });
  } catch (e) {
    toast('新位置已加上，但旧记录没删掉：' + (e.message || e), 8000);
  }
  toast('✓ 已改到 ' + dir, 6000);
  await loadProjects();
  await selectProject(dir, true);
}

/* 同一个目录在不同地方写出来可能是 C:/a 或 c:\a：比较前统一一下 */
function normPath(p) {
  return (p || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
}

/* 用链接（#p=路径）打开、却还没加进项目列表的项目。
   以前这种项目在左侧「看不见」——顶上明明开着，列表里空着，看着像没打开。
   这里把它单独摆出来，并给一个「加入列表」的按钮，点一下才算数（不偷偷加）。 */
function projectNotInList() {
  if (!S.path) return null;
  const k = normPath(S.path);
  if (S.projects.some((p) => normPath(p.path) === k)) return null;
  return { path: S.path, name: (S.path.split(/[\\/]/).filter(Boolean).pop()) || S.path };
}

function renderProjects() {
  const box = $('#projList');
  const cur = projectNotInList();
  $('#projCount').textContent = S.projects.length ? `${S.projects.length} 个` : '';
  const curHtml = cur ? `<div class="proj cur-only" data-path="${esc(cur.path)}" title="${esc(cur.path)}">
      <div class="proj-name"><span class="dot"></span>${esc(cur.name)}</div>
      <div class="proj-sub">正在使用 · 还没加入列表</div>
      <button class="proj-join" data-path="${esc(cur.path)}">＋ 加入项目列表</button>
    </div>` : '';
  if (!S.projects.length) {
    box.innerHTML = curHtml || `<div class="empty" style="padding:26px 10px">
        还没有项目<br><span class="small">用自己的图纸文件夹新建一个，或者先看看示例</span>
        <div class="demo-box">
          <button id="btnDemo">先看看示例项目</button>
          <span class="small">建在临时目录，随时可以在列表里移除</span>
        </div>
      </div>`;
    wireProjectJoin(box);
    const b = box.querySelector('#btnDemo');
    if (b) b.onclick = startDemo;
    return;
  }
  box.innerHTML = curHtml + S.projects.map((p) => {
    const active = S.path && p.path.toLowerCase() === S.path.toLowerCase();
    const dot = !p.exists || !p.is_repo ? 'off' : (p.dirty ? 'dirty' : '');
    const sub = p.is_repo
      ? `${p.branch || 'main'} · ${p.last_time ? fmtTime(p.last_time) : '尚未记录'}`
      : (p.exists ? '不是追光项目' : '目录不存在');
    return `<div class="proj ${active ? 'active' : ''}" data-path="${esc(p.path)}" title="${esc(p.path)}">
      <div class="proj-name"><span class="dot ${dot}"></span>${esc(p.name)}</div>
      <div class="proj-sub">${esc(sub)}</div>
    </div>`;
  }).join('');
  $$('.proj', box).forEach((el) => {
    el.onclick = () => selectProject(el.dataset.path);
    el.oncontextmenu = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const p = S.projects.find((x) => x.path === el.dataset.path);
      if (p) showProjMenu(e.clientX, e.clientY, p);
    };
  });
  wireProjectJoin(box);
}

function wireProjectJoin(box) {
  const btn = $('.proj-join', box);
  if (!btn) return;
  btn.onclick = async (e) => {
    e.stopPropagation();
    const p = btn.dataset.path;
    btn.disabled = true;
    try {
      await post('/api/add', { target: p, path: p });
      await loadProjects();
      toast(`✓ 已加入项目列表：${p}`, 3000);
    } catch (err) {
      btn.disabled = false;
      toast('加入列表失败：' + err.message, 4000);
    }
  };
}

/* ------------------------------------------------------------------ 对方机器上的项目 */
/* 另一台电脑新建了一个项目，这边左侧就能看到它——点一下把对方的文件整个拉过来，
   目录铺好、分支指针指到同一版，拉完自动接着和对方实时同步。 */
function hostOf(url) {
  const m = /^https?:\/\/([^/:]+)(?::(\d+))?/i.exec(url || '');
  return m ? m[1] + (m[2] ? ':' + m[2] : '') : (url || '对方');
}

function renderPeer() {
  const box = $('#peerBox');
  const list = $('#peerList');
  if (!box || !list) return;
  const remote = (S.peerProjects || []).filter((p) => !p.known);
  const hosts = (S.peerHosts || []).filter((h) => h && (h.error || h.host));
  const show = S.peerBusy || remote.length > 0 || hosts.length > 0;
  box.hidden = !show;
  if (!show) {
    list.innerHTML = '';
    $('#peerCount').textContent = '';
    return;
  }
  $('#peerCount').textContent = remote.length ? `${remote.length} 个` : '';
  if (!remote.length) {
    const bad = hosts.find((h) => h.error);
    list.innerHTML = `<div class="empty small" style="padding:10px 8px">${
      S.peerBusy ? '正在找局域网里的另一台电脑…'
        : (bad ? esc(bad.error) : '对方机器上还没有新项目')}</div>`;
    return;
  }
  list.innerHTML = remote.map((p, i) => `<div class="proj peer" data-i="${i}"
      title="点一下把「${esc(p.name)}」拉到本机">
      <div class="proj-name"><span class="dot peer"></span>${esc(p.name)}</div>
      <div class="proj-sub">来自 ${esc(p.peer_host || hostOf(p.url))} · ${p.files || 0} 个文件${
        p.bytes ? ' · ' + fmtSize(p.bytes) : ''}${p.time ? ' · ' + fmtTime(p.time) : ''}</div>
      <span class="peer-add" title="拉到本机">拉过来</span>
    </div>`).join('');
  $$('.proj.peer', list).forEach((el) => {
    el.onclick = () => openCloneDialog(remote[Number(el.dataset.i)]);
  });
}

let peerBusyTimer = 0;
async function loadPeerProjects(quiet) {
  if (S.peerBusy) return;
  S.peerBusy = true;
  renderPeer();
  // 局域网扫描（对方关机时）最多等 6 秒，先把「正在找」显示出来
  clearTimeout(peerBusyTimer);
  peerBusyTimer = setTimeout(() => renderPeer(), 350);
  try {
    const d = await api('/api/peer/projects');
    S.peerProjects = d.projects || [];
    S.peerHosts = d.peers || [];
  } catch (e) {
    S.peerProjects = S.peerProjects || [];
    if (!quiet) toast('找对方项目失败：' + e.message, 5000);
  } finally {
    clearTimeout(peerBusyTimer);
    S.peerBusy = false;
    renderPeer();
  }
}

/* 克隆进度：后台任务，前端轮询（几千个文件要几分钟，不能挂在一次请求上） */
function pollClone(id, onTick, route) {
  let stop = false;
  const tick = async () => {
    if (stop) return;
    let job = null;
    try {
      job = (await api(route || '/api/peer/clone/status', { id })).job;
    } catch (e) {
      onTick({ state: 'error', error: '进度查询失败：' + e.message });
      return;
    }
    onTick(job);
    if (job.state === 'running') setTimeout(tick, 400);
  };
  tick();
  return () => { stop = true; };
}

async function openCloneDialog(item) {
  const name = item.name || '';
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="clone-info">
      <div class="ci-name">${esc(name)}</div>
      <div class="ci-sub">来自 ${esc(item.peer_host || hostOf(item.url))}
        · 分支 ${esc(item.branch || 'main')} · ${item.files || 0} 个文件${
        item.bytes ? ' · ' + fmtSize(item.bytes) : ''}</div>
      ${item.message ? `<div class="ci-msg">最新一版：${esc(item.message)}${
        item.author ? ' — ' + esc(item.author) : ''}</div>` : ''}
    </div>
    <div class="field"><label>保存到哪个文件夹</label>
      <div class="row" style="margin:0">
        <input class="dest" style="flex:1" placeholder="点「浏览…」选一个文件夹，会存成 <那个文件夹>\\${esc(name)}">
        <button class="browse" style="flex:none" title="打开 Windows 的资源管理器对话框选文件夹">浏览…</button>
      </div>
      <div class="muted small dest-hint">必须是一个空文件夹（不存在也行，会自动新建），
        免得对方的文件混进你已有的资料里。</div>
    </div>
    <div class="cp" hidden>
      <div class="cp-bar"><i style="width:0%"></i></div>
      <div class="cp-text muted small"></div>
    </div>`;
  const m = openModal({
    title: '把「' + name + '」拉到本机',
    body,
    width: 'min(620px,100%)',
    footer: '<button class="go primary">开始拉取</button><button class="cancel">关闭</button>',
  });
  const destEl = $('.dest', body);
  const hint = $('.dest-hint', body);
  const cp = $('.cp', body);
  const bar = $('.cp-bar i', cp);
  const ptext = $('.cp-text', cp);
  const goBtn = $('.go', m.foot);
  let busy = false;

  /* 选的是「放在哪个父文件夹里」：自动拼上项目名，拼出来是什么直接摆在输入框里，
     可以自己再改——不搞「看见 A 实际存到 B」那套 */
  $('.browse', body).onclick = async () => {
    const picked = await pickFolder(destEl.value);
    if (!picked) return;
    const sep = picked.includes('\\') ? '\\' : '/';
    const tail = picked.replace(/[\\/]+$/, '');
    destEl.value = tail + sep + name.replace(/[\\/:*?"<>|]/g, '-');
    hint.textContent = `会在「${tail}」里建一个「${destEl.value.split(/[\\/]/).pop()}」文件夹，对方的文件铺进去`;
  };
  $('.cancel', m.foot).onclick = () => {
    if (busy && !confirm('还在拉取，关掉窗口的话任务会在后台继续。要关掉窗口吗？')) return;
    m.close();
  };

  goBtn.onclick = async () => {
    const dest = destEl.value.trim();
    if (!dest) return toast('先选一个保存位置');
    busy = true;
    goBtn.disabled = true;
    goBtn.textContent = '正在拉取…';
    $('.browse', body).disabled = true;
    destEl.disabled = true;
    cp.hidden = false;
    bar.style.width = '0%';
    ptext.textContent = '正在连对方…';
    let job;
    try {
      job = (await post('/api/peer/clone', {
        url: item.url, project: name, dest,
      })).job;
    } catch (e) {
      busy = false;
      goBtn.disabled = false;
      goBtn.textContent = '开始拉取';
      $('.browse', body).disabled = false;
      destEl.disabled = false;
      cp.hidden = true;
      return toast('拉取失败：' + e.message, 6000);
    }
    pollClone(job.id, (st) => {
      const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : 0;
      if (m.el.isConnected) {
        bar.style.width = (st.state === 'done' ? 100 : pct) + '%';
        ptext.textContent = st.state === 'error'
          ? '出错：' + st.error
          : `${st.phase || ''}${st.total ? ` · ${st.done}/${st.total}` : ''}${
            st.detail ? ' · ' + st.detail : ''}`;
        ptext.classList.toggle('bad', st.state === 'error');
      }
      if (st.state === 'done') finish(st);
      else if (st.state === 'error') {
        busy = false;
        if (!m.el.isConnected) return toast('拉取失败：' + st.error, 8000);
        goBtn.disabled = false;
        goBtn.textContent = '重试';
        $('.browse', body).disabled = false;
        destEl.disabled = false;
        toast('拉取失败：' + st.error, 8000);
      }
    });

    async function finish(st) {
      const res = st.result || {};
      busy = false;
      if (m.el.isConnected) {
        goBtn.textContent = '完成';
        setTimeout(() => m.close(), 600);
      }
      toast(`✓ 已拉到本机：${res.path || dest}（${res.files || 0} 个文件），已开始和对方实时同步`, 6000);
      await loadProjects();
      await loadPeerProjects(true);
      if (res.path) selectProject(res.path);
    }
  };
}

function renderUrls() {  $('#urls').innerHTML = (S.urls || []).map((u) => `<div title="点击复制">${esc(u)}</div>`).join('') || '—';
  $$('#urls div').forEach((el) => {
    el.onclick = () => {
      navigator.clipboard && navigator.clipboard.writeText(el.textContent.trim());
      toast('已复制 ' + el.textContent.trim());
    };
  });
}

async function selectProject(path, keepHash) {
  // 换项目就作废：从「移除项目」回来时 S.path 是空的，以前这个条件直接跳过了重置
  if (S.path !== path) {
    S.liveSeq = 0;
    S.liveInit = false;
    S.events = [];
    S.branch = '';
  }
  S.path = path;
  S.selected = null;
  S.selectedData = null;
  // 换项目时上一条搜索必须整个作废：既要收起结果（它是**上一个项目**的历史），
  // 也要掐掉还在排队的定时器和在途请求——否则定时器会拿新项目去搜旧关键词，
  // 或者在途请求回来时把上一个项目的命中写进这一屏
  S.filter = '';
  S.findHits = [];
  findSeq++;
  clearTimeout(findTimer);
  if ($('#searchBox')) $('#searchBox').value = '';
  if ($('#findBox')) { $('#findBox').hidden = true; $('#findBox').innerHTML = ''; }
  // 「只看今天」是这一屏的临时视图，不跟项目走：以前切到 A 有今天的版本就静悄悄
  // 接着筛、切到没今天的项目又被自动放开，同一个开关一会儿留一会儿没留
  S.todayOnly = false;
  S.todayInfo = null;
  S.pendingShowAll = false;
  S.pendingBarOpen = false;
  const pb = $('#pendingBody');
  if (pb) pb.dataset.sig = '';
  S.fileQuery = '';
  S.fileView = 'all';
  S.fileExt = '';
  S.sel = new Set();
  S.plan = null;
  S.choices = {};
  if ($('#mergeBody')) $('#mergeBody').innerHTML = '<div class="muted small">尚未检查。</div>';
  if ($('#btnApplyMerge')) $('#btnApplyMerge').disabled = true;
  if ($('#mergeMeta')) $('#mergeMeta').textContent = '';
  if (!keepHash) history.replaceState(null, '', '#p=' + encodeURIComponent(path));
  renderProjects();
  $('#detail').innerHTML = '<div class="detail-empty">加载中…</div>';
  try {
    const d = await api('/api/project', { path });
    S.project = d;
    document.title = d.name + ' · 追光几何';
    $('#projTitle').textContent = d.name + '  ·  ' + d.path;
    $('#btnOpenProject').hidden = false;
    $('#headPill').textContent = d.head ? d.head.slice(0, 10) : '尚未记录';
    $('#headPill').classList.toggle('dirty', !!(d.status && !d.status.clean));
    $('#ignoreBox').value = (d.ignore || []).join('\n');
    $('#watchOn').checked = !!d.watching;
    $('#watchInterval').value = d.watch_interval || 30;
    $('#authorName').value = d.author || '';
    applyLive(d.live || {}, true);
    await loadTimeline();
    renderPending();
    renderPendingBar();
    // 打开项目先看到「最新一版」：右边一大片空白 +「请自己选一个」不是好的第一眼。
    // 只在这一版是「刚打开、还没选过」时自动选，刷新时仍然尊重用户选中的那条。
    const newest = newestCommitId();
    if (!S.selected && newest) await selectCommit(newest);
    else renderDetail();
    renderBanner();
    // 后台把还缺图的模型（STEP/STL…）补上，不挡界面
    if (window.zgrThumbs) {
      const ctx = { path, token: S.token || '' };
      setTimeout(() => window.zgrThumbs.scan(ctx), 800);
    }
    await loadFeed(true);
    ensureFeed();
  } catch (e) {
    toast('打开项目失败：' + e.message, 4000);
    $('#detail').innerHTML = `<div class="detail-empty">${esc(e.message)}</div>`;
  }
}

function renderBanner() {
  const b = $('#banner');
  /* 恢复被打断：这条最要紧，放在最前面。图纸没丢——要么再恢复一遍，要么从备份拷回来 */
  const pr = S.project && S.project.pending_restore;
  if (pr && pr.corrupt) {
    /* 记号坏了：确实断过，但不知道断在哪一步。只能提示去体检，别自作主张动文件 */
    b.hidden = false;
    b.innerHTML = `<span>⚠ 上次恢复被打断，但恢复记号已损坏，不知道当时恢复到哪个版本。
      文件可能半新半旧——建议先点「🩺 体检」，再决定是否重新恢复。</span>
      <button class="ghost">我知道了</button>`;
    b.querySelector('button').onclick = async () => {
      await post('/api/recovery', { path: S.path, dismiss: true });
      await selectProject(S.path, true);
    };
    return;
  }
  if (pr) {
    b.hidden = false;
    b.innerHTML = `<span>⚠ 上次恢复到 ${esc((pr.commit || '').slice(0, 10))} 做到一半被打断了
      （${pr.time ? fmtTime(pr.time) : '时间不详'}）。文件可能是半新半旧的，但一个都没丢。</span>
      <button class="primary">继续做完</button>
      <button class="ghost">回到恢复前</button>`;
    const [go, back] = b.querySelectorAll('button');
    go.onclick = async () => {
      go.disabled = back.disabled = true;
      go.textContent = '恢复中…';
      try {
        const r = await post('/api/recovery', { path: S.path, mode: 'again' });
        toast(`✓ 已恢复到一致状态（写入 ${r.written || 0} 个文件）`, 4500);
        await selectProject(S.path, true);
      } catch (e) {
        go.disabled = back.disabled = false;
        go.textContent = '继续做完';
        toast(e.message, 7000);
      }
    };
    back.onclick = async () => {
      if (!pr.backup_exists) return toast('这批备份已经不在了，只能用「继续做完」', 5000);
      if (!confirm('把备份里的文件原样拷回去（= 回到这次恢复之前的样子）？')) return;
      go.disabled = back.disabled = true;
      back.textContent = '拷回中…';
      try {
        const r = await post('/api/recovery', { path: S.path, mode: 'backup' });
        toast(`✓ 已从备份拷回 ${r.restored || 0} 个文件`, 4500);
        await selectProject(S.path, true);
      } catch (e) {
        go.disabled = back.disabled = false;
        back.textContent = '回到恢复前';
        toast(e.message, 7000);
      }
    };
    return;
  }
  const pn = S.project && S.project.pull_needed;
  if (!pn) {
    // 盘快满了：这件事发生在「点记录」之前才有意义——等写到一半失败就晚了。
    // 只说事实和几个能立刻做的事，不弹窗、不拦任何操作。
    const disk = (S.project && S.project.status && S.project.status.disk) || null;
    if (disk && disk.free >= 0 && disk.low) {
      b.hidden = false;
      b.innerHTML = `<span>⚠ 盘上只剩 <b>${fmtSize(disk.free)}</b>（建议至少留 ${fmtSize(disk.min_free || 0)}）。
        录一版大改动可能写不下，SolidWorks 存盘也会跟着失败。</span>
        <button class="ghost">看看能清什么</button>`;
      b.querySelector('button').onclick = () => showSizes();
      return;
    }
    b.hidden = true;
    b.innerHTML = '';
    return;
  }
  b.hidden = false;
  // 记号里的原因要说出来：同步时图纸正被占用，提示「对方推送了新版本」会让人以为是别的问题，
  // 点一次失败一次也看不出为什么
  const why = String(pn.reason || '').trim();
  const failed = /失败|占用|只读/.test(why);
  b.innerHTML = `<span title="${esc(why)}">⚠ ${esc((why || '对方推送了新版本').split('\n')[0].slice(0, 140))}
    ${pn.time ? '（' + fmtTime(pn.time) + '）' : ''}，本机工作区还没更新。</span>
    <button class="primary">${failed ? '重试更新工作区' : '更新工作区'}</button>`;
  b.querySelector('button').onclick = async (e) => {
    e.target.disabled = true;
    try {
      const r = await post('/api/checkout', { path: S.path });
      toast(`✓ 工作区已更新（写入 ${r.written} 个文件）`);
      await selectProject(S.path, true);
    } catch (err) {
      e.target.disabled = false;
      if (/未记录改动/.test(err.message) &&
          confirm(err.message + '\n\n强制覆盖？覆盖前的文件会备份到 .zgr/backup。')) {
        try {
          const r = await post('/api/checkout', { path: S.path, force: true });
          toast(`✓ 工作区已更新（写入 ${r.written} 个文件）`);
          await selectProject(S.path, true);
        } catch (e2) { toast(e2.message, 5000); }
      } else toast(err.message, 5000);
    }
  };
}

/* ------------------------------------------------------------------ 实时同步 / 动态 */
const LIVE_STATE = {
  idle: '已对齐', pulled: '刚接收了对方版本', pending: '对方有新版本待落地',
  merged: '刚自动合并', merged_pending: '已合并，工作区待更新',
  pushed: '刚把本机版本推过去', merged_pushed: '合并并已推送',
  local_ahead: '本机领先，等待推送', remote_ahead: '对方领先',
  diverged: '两边都改了，等待合并', pull_failed: '写入工作区失败',
  push_failed: '推送失败', error: '出错',
  conflict: '有文件冲突，等你选一次',
};
const EVENT_KIND = {
  commit: '记录', auto_commit: '自动记录', peer_pull: '接收对方',
  peer_merge: '自动合并', peer_push: '推送给对方', peer_pending: '待更新',
  peer_diverged: '需要合并', peer_conflict: '冲突等你选',
  merge: '合并', undo: '回溯', sync: '同步',
  pack: '打包', trash_restore: '复原', checkout: '更新工作区',
  restore: '恢复文件', live_on: '实时已开启', live_off: '实时已关闭',
};

/* 自动配对状态：用户不需要填地址，也不要点开启 */
/* 同步是不是真的还在跑：线程活着 ≠ 对方在线。
   判据是「上一次真正对上话」距今多久（对账周期×3，且至少 90 秒）。 */
function liveStale(live) {
  live = live || {};
  if (!live.running) return false;
  const lastOk = Number(live.last_ok || 0);
  if (!lastOk) return false;
  const interval = Math.max(5, Number(live.interval || 15)) * 1000;
  return Date.now() - lastOk > Math.max(90000, interval * 3);
}

function renderPairState(live) {
  const box = $('#pairState');
  if (!box) return;
  const txt = $('.pair-text', box);
  const btn = $('#btnAutoPair');
  live = live || {};
  const who = live.peer_author || live.peer_host || live.peer_name || live.url || '对方电脑';
  if (live.running) {
    // 「running」只说明线程还活着，不代表对方在线。以前这里永远显示「实时同步中」，
    // 对方关机一整天，这边照样一片绿：用户以为两台一直在同步，等发现时已经差了几十版。
    // 判断依据是「上一次真正对上话」到现在有多久（对账周期 × 3 且至少 90 秒）。
    const lastOk = Number(live.last_ok || 0);
    const stale = liveStale(live);
    if (stale) {
      box.className = 'pair-state bad';
      const why = live.last_error ? `（${esc(live.last_error)}）` : '';
      txt.innerHTML = `⚠ 对方 <b>${esc(who)}</b> 已 ${fmtAgo(lastOk)}没联系上${why}`
        + ' · 正在自动重试；这期间的改动都还在本机，连上后会自动同步';
      if (btn) btn.textContent = '重新查找';
    } else if (!lastOk) {
      box.className = 'pair-state wait';
      txt.innerHTML = `🤝 已自动配对：<b>${esc(who)}</b> · 正在等对方回应…`;
      if (btn) btn.textContent = '重新查找';
    } else {
      box.className = 'pair-state ok';
      txt.innerHTML = `🤝 已自动配对：<b>${esc(who)}</b> · 实时同步中`
        + `（最近一次对上 ${fmtAgo(lastOk)}）`;
      if (btn) btn.textContent = '重新查找';
    }
  } else if (live.auto === false) {
    box.className = 'pair-state';
    txt.textContent = '自动同步已被手动暂停。点右边按钮可重新自动配对。';
    if (btn) btn.textContent = '立即查找对方';
  } else {
    box.className = 'pair-state wait';
    txt.textContent = live.url
      ? `正在自动连接 ${live.url}…（对方没开机就每分钟左右重试一次）`
      : '正在自动寻找同一局域网里的另一台电脑…（找到同名项目就自动开启，无需任何设置）';
    if (btn) btn.textContent = '立即查找对方';
  }
}

function applyLive(live, fillInputs) {
  renderPairState(live);
  const running = !!live.running;
  const pill = $('#livePill');
  if (pill) {
    pill.hidden = !running;
    // 顶部这颗胶囊是唯一「不点开任何标签就能看到」的同步状态：
    // 掉线时必须在这里就说出来，否则用户不会去翻「双机同步」那一页
    const stale = liveStale(live);
    pill.classList.toggle('bad', stale);
    if (running) {
      const lastOk = Number(live.last_ok || 0);
      pill.textContent = stale
        ? `⚠ 对方 ${fmtAgo(lastOk) || '很久'}没联系`
        : '● 实时同步中';
      pill.title = stale
        ? ('对方已掉线，正在自动重试' + (live.last_error ? '：' + live.last_error : '')
           + '；这期间的改动都在本机，连上后会自动同步')
        : '正在和对方实时对账';
    }
  }
  const dot = $('#liveDot');
  if (dot) dot.classList.toggle('on', running);
  const st = $('#liveState');
  if (st) {
    if (running) {
      const when = live.last_check ? fmtTime(live.last_check) : '等待第一次对账';
      let s = `● 运行中 · ${live.peer_name || live.url || '对方'} · 最近对账 ${when}`;
      if (live.mode === 'split' && live.my_branch) {
        const others = (live.peer_branches || [])
          .map((b) => b.branch + (b.state === 'diverged' ? '（分叉）' : ''))
          .join('、');
        s = `● 一人一条线 · 我在「${live.my_branch}」`
          + (others ? ` · 对方在「${others}」` : '')
          + ` · 最近对账 ${when}`;
      }
      if (live.state && live.state !== 'idle') s += ' · ' + (LIVE_STATE[live.state] || live.state);
      if (live.last_error) s += ' · ⚠ ' + live.last_error;
      st.textContent = s;
    } else {
      st.textContent = live.url ? '已暂停（对方地址已保存）' : '未开启';
    }
  }
  if (!fillInputs) return;
  if (live.url && $('#liveUrl') && !$('#liveUrl').value) $('#liveUrl').value = live.url;
  if (live.url && $('#liveUrl') && !$('#liveUrl').dataset.touched) $('#liveUrl').value = live.url;
  if ($('#liveInterval')) $('#liveInterval').value = live.interval || 15;
  if ($('#livePull')) $('#livePull').checked = live.auto_pull !== false;
  if ($('#livePush')) $('#livePush').checked = live.auto_push !== false;
  if ($('#liveMerge')) $('#liveMerge').checked = live.auto_merge !== false;
  if ($('#liveMode')) $('#liveMode').value = live.mode === 'split' ? 'split' : 'shared';
  applyLiveMode();
  if ($('#liveCheckout')) $('#liveCheckout').checked = live.checkout !== false;
  if ($('#liveWatch')) $('#liveWatch').checked = !!live.auto_watch;
  if ($('#watchOn') && live.watch !== undefined) $('#watchOn').checked = !!live.watch;
}

/* 一人一条线时，「分叉自动合并」这个开关就没有意义了：不合并是我选的。
   灰掉它而不是删掉，用户还能切回共用一条线。 */
function applyLiveMode() {
  const sel = $('#liveMode');
  if (!sel) return;
  const split = sel.value === 'split';
  const cb = $('#liveMerge');
  if (cb) {
    cb.disabled = split;
    if (split) cb.checked = false;
  }
  const hint = $('#liveModeHint');
  if (hint) {
    hint.textContent = split
      ? '一人一条线：我记的进我的分支、他记的进他的分支，两条线都能看见对方的版本；要不要并、并哪几个，你自己点「并入」决定——程序不会替你们合。'
      : '共用一条线：两个人往同一条分支上记，分叉时程序自动做三路合并，冲突默认保留本机。';
  }
}

/* 实时刷新：有人动就 3 秒一次，静置两分钟后降到 12 秒一次。
   这台机器常年开着，没人看的时候没必要每 3 秒扫一遍几千个文件的目录；
   而只要动一下鼠标/敲个键、或者对方真记了新版本，立刻回到 3 秒。 */
/* 三个数可以被测试覆盖（window.__zgrFeed*），生产环境就是 3 秒 / 12 秒 / 40 次 */
const FEED_FAST = window.__zgrFeedFast || 3000;
const FEED_SLOW = window.__zgrFeedSlow || 12000;
const FEED_IDLE_TICKS = window.__zgrFeedIdleTicks || 40;   // 40 次没变化 = 2 分钟

function ensureFeed() {
  if (S.liveTimer) return;
  S.idleTicks = 0;
  const schedule = (ms) => {
    clearTimeout(S.liveTimer);
    S.liveTimer = setTimeout(tick, ms);
  };
  const tick = async () => {
    S.liveTimer = 0;
    if (!S.path) return undefined;
    if (document.hidden) return schedule(FEED_SLOW);
    const before = S.liveSeq;
    await loadFeed(false);
    await pollStatus();
    S.idleTicks = (S.liveSeq !== before) ? 0 : (S.idleTicks || 0) + 1;
    return schedule(S.idleTicks > FEED_IDLE_TICKS ? FEED_SLOW : FEED_FAST);
  };
  const wake = () => {
    if (!S.idleTicks) return;        // 已经在快节奏上，不用重排
    S.idleTicks = 0;
    schedule(FEED_FAST);
  };
  ['mousemove', 'keydown', 'click', 'wheel'].forEach((ev) =>
    window.addEventListener(ev, wake, { passive: true }));
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) wake();
  });
  schedule(FEED_FAST);
}

async function loadFeed(initial) {
  if (!S.path || S.feedBusy) return;
  // 页面整夜开着：日期一变就把时间线和「今天」那一行重新算一遍。
  // 否则「今天 17 个版本」挂着昨天的数，第二天点开又是另一天，两处对不上。
  if (!initial && S.dayStamp !== dayKey(Date.now())) refreshQuiet();
  S.feedBusy = true;
  try {
    const d = await api('/api/events', { path: S.path, since: S.liveSeq });
    applyLive(d.live || {}, initial);
    const evs = d.events || [];
    S.liveSeq = Math.max(S.liveSeq, d.seq || 0);
    if (initial) {
      S.events = evs.slice(-80);
      renderFeed();
      return;
    }
    if (!evs.length) return;
    S.events = S.events.concat(evs).slice(-80);
    renderFeed();
    const last = evs[evs.length - 1];
    if (last.kind === 'peer_pull' || last.kind === 'peer_merge') {
      toast(`✓ ${last.author || '对方'}：${last.message || '同步了新版本'}`, 4200);
    } else if (last.kind === 'peer_pending') {
      toast('⚠ 对方有新版本，但本机工作区有改动，没有自动覆盖', 4200);
    } else if (last.kind === 'peer_push') {
      toast('↑ 已把本机版本自动同步给对方', 3000);
    } else if ((last.kind === 'commit' || last.kind === 'auto_commit')
               && last.author && last.author !== $('#authorName').value) {
      toast(`${last.author} 记录了新版本`, 3200);
    }
    if (d.head && S.project && d.head !== S.project.head) refreshQuiet();
  } catch (e) {
    /* 对账失败不打扰用户，状态行里会显示 */
  } finally {
    S.feedBusy = false;
  }
}

function renderFeed() {
  const box = $('#liveFeed');
  if (!box) return;
  if (!S.events.length) {
    box.innerHTML = '<div class="muted small">还没有动态。开启实时同步后，对方的一举一动都会出现在这里。</div>';
    return;
  }
  box.innerHTML = S.events.slice().reverse().slice(0, 60).map((e) => {
    const c = e.counts || {};
    const delta = (c.added || c.modified || c.deleted)
      ? ` <i class="ev-d">+${c.added || 0} ~${c.modified || 0} −${c.deleted || 0}</i>` : '';
    const files = (e.files || []).slice(0, 8).map((f) =>
      `<span class="ev-file ${esc(f.st || '')}" title="${esc(f.path)}">${esc(baseName(f.path))}</span>`).join('');
    const act = e.kind === 'peer_conflict'
      ? '<button class="mini" data-conflict="1">去选择</button>'
      : (e.head ? `<button class="mini" data-ver="${esc(e.head)}"
          title="打开这一版：看总文件数、对方改了什么、每个文件的缩略图">看这一版</button>` : '');
    return `<div class="ev ${esc(e.kind || '')}">
      <span class="ev-when">${fmtTime(e.t)}</span>
      <span class="ev-who">${esc(e.author || '系统')}</span>
      <span class="ev-what"><b>${esc(EVENT_KIND[e.kind] || e.kind || '动态')}</b>
        ${esc(e.message || '')}${delta} ${act}</span>
      ${files ? `<div class="ev-files">${files}${(e.files || []).length > 8 ? ' …' : ''}</div>` : ''}
    </div>`;
  }).join('');
  box.querySelectorAll('[data-conflict]').forEach((b) => {
    b.onclick = () => {
      const tab = document.querySelector('[data-tab="sync"]');
      if (tab) tab.click();
      planMerge();
    };
  });
  box.querySelectorAll('[data-ver]').forEach((b) => {
    b.onclick = (ev) => {
      ev.stopPropagation();
      openVersionFromFeed(b.dataset.ver);
    };
  });
}

/* 从实时动态跳到某个版本：时间线选中它，右边就是完整清单（总文件数、改动、缩略图、操作） */
async function openVersionFromFeed(head) {
  if (!head || !S.path) return;
  const tab = document.querySelector('[data-tab="timeline"]');
  if (tab) tab.click();
  if (!S.commits.some((c) => c.id === head)) {
    try { await refreshQuiet(); } catch (e) { /* 下一次轮询还会试 */ }
  }
  if (!S.commits.some((c) => c.id === head)) return toast('这一版还没同步下来，稍等几秒再点', 3500);
  await selectCommit(head);
  const el = document.querySelector('#timeline .commit.active');
  if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' });
}

async function refreshQuiet() {
  if (!S.path) return;
  try {
    const d = await api('/api/project', { path: S.path });
    S.project = d;
    $('#headPill').textContent = d.head ? d.head.slice(0, 10) : '尚未记录';
    $('#headPill').classList.toggle('dirty', !!(d.status && !d.status.clean));
    renderBanner();
    renderPending();
    const keep = S.selected;
    await loadTimeline();
    if (keep && S.commits.some((c) => c.id === keep)) selectCommit(keep);
    else renderDetail();
  } catch (e) { /* 静默 */ }
}

async function saveLive(on) {
  if (!S.path) return toast('请先选择项目');
  const url = $('#liveUrl').value.trim().replace(/\/+$/, '');
  if (on && !url) {
    // 地址留空也不用用户去查：直接走局域网自动配对
    if ($('#btnAutoPair')) $('#btnAutoPair').click();
    else toast('正在自动寻找对方…');
    return;
  }
  try {
    const r = await post('/api/live', {
      path: S.path, enabled: on, url,
      interval: Number($('#liveInterval').value) || 15,
      auto_pull: $('#livePull').checked, auto_push: $('#livePush').checked,
      auto_merge: $('#liveMerge').checked, checkout: $('#liveCheckout').checked,
      auto_watch: $('#liveWatch') ? $('#liveWatch').checked : false,
      mode: $('#liveMode') ? $('#liveMode').value : 'shared',
    });
    applyLive(r, true);
    if (on) {
      toast('✓ 实时同步已开启', 3000);
      if ($('#syncUrl') && url) $('#syncUrl').value = url;
    } else toast('已暂停实时同步');
  } catch (e) { toast('设置失败：' + e.message, 5000); }
}

/* ------------------------------------------------------- 远端存储（NAS/WebDAV/S3） */
const REMOTE_KIND_LABEL = {
  local: '共享目录', webdav: 'WebDAV', s3: 'S3', http: '另一台机器', share: '共享目录',
};

function showRemoteFields() {
  const k = $('#remoteKind').value;
  $$('[data-rkind]').forEach((el) => {
    el.style.display = el.dataset.rkind === k ? '' : 'none';
  });
}

async function loadRemotes() {
  const box = $('#remoteList');
  if (!S.path) { box.innerHTML = '<div class="muted small">先选择项目。</div>'; return; }
  try {
    const d = await api('/api/remotes', { path: S.path });
    S.remotes = d.remotes || [];
  } catch (e) {
    box.innerHTML = `<div class="err small">读取失败：${esc(e.message)}</div>`;
    return;
  }
  if (!S.remotes.length) {
    box.innerHTML = '<div class="muted small">还没有配置远端。下面填好点「保存并测试」即可。</div>';
    return;
  }
  box.innerHTML = S.remotes.map((r) => {
    const target = r.path || r.url || r.bucket || '';
    const bits = [REMOTE_KIND_LABEL[r.kind] || r.kind, target,
      r.user ? '用户 ' + r.user : '',
      r.has_secret ? '已存密钥' : '无密钥'].filter(Boolean).map(esc);
    return `<div class="backup-row" data-remote="${esc(r.name)}">
      <div class="bk-main"><b>${esc(r.name)}</b></div>
      <div class="bk-sub">${bits.join(' · ')}</div>
      <button class="ghost" data-act="test">测试</button>
      <button class="ghost" data-act="sync">同步</button>
      <button class="danger-ghost" data-act="del">删除</button>
    </div>`;
  }).join('');
  $$('.backup-row', box).forEach((row) => {
    $$('button[data-act]', row).forEach((b) => {
      b.onclick = () => remoteAction(row.dataset.remote, b.dataset.act);
    });
  });
}

async function saveRemote() {
  if (!S.path) return toast('请先选择项目');
  const kind = $('#remoteKind').value;
  // 注意：body.path 是本地项目目录；远端自己的目录叫 local_path，别混
  const body = { path: S.path, kind, name: $('#remoteName').value.trim() };
  if (!body.name) return toast('给这个远端起个名字');
  if (kind === 'local') body.local_path = $('#remotePath').value.trim();
  if (kind === 'webdav') {
    body.url = $('#remoteDavUrl').value.trim();
    body.user = $('#remoteDavUser').value.trim();
    body.password = $('#remoteDavPass').value;
  }
  if (kind === 's3') {
    body.endpoint = $('#remoteS3Endpoint').value.trim();
    body.bucket = $('#remoteS3Bucket').value.trim();
    body.region = $('#remoteS3Region').value.trim() || 'us-east-1';
    body.access_key = $('#remoteS3Ak').value.trim();
    body.secret_key = $('#remoteS3Sk').value;
  }
  if (kind === 'local' && !body.local_path) return toast('填一下共享目录路径');
  try {
    await post('/api/remote/save', body);
    $('#remoteMeta').textContent = '已保存，正在测试…';
    await loadRemotes();
    const t = await post('/api/remote/test', { path: S.path, name: body.name });
    $('#remoteMeta').textContent = t.kind === 'http'
      ? `✓ 连上「${t.remote || '对端'}」` : `✓ 读写正常，已有 ${t.objects || 0} 个对象`;
    toast('✓ 远端已保存并验证通过', 3200);
  } catch (e) {
    $('#remoteMeta').textContent = '';
    toast('保存失败：' + e.message, 6000);
  }
}

async function remoteAction(name, act) {
  if (!S.path) return;
  if (act === 'del') {
    if (!confirm(`删除远端「${name}」？\n\n只删本机的配置和密码，远端已经传上去的数据不动。`)) return;
    try {
      await post('/api/remote/remove', { path: S.path, name });
      toast('已删除');
      loadRemotes();
    } catch (e) { toast('删除失败：' + e.message, 5000); }
    return;
  }
  if (act === 'test') {
    toast('正在检查…', 1500);
    try {
      const t = await post('/api/remote/test', { path: S.path, name });
      toast(t.kind === 'http'
        ? `✓ 连上「${t.remote || '对端'}」`
        : `✓ 读写正常，已有 ${t.objects || 0} 个对象`, 3600);
    } catch (e) { toast('检查失败：' + e.message, 6000); }
    return;
  }
  if (act === 'sync') {
    const logEl = $('#syncLog');
    if (logEl) logEl.textContent = '正在与远端同步…';
    try {
      const r = await post('/api/remote/sync', {
        path: S.path, name,
        checkout: $('#syncCheckout') ? $('#syncCheckout').checked : true,
        force: $('#syncForce') ? $('#syncForce').checked : false,
      });
      if (logEl) {
        logEl.textContent = `远端「${name}」 · 分支 ${r.branch}\n`
          + `下载 ${r.pulled || 0} 个对象，上传 ${r.pushed || 0} 个对象\n`
          + (r.messages || []).map((m) => '· ' + m).join('\n');
      }
      toast('✓ 已与远端同步', 3000);
      if (r.checked_out) refreshQuiet();
    } catch (e) {
      if (logEl) logEl.textContent = '同步失败：' + e.message;
      toast('同步失败：' + e.message, 6000);
    }
  }
}

async function packVersion(commit, worktree) {
  if (!S.path) return toast('请先选择项目');
  toast('正在打包…', 1800);
  try {
    const r = await post('/api/pack', {
      path: S.path, commit: commit || undefined, worktree: !!worktree,
    });
    toast(`✓ 已打包 ${r.files} 个文件（${fmtSize(r.size)}），开始下载`, 4200);
    const a = document.createElement('a');
    a.href = withToken(r.download);
    a.download = r.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) { toast('打包失败：' + e.message, 5000); }
}

async function revealFile(rel) {
  if (!S.path || !rel) return;
  try {
    await post('/api/reveal', { path: S.path, file: rel });
  } catch (e) { toast(e.message, 4000); }
}

/* 用本机默认程序打开工作区里的文件（SolidWorks / CAD / 文本编辑器） */
async function openFile(rel) {
  if (!S.path || !rel) return;
  try {
    await post('/api/openfile', { path: S.path, file: rel });
  } catch (e) { toast('打开失败：' + e.message, 4500); }
}

/* 单独改一个零件：先从这个版本开一条新分支、切过去，再用默认程序打开这个文件。
   这样你在 SolidWorks 里改它、保存、回来点「立即记录」，改的就是这条新分支，
   主干和其它零件不受影响——相当于 git checkout -b 之后只动一个文件。 */
async function openPartInNewBranch(rel) {
  if (!S.path || !rel) return;
  const name0 = baseName(rel).replace(/\.[^.\/\\]+$/, '');
  const m = openModal({
    title: '单独改：' + baseName(rel),
    width: 'min(560px,100%)',
    body: `
      <p class="small">会做两件事：从 <b>当前版本</b>（${esc((S.project && S.project.head || '').slice(0, 10) || '空')}）开一条新分支并切过去，
        然后在本机用默认程序打开这个文件。</p>
      <p class="small muted">改完保存后回到这里点「立即记录」，这一版就记在新分支上；主干不受影响，
        随时可以在上面的分支板切回来。</p>
      <label class="small" style="display:block;margin:14px 0 6px">新分支名字</label>
      <input class="br-name" value="${esc('改-' + name0)}" spellcheck="false">
      <p class="small muted br-hint" style="margin-top:8px"></p>`,
    footer: '<button class="go primary">建分支并打开</button><button class="cancel">取消</button>',
  });
  const input = $('.br-name', m.body);
  const hint = $('.br-hint', m.body);
  const refreshHint = () => {
    const n = (input.value || '').trim();
    hint.textContent = Object.prototype.hasOwnProperty.call(S.heads || {}, n)
      ? '⚠ 这个名字已经是一条分支：会直接切过去（不会移动它的指针），改动记在那条分支上。'
      : '';
  };
  input.oninput = refreshHint;
  refreshHint();
  input.focus();
  input.select();
  $('.cancel', m.foot).onclick = m.close;
  const submit = async () => {
    const name = (input.value || '').trim();
    if (!name) return toast('分支名不能为空', 3000);
    if (/[\\/]/.test(name)) return toast('分支名里不能有斜杠', 3000);
    m.close();
    try {
      const r = await post('/api/branch', { path: S.path, branch: name });
      S.branch = r.branch || name;
      toast(r.created
        ? `✓ 已从当前版本分出分支 ${S.branch}，正在打开 ${baseName(rel)}`
        : `✓ 已切到已有分支 ${S.branch}，正在打开 ${baseName(rel)}`, 4200);
      await openFile(rel);
      S.selected = null;
      await loadTimeline();
      pollStatus();          // 刷新头顶那个版本号/未记录标记（loadProject 从来没定义过）
    } catch (e) {
      toast('建分支失败：' + (e.message || e), 5000);
    }
  };
  $('.go', m.foot).onclick = submit;
  input.onkeydown = (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); submit(); }
  };
}

function rollbackTo(c) {
  const m = openModal({
    title: '回溯到此版本',
    body: `<p>会把项目内容回溯到 <b>${esc((c.message || c.id.slice(0, 10)).split('\n')[0])}</b>（${fmtTime(c.time)}）。</p>
      <p class="muted small">回溯会生成一个<b>新版本</b>（父版本是当前版本），所以后面这些版本仍然留在时间线里，
      任何一版都能再回来。被覆盖或删除的文件先备份到 <code>.zgr/backup/</code>，可在设置里整批复原。</p>`,
    footer: '<button class="go primary">确认回溯</button><button class="cancel">取消</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.go', m.foot).onclick = async () => {
    m.close();
    try {
      const r = await post('/api/undo', {
        path: S.path, commit: c.id, checkout: true, backup: true,
        message: `回溯到 ${c.id.slice(0, 10)}（${(c.message || '').split('\n')[0]}）`,
      });
      toast('✓ 已回溯，生成新版本 ' + r.commit.slice(0, 10), 4200);
      await selectProject(S.path, true);
      selectCommit(r.commit);
    } catch (e) { toast('回溯失败：' + e.message, 5000); }
  };
}

function mergeFrom(c) {
  let planOurs = '';          // 核对时是基于哪一版算的；合并时带回去让服务端校验
  const m = openModal({
    title: '把这一版合并进当前版本',
    body: `<p>把 <b>${esc(c.id.slice(0, 10))}</b>（${fmtTime(c.time)}）的内容与当前版本合并，
        生成一个双父提交。</p>
      <div class="merge-plan" id="mergeFromPlan"><span class="muted small">正在核对会动到哪些文件…</span></div>`,
    footer: '<button class="go primary" disabled>智能合并</button><button class="cancel">取消</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  // 先算一遍（不落盘）：跟「恢复整个项目」一样，动手前先说清楚会发生什么。
  // 以前这里只有一句「合并后仍可撤销」，用户根本不知道有几个文件会冲突，
  // 只能先按下去再看结果——而合并是要写工作区的。
  post('/api/merge/from', { path: S.path, commit: c.id, apply: false })
    .then((p) => {
      planOurs = p.ours || '';
      const box = $('#mergeFromPlan', m.body);
      if (!box) return;
      const counts = p.counts || {};
      const changed = (p.entries || []).filter((e) => e.status !== 'same');
      if (!changed.length) {
        box.innerHTML = '<p class="muted small">这一版和当前版本的内容完全一样，没有要合并的东西。</p>';
        return;
      }
      // 「一边改了、另一边删了」和「只有一边改过」不是一回事：合并动作安全
      // （默认不替你删文件 / 跟着删），但用户得知道对方到底动了没动。
      let mdTheirs = 0;               // 对方改了、本机删了
      let mdOurs = 0;                 // 本机改了、对方删了
      (p.entries || []).forEach((e) => {
        if (e.status === 'theirs_only' && e.base) mdTheirs++;
        else if (e.status === 'ours' && !e.theirs && e.base) mdOurs++;
      });
      const theirSide = Math.max(0, (counts.theirs || 0)
        + (counts.theirs_only || 0) - mdTheirs);
      const conflicts = p.conflicts || [];
      const rows = [];
      rows.push(`<li>共 <b>${changed.length}</b> 个文件不一样</li>`);
      if (theirSide) rows.push(`<li><b>${theirSide}</b> 个只有对方改过 → 直接采用对方</li>`);
      const oursOnly = Math.max(0, (counts.ours || 0) - mdOurs);
      if (oursOnly) rows.push(`<li><b>${oursOnly}</b> 个只有本机改过 → 保持不动</li>`);
      if (mdTheirs) {
        rows.push(`<li><b>${mdTheirs}</b> 个对方改了、本机这边删了 → 恢复成对方的版本</li>`);
      }
      if (mdOurs) {
        rows.push(`<li><b>${mdOurs}</b> 个本机改了、对方那边删了 → 本机留着（不替你删文件）</li>`);
      }
      if (counts.theirs_deleted) {
        rows.push(`<li><b>${counts.theirs_deleted}</b> 个对方那边删了 → 本机留着（不替你删文件）</li>`);
      }
      if (counts.ours_deleted) {
        rows.push(`<li><b>${counts.ours_deleted}</b> 个本机这边删了 → 跟着删</li>`);
      }
      if (counts.conflict) {
        rows.push(`<li><b>${counts.conflict}</b> 个两边都改了 → 逐行三路合并，
          合不动的保留本机内容（不会丢）</li>`);
      }
      box.innerHTML = `<ul class="merge-plan-list">${rows.join('')}</ul>`
        + (conflicts.length
          ? `<div class="merge-plan-files"><div class="muted small">两边都改了的文件：</div>`
            + conflicts.slice(0, 6).map((pth) => `<div class="mf">${esc(pth)}</div>`).join('')
            + (conflicts.length > 6
              ? `<div class="muted small">还有 ${conflicts.length - 6} 个…</div>` : '')
            + '</div>'
          : '')
        + '<p class="muted small">合并后仍可随时「撤销这次合并」。</p>';
      const go = $('.go', m.foot);
      go.disabled = false;
      go.textContent = `智能合并（${changed.length} 个文件）`;
    })
    .catch((e) => {
      const box = $('#mergeFromPlan', m.body);
      if (box) box.innerHTML = `<p class="bom-warn">算不出合并计划：${esc(e.message)}</p>`;
    });
  $('.go', m.foot).onclick = async () => {
    // 成功才关窗：万一服务端回 409「核对之后又记了一版」，这一页就是用户
    // 唯一的上下文——先关掉，他得从时间线重新找到这一版再来一遍
    const btn = $('.go', m.foot);
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '合并中…';
    try {
      const r = await post('/api/merge/from', {
        path: S.path, commit: c.id, apply: true, strategy: 'smart', expect: planOurs,
        message: `合并历史版本 ${c.id.slice(0, 10)}`,
      });
      m.close();
      toast(`✓ 已合并 ${r.files} 个文件，新版本 ${r.commit.slice(0, 10)}`, 4500);
      await selectProject(S.path, true);
      selectCommit(r.commit);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = oldText;
      toast('合并失败：' + e.message, 6000);
    }
  };
}

/* ------------------------------------------------------------------ 回收站 */
async function loadBackups() {
  if (!S.path) return toast('请先选择项目');
  const box = $('#backupList');
  box.innerHTML = '<div class="muted small">读取中…</div>';
  try {
    const d = await api('/api/backups', { path: S.path });
    S.backups = d.backups || [];
    S.backupPolicy = d;
    // 说清「它自己会瘦身」，否则用户看到回收站几百 MB 只会担心盘被吃满
    const policy = d.auto_prune === false ? '自动清理：已关'
      : `自动清理：超过 ${d.max_mb || 2048} MB 时删最旧的（至少留 ${d.min_keep || 10} 批）`;
    $('#backupMeta').textContent = S.backups.length
      ? `${S.backups.length} 批备份 · 共 ${fmtSize(d.total_bytes || 0)} · `
        + `${policy} · 自动更新文件夹：${d.auto_checkout ? '开' : '关'}`
      : `还没有备份 · ${policy}`;
    if (!S.backups.length) {
      box.innerHTML = '<div class="muted small">还没有备份。同步/合并/恢复/回溯时被覆盖或删除的文件会自动出现在这里。</div>';
      return;
    }
    box.innerHTML = S.backups.map((b) => `
      <div class="backup-row" data-name="${esc(b.name)}">
        <div class="bk-main"><b>${fmtTime(b.time)}</b>
          <span class="muted small">${esc(b.name)}</span></div>
        <div class="bk-sub">${b.files} 个文件 · ${fmtSize(b.bytes)}
          <i title="${esc((b.sample || []).join('\\n'))}">${esc((b.sample || []).slice(0, 4).join('、'))}${b.files > 4 ? ' …' : ''}</i></div>
        <div class="bk-acts">
          <button class="bk-see ghost">看文件</button>
          <button class="bk-go ghost">复原这批</button>
        </div>
      </div>`).join('');
    $$('.backup-row', box).forEach((row) => {
      $('.bk-go', row).onclick = () => restoreBackup(row.dataset.name);
      $('.bk-see', row).onclick = () => showBackupFiles(row, row.dataset.name);
    });
  } catch (e) {
    box.innerHTML = `<div class="err small">${esc(e.message)}</div>`;
  }
}

/* 用错一个零件就想找回那一个：整批复原会把当时被覆盖的文件一起盖回去。
   点「看文件」展开这批备份的清单，逐个恢复；恢复前当前文件同样会再备份一次。 */
async function showBackupFiles(row, name) {
  const old = $('.bk-files', row);
  if (old) { old.remove(); return; }          // 再点一次收起
  const box = document.createElement('div');
  box.className = 'bk-files';
  box.innerHTML = '<div class="muted small">读取中…</div>';
  row.appendChild(box);
  try {
    const d = await api('/api/backup/files', { path: S.path, name, limit: 500 });
    if (!d.total) {
      box.innerHTML = '<div class="muted small">这批备份里没有文件。</div>';
      return;
    }
    box.innerHTML = `
      <div class="muted small">共 ${d.total} 个文件${d.truncated ? `（只列前 ${d.files.length} 个）` : ''}</div>
      ${d.files.map((f) => `
        <div class="bk-file" data-file="${esc(f.path)}">
          <span class="bk-path">${esc(f.path)}</span>
          <span class="muted small">${fmtSize(f.size)} · ${fmtTime(f.time)}</span>
          <button class="bk-one ghost">恢复这个</button>
        </div>`).join('')}`;
    $$('.bk-file', box).forEach((r) => {
      $('.bk-one', r).onclick = () => restoreBackupFile(name, r.dataset.file);
    });
  } catch (e) {
    box.innerHTML = `<div class="err small">${esc(e.message)}</div>`;
  }
}

function restoreBackupFile(name, file) {
  if (!confirm(`把「${file}」恢复成 ${name} 那一版？\n\n当前这个文件会先另存一批备份，恢复错了还能再恢复回来。\n（只改磁盘上的文件，想留成版本还要再记录一次）`)) return;
  post('/api/backup/restore_file', { path: S.path, name, file })
    .then(async (r) => {
      toast(`✓ 已恢复 ${r.file}（${fmtSize(r.size)}）`, 5000);
      await refreshQuiet();
      await loadBackups();
    })
    .catch((e) => toast('恢复失败：' + e.message, 6000));
}

function restoreBackup(name) {
  if (!confirm(`把这批备份（${name}）里的文件放回项目目录？\n\n当前同名文件会先再备份一次，之后仍然能反悔。`)) return;
  post('/api/backup/restore', { path: S.path, name })
    .then(async (r) => {
      toast(`✓ 已复原 ${r.count} 个文件；记录下来就是一个新版本`, 4500);
      await refreshQuiet();
      await loadBackups();
    })
    .catch((e) => toast('复原失败：' + e.message, 5000));
}

/* ------------------------------------------------------------------ 时间线 */
async function loadTimeline() {
  if (!S.path) return;
  // graph=1：所有分支可达的提交 + 完整 parents，界面才画得出分叉。
  // 一次最多取 S.logLimit 个：几千个版本的项目全量画出来会让首屏卡住，
  // 但**必须让用户知道「更早的还没加载」**，否则老版本看上去像是不见了。
  // today_from：把**浏览器这边的**今天零点报给服务端。局域网里另一台机器打开
  // 界面时，服务端和浏览器可能不是同一个时区，按服务端的零点数会对不上卡片
  const d = await api('/api/log', { path: S.path, limit: S.logLimit, changes: 1, graph: 1,
    today_from: localDayStart() });
  S.commits = d.commits || [];
  // 「还有更早的没加载」由服务端说了算：以前拿`条数 >= 上限`猜，项目正好有
  // 上限那么多版本时会一直说「更早的还没加载」，点一下才自愈
  S.logHasMore = d.has_more !== undefined
    ? !!d.has_more : S.commits.length >= S.logLimit;
  S.todayInfo = d.today || null;
  S.heads = d.heads || {};
  S.branchStatus = d.branch_status || {};
  S.branch = d.branch || '';
  if ($('#branchName')) $('#branchName').textContent = d.branch ? `⎇ ${d.branch}` : '';
  // 先算「今天」再画卡片：换到今天的版本一个都没有的项目时，
  // 摘要会把筛子放开，卡片才不至于对着一个空筛子画
  renderDayNote();
  renderTimeline();
}

/* 「今天改了什么」：早上坐下来第一句想知道的就是这个。
   数的是**今天全部**的版本，不受「只看我的」和搜索框影响——那两个筛的是卡片，
   这一行回答的是「我们改了什么」。没有今天的版本就整行收起来，不占地方。 */
function dayKey(ms) {
  const d = new Date(ms);
  return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
}

/* 本地今天零点（毫秒）。「今天」的口径以看这块屏幕的人为准 */
function localDayStart() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function renderDayNote() {
  const el = $('#dayNote');
  if (!el) return;
  const row = $('#dayNoteRow');
  const today = dayKey(Date.now());
  S.dayStamp = today;
  const rows = (S.commits || []).filter((c) => c.time && dayKey(c.time) === today);
  // 今天到底几个版本以服务端为准（它会从分支头往回数到今天零点为止，不受
  // 「时间线只取最近一屏」影响）；服务端没给（老服务端）才退回数这一屏
  const info = S.todayInfo && (!S.todayInfo.since || dayKey(S.todayInfo.since) === today)
    ? S.todayInfo : null;
  const total = info ? Number(info.count) || 0 : rows.length;
  if (!rows.length && !total) {
    el.hidden = true;
    el.innerHTML = '';
    el.classList.remove('on');
    el.setAttribute('aria-pressed', 'false');
    if (row) row.hidden = true;
    // 今天的版本被删干净（换了项目、或本来昨天记的已跨天）：
    // 筛子自动放开，不把人关在一片空白里
    S.todayOnly = false;
    return;
  }
  // 服务端没给（老服务端、或数的时候出错）就只能数这一屏：这时只要还有更早的
  // 没加载，这个数就是下限，必须写「至少」——不能回退成像准数一样的口吻
  const atLeast = info ? !!info.truncated : !!S.logHasMore;
  const partial = total > rows.length;
  // 文件数和每个人几版只能数这一屏里的；和总数对不上时必须说清是哪一部分的数，
  // 不然「改了 40 个文件」「张三 0」会被当成今天的全部
  let detail;
  if (partial) {
    detail = ` · 下面画了 <b>${rows.length}</b> 个`
      + `<span class="dn-hint">${S.logHasMore ? ' · 点「加载更早的」看全今天' : ''}</span>`;
  } else {
    const files = rows.reduce((n, c) => n + (((c.changes || {}).total) || 0), 0);
    const by = new Map();
    rows.forEach((c) => {
      const k = (c.author || '').trim() || '（没记名字）';
      by.set(k, (by.get(k) || 0) + 1);
    });
    const who = [...by.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([k, n]) => `${esc(k)} ${n}`).join(' / ');
    detail = (files ? ` · 改了 <b>${files}</b> 个文件` : '') + ` · ${who}`
      + `<span class="dn-hint">${S.todayOnly ? ' · 只看今天 ✓' : ' · 点一下只看今天'}</span>`;
  }
  el.hidden = false;
  if (row) row.hidden = false;
  el.classList.toggle('on', !!S.todayOnly);
  el.setAttribute('aria-pressed', S.todayOnly ? 'true' : 'false');
  el.title = S.todayOnly ? '点一下看今天的和更早的全部版本' : '点一下只留今天记的版本';
  el.innerHTML = (atLeast ? `今天 至少 <b>${total}</b> 个版本`
    : `今天 <b>${total}</b> 个版本`) + detail;
  // 复制出去的那段话按「人有几个、谁几版、最近改了什么」组织——粘进群里
  // 别人不用点开任何东西就能看懂
  _todayText = todaySummaryText(rows, total, partial);
}

/* 今天这段摘要的纯文本版（复制用，不带 HTML）。只包含**今天**的版本；
   这一屏没画全时也不假装知道文件数。 */
let _todayText = '';
function todaySummaryText(rows, total, partial) {
  const name = (S.project && S.project.name) || '';
  const lines = [];
  lines.push(`${name ? `【${name}】` : ''}今天 ${total} 个版本`);
  if (!partial) {
    const files = rows.reduce((n, c) => n + (((c.changes || {}).total) || 0), 0);
    if (files) lines.push(`改了 ${files} 个文件`);
    const by = new Map();
    rows.forEach((c) => {
      const k = (c.author || '').trim() || '（没记名字）';
      by.set(k, (by.get(k) || 0) + 1);
    });
    if (by.size) {
      lines.push([...by.entries()].sort((a, b) => b[1] - a[1])
        .map(([k, n]) => `${k} ${n} 版`).join(' / '));
    }
  } else {
    lines.push(`（列表只加载了最近 ${rows.length} 个）`);
  }
  rows.slice(0, 8).forEach((c) => {
    const t = new Date(c.time || 0);
    const hm = String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
    const msg = (c.message || '').split('\n')[0] || '(无说明)';
    const who = (c.author || '').trim();
    lines.push(`· ${hm} ${msg}${who ? `（${who}）` : ''}`);
  });
  if (rows.length > 8) lines.push(`…还有 ${rows.length - 8} 个`);
  return lines.join('\n');
}

/* 复制今日摘要。http 下的非 localhost 页面没有 navigator.clipboard，
   退回到临时 textarea + execCommand，别让用户点了没反应。 */
async function copyToday() {
  const text = _todayText || '';
  if (!text) { toast('今天还没有版本，没有可复制的摘要', 3000); return; }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (!ok) throw new Error('execCommand 失败');
    }
    toast('已复制今天的摘要，可以直接粘到群里', 3000);
  } catch (e) {
    toast('复制失败：浏览器不让写剪贴板，可以手动选中那一行', 4000);
  }
}

/* 点「今天」那一行：只留今天的版本。和「只看我的」是两把筛子，可以叠着用；
   再点一次放开。卡片筛掉之后，抬头必须说清「筛了、另有几个是更早的」——
   不然看不见的版本在用户眼里就是丢了。 */
function toggleToday() {
  if (!S.todayOnly) {
    const today = dayKey(Date.now());
    const n = (S.commits || []).filter((c) => c.time && dayKey(c.time) === today).length;
    const info = S.todayInfo && (!S.todayInfo.since || dayKey(S.todayInfo.since) === today)
      ? S.todayInfo : null;
    if (!n && !(info && Number(info.count) > 0)) {
      toast('今天还没有版本，没得筛', 3200);
      return;
    }
  }
  S.todayOnly = !S.todayOnly;
  renderDayNote();
  renderTimeline();
}

/* 时间线只显示最近 N 个版本；点一下往后翻一倍。
   老项目（167/174 上那些）动辄上千个版本，全量取会让首屏等好几秒——
   但「看不见」和「不存在」在用户眼里是一回事，所以宁可多一个按钮。 */
async function loadMoreLog() {
  const before = S.commits.length;
  S.logLimit = Math.min(20000, S.logLimit * 4);
  await loadTimeline();
  toast(S.logHasMore
    ? `已加载最近 ${S.commits.length} 个版本（还有更早的）`
    : `已加载全部 ${S.commits.length} 个版本`, 3200);
  return before;
}

/* 从选中的版本（或最新版本）分出一条新分支，并切过去。
   分出去之后时间线上就能看到两条线，而不是永远一根串珠。 */
async function newBranch() {
  if (!S.path) return;
  const base = S.selected || (S.commits[0] && S.commits[0].id);
  const m = openModal({
    title: '新建分支',
    width: 'min(460px,100%)',
    body: `<div class="field"><label>分支名</label>
      <input class="bn" placeholder="例如：改料盘方案" style="width:100%"></div>
      <div class="muted">从版本 <span class="mono">${base ? base.slice(0, 10) : '(还没有任何记录)'}</span> 分出去。
      新建后当前分支会切到它，之后的「立即记录」都记在这条线上。</div>`,
    footer: '<button class="primary go">创建并切换</button><button class="cancel">取消</button>',
  });
  const input = $('.bn', m.body);
  input.focus();
  const go = async () => {
    const name = input.value.trim();
    if (!name) return toast('请填一个分支名');
    $('.go', m.el).disabled = true;
    try {
      const d = await post('/api/branch', {
        path: S.path, branch: name, commit: base || '',
      });
      m.close();
      toast(`✓ 已切到分支 ${d.branch}`, 3000);
      S.selected = null;
      await loadTimeline();
      pollStatus();          // 刷新头顶那个版本号/未记录标记（loadProject 从来没定义过）
    } catch (e) {
      $('.go', m.el).disabled = false;
      toast(e.message || String(e), 5000);
    }
  };
  $('.go', m.el).onclick = go;
  $('.cancel', m.el).onclick = m.close;
  input.onkeydown = (e) => { if (e.key === 'Enter') go(); };
}
if ($('#btnBranch')) $('#btnBranch').onclick = newBranch;

/* 分支板：一条分支 = 一列卡片，横向滑动切换。
   每条分支从自己的头提交沿第一父链往回收，收到「已被别的分支认领」的提交就停，
   那个位置就是分叉点 —— 显示在列的底部「从 xxx 分出」。
   这样分支是看得见的方块/列，而不是挤在左边的一根竖条。 */
const LANE_COLORS = ['#2f6bff', '#e8912d', '#2f9e44', '#c2255c',
  '#7048e8', '#0b7285', '#a61e4d', '#5c940d'];
const MARK_COLORS = { red: '#e03131', yellow: '#f59f00', green: '#2f9e44' };

function layoutBoard(commits, heads, current, status) {
  const byId = new Map(commits.map((c) => [c.id, c]));
  const names = Object.keys(heads || {});
  if (!names.length && commits.length) names.push(current || 'main');
  const st = status || {};
  const rank = new Map(commits.map((c, i) => [c.id, i]));
  // 认领顺序按服务端给的固定顺序，和「当前在哪条分支」无关：
  // 否则切一次分支，别的列上「从 xxx 分出」的标签会跟着变，看着像历史被改了。
  const claimOrder = names.slice();
  // 摆列顺序：当前分支永远第一列 → 还活着（没合并）的分支按最新提交 → 已合并的收到最后。
  // 不然合并几次之后横向排一排「死分支」，越用越难看。
  const display = names.slice().sort((a, b) => {
    const am = a === current ? 0 : (st[a] && st[a].merged ? 2 : 1);
    const bm = b === current ? 0 : (st[b] && st[b].merged ? 2 : 1);
    if (am !== bm) return am - bm;
    const ra = rank.has(heads[a]) ? rank.get(heads[a]) : 1e9;
    const rb = rank.has(heads[b]) ? rank.get(heads[b]) : 1e9;
    return ra - rb;
  });

  const owner = new Map();
  const built = new Map();
  for (const name of claimOrder) {
    const info = st[name] || {};
    const lane = {
      name,
      head: heads[name] || (commits[0] && commits[0].id),
      commits: [],
      forkAt: null,
      forkFrom: null,
      current: name === current,
      merged: !!info.merged,
      mergedInto: info.merged_into || info.same_as || null,
      sameAs: info.same_as || null,
    };
    let cur = lane.head;
    const seen = new Set();
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      if (owner.has(cur)) {
        lane.forkAt = cur;
        lane.forkFrom = owner.get(cur);
        lane.forkCommit = byId.get(cur) || null;
        break;
      }
      const c = byId.get(cur);
      if (!c) { lane.forkAt = cur; break; }
      owner.set(cur, name);
      lane.commits.push(c);
      cur = (c.parents || [])[0];
    }
    built.set(name, lane);
  }
  const lanes = display.map((n) => built.get(n)).filter(Boolean);
  // 兜底：没被任何分支「第一父链」认领的提交，别让它们凭空消失。
  // 但它们其实分两种：一种还被某个分支的合并提交带着（比如分支合并完又删掉了），
  // 那些是「已合并的历史」；一种哪个头都走不到，才是真正的「其它」。
  const rest = commits.filter((c) => !owner.has(c.id));
  if (rest.length) {
    const reach = new Set();
    const stack = [];
    for (const l of lanes) {
      if (l.head && byId.has(l.head)) stack.push(l.head);
    }
    while (stack.length) {
      const id = stack.pop();
      if (reach.has(id)) continue;
      reach.add(id);
      const c = byId.get(id);
      if (c) stack.push(...(c.parents || []));
    }
    const hist = rest.filter((c) => reach.has(c.id));
    const junk = rest.filter((c) => !reach.has(c.id));
    if (hist.length) {
      lanes.push({ name: '已合并的历史', head: hist[0].id, commits: hist, forkAt: null,
                   forkFrom: null, current: false, orphan: true, merged: false });
    }
    if (junk.length) {
      lanes.push({ name: '其它', head: junk[0].id, commits: junk, forkAt: null,
                   forkFrom: null, current: false, orphan: true });
    }
  }
  return lanes;
}

/* 「这一版是谁记的」：一人一条线时，时间线上混着两个人的版本，
   以前只有点开右侧详情才看得出是对方的——扫一眼根本分不清。 */
function myAuthor() {
  // 用**已经保存**的记录人，不是输入框里正在敲的那个：改名字还没点「保存」时，
  // 按输入框算会让所有旧提交当场变成「对方」，点保存后又变回来——闪一下更容易搞混
  const saved = ((S.project && S.project.author) || '').trim();
  const box = (($('#authorName') || {}).value || '').trim();
  return saved || box;
}

function isPeerAuthor(author) {
  // 大小写和首尾空格不算差别：另一台机器上存成 `JLC`、这台设成 `jlc` 是同一个人。
  // 本地化的大小写转换在这里没意义，统一转小写比一遍就够了。
  const me = myAuthor().trim().toLowerCase();
  const who = (author || '').trim().toLowerCase();
  return !!(who && me && who !== me);
}

/* 「改动 6 +6 ~0 −0」这种一半是零的写法只是噪音：只写真的发生过的。
   第一版（根提交）就只剩「+6」，改一版的写「+1 ~1 −1」。 */
function chgParts(ch) {
  const out = [];
  if (ch.added) out.push('+' + ch.added);
  if (ch.modified) out.push('~' + ch.modified);
  if (ch.deleted) out.push('−' + ch.deleted);
  return out.join(' ');
}

function commitCard(c) {
  const on = S.selected === c.id ? ' active' : '';
  const peer = isPeerAuthor(c.author);
  return `<article class="commit${on}${peer ? ' peer' : ''}" data-id="${c.id}">
    <div class="row1">
      <div class="msg" title="${esc(c.message)}">${esc((c.message || '(无说明)').split('\n')[0])}</div>
      ${peer ? `<span class="peer-chip" title="${esc(c.author)} 记的（不是你这台机器）">对方</span>` : ''}
    </div>
    <div class="row2">
      <span class="mono">${c.id.slice(0, 8)}</span>
      <span class="when">${fmtTime(c.time)}</span>
    </div>
    <div class="row3">
      ${c.changes ? `<span class="dchg" title="这个版本改了多少个文件">改动 <b>${c.changes.total}</b>
        <i>${chgParts(c.changes)}</i></span>` : ''}
      <span>${(c.stats && c.stats.files) || 0} 个文件</span>
      <span>${fmtSize(c.stats && c.stats.bytes)}</span>
      ${c.tags && c.tags.length ? '<span class="tags">' + c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join('') + '</span>' : ''}
    </div>
  </article>`;
}

/* ---------------------------------------------------------------- 全历史搜索 */
/* 时间线一次只取 S.logLimit 个版本（默认 400，老项目几千个）。搜索框以前只过滤
   这一屏：用户搜「客户确认」，屏幕里没有就一片空白，很容易以为这个版本不存在。
   现在同时去服务端翻全部历史，并说清楚「这一屏没有、全历史里有」。 */
let findTimer = 0;
let findSeq = 0;

function scheduleFind() {
  clearTimeout(findTimer);
  const q = (S.filter || '').trim();
  const box = $('#findBox');
  if (!box) return;
  if (!q || !S.path) {
    box.hidden = true;
    box.innerHTML = '';
    S.findHits = [];
    findSeq++;
    renderTimeline();
    return;
  }
  findTimer = setTimeout(() => runFind(q), 350);
}

async function runFind(q) {
  const seq = ++findSeq;
  const box = $('#findBox');
  // 上一轮的命中先作废：搜索失败时不能还留着「全历史里找到了」的旧结论
  S.findHits = [];
  box.hidden = false;
  box.innerHTML = '<div class="find-note">正在翻全部历史…</div>';
  try {
    const d = await api('/api/find', { path: S.path, q, limit: 100 });
    if (seq !== findSeq) return;
    renderFind(q, d);
  } catch (e) {
    if (seq !== findSeq) return;
    S.findHits = [];
    renderTimeline();          // 空态别再写「全历史里找到了」
    box.innerHTML = `<div class="find-note">翻不了全部历史：${esc(e.message)}</div>`;
  }
}

/* 把命中的词标出来：结果里一眼看出这一行为什么在这儿。
   先按原串算区间、再逐段转义——如果对着转义后的串替换，会切坏 &amp; 这类实体。 */
function hlText(text, q) {
  const t = String(text || '');
  const needle = String(q || '').trim().toLowerCase();
  if (!needle) return esc(t);
  const low = t.toLowerCase();
  const marks = [];
  let i = low.indexOf(needle);
  while (i >= 0) {
    marks.push([i, i + needle.length]);
    i = low.indexOf(needle, i + needle.length);
  }
  if (!marks.length) return esc(t);
  let out = '';
  let pos = 0;
  marks.forEach(([a, b]) => {
    out += esc(t.slice(pos, a)) + '<mark>' + esc(t.slice(a, b)) + '</mark>';
    pos = b;
  });
  return out + esc(t.slice(pos));
}

function renderFind(q, d) {
  const box = $('#findBox');
  const hits = d.hits || [];
  S.findHits = hits;
  renderTimeline();          // 空态文案要看全历史有没有找到
  const scanned = d.scanned || 0;
  const limited = d.scan_limited ? '（只翻了最近的一部分，更早的还没翻）' : '';
  if (!hits.length) {
    box.innerHTML = `<div class="find-note">全部 ${scanned} 个版本里都没有提到「${esc(q)}」`
      + `（说明、记录人、版本号都比过了）${limited}</div>`;
    return;
  }
  const all = S.commits || [];
  const shown = hits.slice(0, 20);
  const outside = hits.filter((h) => !all.some((c) => c.id === h.id)).length;
  box.innerHTML = `<div class="find-note">全部 ${scanned} 个版本里找到 <b>${hits.length}</b> 个${
    outside ? `，其中 <b>${outside}</b> 个不在下面这一屏（点一行就能打开）` : ''}${limited}</div>`
    + '<div class="find-list">' + shown.map((h, i) => `<div class="find-row" data-i="${i}" tabindex="0">
        <span class="fm" title="${esc(h.message)}">${h.message ? hlText(h.message, q) : '(无说明)'}</span>
        <span class="ft muted">${
          // 说明里看不到关键词，那就是靠记录人对上的：写明白，免得以为搜错了
          (h.how || '').indexOf('记录人') >= 0 && (h.how || '').indexOf('说明') < 0
            ? '<span class="fh-why">按记录人</span>' : ''}${
          hlText(h.author || '', q)} · ${fmtTime(h.time)}</span>
      </div>`).join('')
    + (hits.length > shown.length
      ? `<div class="find-more muted">还有 ${hits.length - shown.length} 个没列出来，关键词写长一点</div>` : '')
    + '</div>';
  const rows = $$('.find-row', box);
  rows.forEach((el, i) => {
    const open = () => selectCommit(hits[Number(el.dataset.i)].id);
    el.onclick = open;
    // 键盘：↑/↓ 在结果里走，回车打开，Esc 回到搜索框
    el.onkeydown = (ev) => {
      if (ev.key === 'Enter') { ev.preventDefault(); open(); }
      else if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        (rows[i + 1] || rows[0]).focus();
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (i === 0) $('#searchBox').focus(); else rows[i - 1].focus();
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        $('#searchBox').focus();
      }
    };
  });
}

/* ---------------------------------------------------------------- 只看我的 */
/* 一人一条线之后，最常见的问法是「我这边改到哪了」。这个开关只筛**卡片**，
   不影响右边的详情，也不改变任何数据——按一下就能回去看全部。
   记录人还没设就点：得说清楚为什么筛不出来，而不是默默什么都不显示。 */
function toggleMineOnly() {
  if (!S.mineOnly && !(myAuthor() || '').trim()) {
    toast('还没有设置记录人：先到「设置」里填上名字，才能按人筛', 5200);
    return;
  }
  S.mineOnly = !S.mineOnly;
  try { localStorage.setItem('zgr_mine_only', S.mineOnly ? '1' : '0'); } catch (e) { /* 隐私模式 */ }
  syncMineButton();
  renderTimeline();
}

function syncMineButton() {
  const b = $('#btnMineOnly');
  if (!b) return;
  b.classList.toggle('on', !!S.mineOnly);
  b.textContent = S.mineOnly ? '只看我的 ✓' : '只看我的';
  b.title = S.mineOnly ? '点一下显示全部版本' : '只看这台机器记的版本';
}

function renderTimeline() {
  const box = $('#timeline');
  // 跨零点：页面整夜开着不动，昨天那行「今天 17 个版本」第二天早上还挂着，
  // 点开又是另一天的数，两处对不上。日期一变就先按新的今天重算这一行
  // （今天没版本时它会把「只看今天」放开，免得把人关在空白里）。
  if (S.dayStamp !== dayKey(Date.now())) renderDayNote();
  const f = S.filter.trim().toLowerCase();
  // 「只看我的」：只看这台机器记的版本。记录人可能写成 ` JLC ` / `jlc`，
  // 所以去空格、不分大小写比——和「对方保存」那个判断同一口径
  const mine = S.mineOnly ? (myAuthor() || '').trim().toLowerCase() : '';
  const isMine = (c) => (c.author || '').trim().toLowerCase() === mine;
  // 「今天」：只留今天记的版本（本机时区）。和「只看我的」叠着用
  const todayK = S.todayOnly ? dayKey(Date.now()) : 0;
  const isToday = (c) => !!todayK && !!c.time && dayKey(c.time) === todayK;
  // 说明、版本号、记录人都算：一人一条线之后，「谁改的」是最常见的问法
  const keep = (c) => (!mine || isMine(c)) && (!todayK || isToday(c))
    && (!f || (c.message || '').toLowerCase().includes(f)
        || (c.author || '').toLowerCase().includes(f) || c.id.startsWith(f));
  const lanes = S.commits.length
    ? layoutBoard(S.commits, S.heads, S.branch, S.branchStatus) : [];
  const total = S.commits.filter(keep).length;
  // 每条分支先按筛子算一遍「还剩几条」。抬头上的分支数、已合并仓、空态都按
  // 真正看得见的算——不然筛了以后那条分支还占着位置、chip 上还写着全历史条数
  const laneRows = new Map();
  lanes.forEach((l) => laneRows.set(l, l.commits.filter(keep)));
  const shown = lanes.filter((l) => laneRows.get(l).length);
  // 已合并的分支默认收进最右边的一格，不占横向空间——合并多了也不会越用越难看
  const mergedLanes = shown.filter((l) => l.merged && !l.current && !l.orphan);
  const liveLanes = shown.filter((l) => mergedLanes.indexOf(l) < 0);
  // 开着「只看我的」时，把被藏起来的别人的版本个数说出来：不然对方刚推过来的
  // 新版看不见，很容易以为同步坏了
  const others = mine ? S.commits.filter((c) => !isMine(c)).length : 0;
  const earlier = todayK ? S.commits.filter((c) => !isToday(c)).length : 0;
  const branchNote = `${liveLanes.length} 条分支`
    + (mergedLanes.length ? ` · ${mergedLanes.length} 条已合并` : '')
    + (mine ? ` · 只看「${esc(myAuthor() || '')}」记的`
      + (others ? `（另有 ${others} 个是别人记的）` : '') : '')
    + (todayK ? ` · 只看今天`
      + (earlier ? `（另有 ${earlier} 个是更早的）` : '') : '');
  $('#timelineMeta').innerHTML = (total
    ? (S.logHasMore
      ? `最近 ${S.commits.length} 个版本（更早的还没加载）· ${branchNote}`
      : `${total} 个版本 · ${branchNote}`)
    : '') + (S.logHasMore
      ? ` <button id="btnMoreLog" class="more-log" title="再往前取 ${S.logLimit} 个版本">⬆ 加载更早的版本</button>`
      : '');
  const moreLog = $('#btnMoreLog');
  if (moreLog) moreLog.onclick = loadMoreLog;

  // 空态要说清楚「为什么空」和「点哪里能看见」：三层筛子叠在一起时，
  // 把原因归到没开的那一层上（今天有版本、只是不匹配搜索，却说「今天没你的版本」），
  // 用户会照着错误的提示一路点下去
  const whyEmpty = () => {
    if (S.findHits && S.findHits.length) return '这一屏没有，但全历史里找到了（见上方结果）';
    if (f) {
      return `没有匹配「${esc(S.filter.trim())}」的版本`
        + (todayK ? '——点「今天」看更早的，或清掉搜索' : '——清掉搜索看全部');
    }
    if (todayK && S.logHasMore) return '今天的版本还没加载到这一屏——点下面「加载更早的版本」';
    if (mine && todayK) {
      return `今天没有「${esc(myAuthor() || '')}」记的版本——点「只看我的」或「今天」放开筛子`;
    }
    if (todayK) return '今天只有这些版本——点「今天」看更早的';
    if (mine) return `这里没有「${esc(myAuthor() || '')}」记的版本——点「只看我的」看全部`;
    return '没有匹配的记录';
  };

  if (!S.commits.length) {
    box.innerHTML = `<div class="empty">${f || mine || todayK
      ? whyEmpty() : '还没有任何记录<br>点右上角「立即记录」开始'}</div>`;
    return;
  }

  const laneCol = (lane, i) => {
    const list = laneRows.get(lane) || [];
    if (!list.length) return '';
    const color = lane.orphan ? '#868e96'
      : (lane.merged ? '#c9cdd4' : LANE_COLORS[i % LANE_COLORS.length]);
    const headTag = lane.current ? '<span class="lane-badge">当前</span>'
      : (lane.merged ? `<span class="lane-badge merged">已并入 ${esc(lane.mergedInto || '')}</span>` : '');
    const tip = lane.current ? '正在这条分支上' : '点击切到这条分支';
    // 接上的那个提交如果是整条历史的根（没有父），说明大家是同一个起点，
    // 不能说成「从某条分支分出」——那样切一下分支就会显得历史被改了。
    const sharedRoot = lane.forkCommit && !(lane.forkCommit.parents || []).length;
    const short = esc((lane.forkAt || '').slice(0, 8));
    const fork = lane.forkFrom
      ? `<button class="lane-fork" data-fork="${esc(lane.forkAt || '')}" data-from="${esc(lane.forkFrom)}"
           title="点一下跳到这个接点">
           <span class="fk-line"></span>${sharedRoot ? `共同起点 · ${short}` : `由 ${esc(lane.forkFrom)} 分出 · ${short}`}
         </button>`
      : (lane.orphan ? '' : '<div class="lane-root"><span class="fk-line"></span>这条分支的起点</div>');
    const ops = lane.current || lane.orphan ? '' : `<div class="lane-ops">
        ${lane.merged ? '' : `<button class="lane-merge" title="把这条分支整个并进 ${esc(S.branch || '当前分支')}（生成一个合并版本，历史都留着）">并入 ${esc(S.branch || '当前')}</button>`}
        <button class="lane-del" title="删掉这条分支（提交不会被删，随时能按提交号再开出来）">删除</button>
      </div>`;
    return `<section class="lane${lane.current ? ' current' : ''}${lane.orphan ? ' orphan' : ''}${lane.merged ? ' merged' : ''}"
             data-branch="${esc(lane.name)}" style="--lane:${color}">
      <header class="lane-head" title="${tip}">
        <span class="lane-dot"></span>
        <span class="lane-name">${esc(lane.name)}</span>
        ${headTag}
        <span class="lane-count">${list.length}</span>
      </header>
      ${ops}
      <div class="lane-body">${list.map(commitCard).join('')}</div>
      ${fork}
    </section>`;
  };

  const cols = liveLanes.map(laneCol).filter(Boolean).join('');
  /* 已合并的分支默认只留一格小标签；点「展开」才铺成完整列（灰色、只留删除）。
     这样合并几次之后横向不会越来越长，需要看旧分支的版本时也还找得到。 */
  const showMerged = !!S.showMerged && mergedLanes.length;
  const mergedCols = showMerged
    ? mergedLanes.map((l, i) => laneCol(l, liveLanes.length + i)).filter(Boolean).join('')
      + `<div class="bin-toggle-wrap"><button class="bin-toggle">收起已合并</button></div>`
    : '';

  const bin = (!showMerged && mergedLanes.length) ? `<section class="lane merged-bin">
    <header class="lane-head" title="已经并进别的分支，收在这里不占地方">
      <span class="lane-dot"></span><span class="lane-name">已合并</span>
      <span class="lane-count">${mergedLanes.length}</span>
      <button class="bin-expand" title="把这些分支展开成完整的列">展开</button>
    </header>
    <div class="lane-body">${mergedLanes.map((l) => `
      <div class="mchip" data-branch="${esc(l.name)}"
           title="已并入 ${esc(l.mergedInto || '')} · 点一下切过去看看">
        <span class="mchip-name">${esc(l.name)}</span>
        <span class="mchip-n" title="${(laneRows.get(l) || []).length} 个版本">${(laneRows.get(l) || []).length}</span>
        <button class="mchip-del" title="删掉这条分支（提交不会丢）">×</button>
      </div>`).join('')}</div>
    <div class="lane-root">已并进别的分支</div>
  </section>` : '';

  box.innerHTML = `<div class="board">${cols}${mergedCols}${bin}${
    cols || mergedCols || bin ? '' : `<div class="empty">${whyEmpty()}</div>`}</div>`;

  $$('.commit', box).forEach((el) => { el.onclick = () => selectCommit(el.dataset.id); });
  $$('.lane-head', box).forEach((el) => {
    const sec = el.closest('.lane');
    const name = sec && sec.dataset.branch;
    el.onclick = () => {
      if (sec.classList.contains('current')) return;
      switchBranch(name);
    };
  });
  $$('.lane-fork', box).forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const id = el.dataset.fork;
      const target = id && box.querySelector(`.commit[data-id="${id}"]`);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
        target.classList.add('flash');
        setTimeout(() => target.classList.remove('flash'), 1400);
      }
    };
  });
  $$('.lane-merge', box).forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      mergeBranch(el.closest('.lane').dataset.branch);
    };
  });
  $$('.lane-del', box).forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      deleteBranch(el.closest('.lane').dataset.branch);
    };
  });
  $$('.mchip', box).forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('.mchip-del')) return;
      switchBranch(el.dataset.branch);
    };
  });
  $$('.mchip-del', box).forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      deleteBranch(el.closest('.mchip').dataset.branch);
    };
  });
  const expand = $('.bin-expand', box);
  if (expand) expand.onclick = (ev) => { ev.stopPropagation(); S.showMerged = true; renderTimeline(); };
  const collapse = $('.bin-toggle', box);
  if (collapse) collapse.onclick = () => { S.showMerged = false; renderTimeline(); };
  // 当前分支的列滚到可见范围（分支多了以后不至于一打开就看不见自己）
  const cur = box.querySelector('.lane.current .lane-head');
  if (cur && box.scrollWidth > box.clientWidth) {
    cur.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
}

/* 点分支标题切过去：不新建、不改写任何分支；文件跟着换成那条分支的内容。
   以前只动 HEAD 不动工作区，切完看着是「已经过去了」，其实磁盘上还是老内容，
   下一次自动记录就会在对方的分支上记一版「改回去」——所以这里先问清楚。 */
async function switchBranch(name) {
  if (!S.path || !name) return;
  try {
    let d = await post('/api/branch', { path: S.path, branch: name });
    if (d.dry) {
      const p = d.plan || {};
      const lines = [`切到分支「${name}」会把工作区更新成这条分支的最新版本：`,
        '',
        `  覆盖 ${p.overwrite || 0} 个文件`,
        `  新增 ${p.create || 0} 个`,
        `  删除 ${p.remove || 0} 个`,
        '',
        '被换掉的文件会先备份到回收站（能找回）。'];
      if (!d.clean) {
        lines.push('', `注意：现在还有 ${d.dirty || 0} 个文件没记录。`
          + '切过去以后它们只会留在备份里，不再算在本机当前分支上。');
      }
      lines.push('', '继续切过去吗？');
      if (!confirm(lines.join('\n'))) return;
      d = await post('/api/branch', { path: S.path, branch: name, force: true });
    }
    toast(`✓ 已切到分支 ${d.branch}`, 2500);
    S.selected = null;
    S.branch = d.branch;
    await loadTimeline();
    if (typeof loadProject === 'function') loadProject();
  } catch (e) {
    toast(e.message || String(e), 5000);
  }
}

/* 合并确认框里的「撞车」区。
   「有 N 个冲突」这句话没用：用户接着问的永远是「哪个零件、他什么时候改的」。
   这里把两边**各自最近一次**改这个文件的人和时间摆出来，打个电话就能对清楚。 */
function clashBlock(p, conf) {
  const detail = p.clash_detail || [];
  const n = (p.clashes || []).length;
  if (!n) return '<p class="small" style="color:#0f7b2f">没有撞车，可以直接合并。</p>';
  const line = (x) => (x
    ? `${esc(x.author || '（没写名字）')} · ${esc(fmtTime(x.time))}`
    : '没有改动记录');
  const rows = detail.slice(0, 8).map((d) => `
    <div class="clash-row">
      <div class="clash-path">${esc(d.path)}</div>
      <div class="muted small">本机：${line(d.ours)}</div>
      <div class="muted small">对方：${line(d.theirs)}</div>
    </div>`).join('');
  const head = conf
    ? `有 ${n} 个文件两边都动过（其中 ${conf} 个内容冲突）：按<b>本机优先</b>合并，
       对方的原文仍在历史里，随时能回溯。`
    : `有 ${n} 个文件两边都动过（删除 / 改动撞在一起），按下面这套规则合并。`;
  return `<p class="small" style="color:#b42318">${head}</p>
    <div class="merge-plan-files">${rows || '<div class="muted small">（文件太多，这里只列出数量）</div>'}</div>
    ${n > 8 ? `<div class="muted small">还有 ${n - 8} 个…</div>` : ''}`;
}

/* 把一条分支整个并入当前分支。
   git 的口径：生成一个双父提交（两个头都是它的父），所以两条分支的历史一条都不丢；
   并完之后那条分支就"已经包含在"当前分支里了，可以放心删掉，跟 GitHub 合并 PR 之后
   「Delete branch」是同一件事。 */
async function mergeBranch(name) {
  if (!S.path || !name) return;
  let p;
  try {
    p = await post('/api/branch/merge', { path: S.path, branch: name });
  } catch (e) {
    toast(e.message || String(e), 5500);
    return;
  }
  const target = S.branch || '当前分支';
  const c = p.counts || {};
  const conf = (p.conflicts || []).length;
  const take = (c.theirs || 0) + (c.theirs_only || 0);
  const keep = (c.ours || 0) + (c.theirs_deleted || 0);
  const m = openModal({
    title: `把 ${name} 并入 ${S.branch || '当前分支'}`,
    width: 'min(640px,100%)',
    body: `
      <p class="small">会并进 <b>${p.versions}</b> 个版本，生成一个「合并版本」——
        两条分支的版本都还在时间线上，合并本身也能撤销。</p>
      <div class="merge-stats">
        <span>采用分支的改动 <b>${take}</b></span>
        <span>保留本机 <b>${keep}</b></span>
        <span>两边一样 <b>${c.same || 0}</b></span>
        <span class="${conf ? 'bad' : 'good'}">冲突 <b>${conf}</b></span>
      </div>
      ${clashBlock(p, conf)}
      <p class="muted small">合并会把工作区更新成合并结果（覆盖前自动备份到 <code>.zgr/backup</code>）。</p>`,
    footer: '<button class="go primary">合并</button><button class="cancel">取消</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.go', m.foot).onclick = async () => {
    $('.go', m.foot).disabled = true;
    try {
      const r = await post('/api/branch/merge', {
        path: S.path, branch: name, apply: true, strategy: 'smart',
        // 两边的头都带回核对值：核对完到点确认之间，本机可能又记了一版，
        // 对方也可能往那条分支上推了一版——哪一种都该让人重新看一遍
        expect: p.ours || '', expect_theirs: p.theirs || '',
      });
      m.close();
      toast(`✓ 已把 ${name} 并进 ${target}，合并版本 ${r.commit.slice(0, 10)}`, 4500);
      S.selected = null;
      await loadTimeline();
      pollStatus();          // 刷新头顶那个版本号/未记录标记（loadProject 从来没定义过）
      selectCommit(r.commit);
      askDeleteMerged(name);
    } catch (e) {
      $('.go', m.foot).disabled = false;
      toast('合并失败：' + (e.message || e), 6000);
    }
  };
}

/* 合并完问一句「删不删」——GitHub 合并 PR 之后也是这个流程 */
function askDeleteMerged(name) {
  const m = openModal({
    title: '合并完成',
    width: 'min(520px,100%)',
    body: `<p class="small">分支 <b>${esc(name)}</b> 已经整个并进 <b>${esc(S.branch || '当前分支')}</b>，
      它的版本都在当前分支的历史里了。</p>
      <p class="muted small">删掉它只是去掉这个名字（跟 git branch -d 一样），一个提交都不会丢；
      以后想找还能从合并版本那里回溯到。</p>`,
    footer: `<button class="go danger">删除分支</button>
      <button class="cancel">保留</button>`,
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.go', m.foot).onclick = async () => {
    m.close();
    try {
      await post('/api/branch/delete', { path: S.path, branch: name });
      toast(`✓ 已删除分支 ${name}`, 3000);
      await loadTimeline();
    } catch (e) { toast(e.message || String(e), 5000); }
  };
}

async function deleteBranch(name) {
  if (!S.path || !name) return;
  const st = (S.branchStatus && S.branchStatus[name]) || {};
  const into = st.merged_into || st.same_as || '';
  const m = openModal({
    title: `删除分支 ${name}`,
    width: 'min(560px,100%)',
    body: into
      ? `<p class="small">它已经整个并进 <b>${esc(into)}</b>，删掉不会丢任何版本。</p>
         <p class="muted small">只删掉「${esc(name)}」这个名字；提交、文件、历史都留在库里，
         需要时还能按提交号重新开一条分支。</p>`
      : `<p class="small" style="color:#b42318"><b>这条分支还有没合并的版本。</b>
         删掉之后这些版本就不在任何分支上了（历史对象还在，但时间线上只能从「其它」里翻到）。</p>
         <p class="muted small">建议先「并入当前分支」，再删。</p>
         <label class="small" style="display:block;margin-top:10px">确实要删，请输入分支名
           <b>${esc(name)}</b>：</label>
         <input class="confirm-name" type="text" autocomplete="off" spellcheck="false"
           placeholder="${esc(name)}">`,
    footer: `<button class="go ${into ? 'primary' : 'danger'}" ${into ? '' : 'disabled'}>${into ? '删除' : '仍然删除'}</button>
      <button class="cancel">取消</button>`,
  });
  $('.cancel', m.foot).onclick = m.close;
  const nameIn = $('.confirm-name', m.body);
  if (nameIn) {
    // 防呆：删掉未合并的版本是不能撤销的，必须手打一遍分支名才能按下去
    nameIn.addEventListener('input', () => {
      $('.go', m.foot).disabled = nameIn.value.trim() !== name;
    });
    nameIn.focus();
  }
  $('.go', m.foot).onclick = async () => {
    m.close();
    try {
      const r = await post('/api/branch/delete', {
        path: S.path, branch: name, force: !into,
      });
      toast(r.merged_into ? `✓ 已删除分支 ${name}（已并入 ${r.merged_into}）`
        : `✓ 已删除分支 ${name}`, 3500);
      if (S.branch === name) S.branch = '';
      await loadTimeline();
      pollStatus();          // 刷新头顶那个版本号/未记录标记（loadProject 从来没定义过）
    } catch (e) { toast(e.message || String(e), 5500); }
  };
}

async function selectCommit(id) {
  S.selected = id;
  renderTimeline();
  const detail = $('#detail');
  detail.innerHTML = '<div class="detail-empty">加载中…</div>';
  try {
    const d = await api('/api/commit', { path: S.path, id });
    S.selectedData = d;
    renderDetail();
  } catch (e) {
    detail.innerHTML = `<div class="detail-empty">${esc(e.message)}</div>`;
  }
}

/* 「最新一版」是哪一个：优先当前分支的头，头不在这一屏里就取时间最新的那条。
   时间线是按分叉画的多条线，直接拿数组第一个不一定对。 */
function newestCommitId() {
  if (!S.commits || !S.commits.length) return null;
  const head = S.project && S.project.head;
  if (head && S.commits.some((c) => c.id === head)) return head;
  let best = null;
  S.commits.forEach((c) => {
    if (!best || (c.time || 0) > (best.time || 0)) best = c;
  });
  return best ? best.id : null;
}

/* 详情面板中间那根分隔条：拖动改上下比例，双击复位，按人记住。
   以前上半段（说明/按钮/筛选）有多高就多高，按钮一多零件清单只剩一条缝。 */
function initDetailSplitter() {
  const inner = document.querySelector('.detail-inner');
  const top = document.querySelector('.detail-top');
  const sp = document.querySelector('.detail-splitter');
  if (!inner || !top || !sp) return;
  const KEY = 'zgr.detailTopPx';
  const clamp = (h) => {
    const maxH = Math.max(140, inner.clientHeight - 150);
    return Math.max(96, Math.min(Math.round(h), maxH));
  };
  const apply = (h) => {
    const v = clamp(h);
    top.style.maxHeight = v + 'px';
    return v;
  };
  const saved = parseInt(localStorage.getItem(KEY) || '0', 10);
  if (saved > 0) apply(saved);
  const remember = () => {
    try { localStorage.setItem(KEY, String(Math.round(top.getBoundingClientRect().height))); } catch (e) {}
  };
  sp.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    sp.setPointerCapture(e.pointerId);
    sp.classList.add('dragging');
    const startY = e.clientY;
    const startH = top.getBoundingClientRect().height;
    const move = (ev) => apply(startH + (ev.clientY - startY));
    const up = () => {
      sp.classList.remove('dragging');
      sp.removeEventListener('pointermove', move);
      sp.removeEventListener('pointerup', up);
      sp.removeEventListener('pointercancel', up);
      remember();
    };
    sp.addEventListener('pointermove', move);
    sp.addEventListener('pointerup', up);
    sp.addEventListener('pointercancel', up);
  });
  sp.addEventListener('dblclick', () => {
    top.style.maxHeight = '';
    try { localStorage.removeItem(KEY); } catch (e) {}
  });
}
window.addEventListener('resize', () => {
  const top = document.querySelector('.detail-top');
  if (top && top.style.maxHeight) {
    const inner = document.querySelector('.detail-inner');
    if (inner) {
      const maxH = Math.max(140, inner.clientHeight - 150);
      top.style.maxHeight = Math.max(96, Math.min(parseInt(top.style.maxHeight, 10) || maxH, maxH)) + 'px';
    }
  }
});

function renderDetail() {
  const detail = $('#detail');
  const d = S.selectedData;
  if (!d || !S.selected) {
    // 一条版本都没有的新项目：别让人对着「请选择一个版本」发懵
    const none = !(S.commits && S.commits.length);
    detail.innerHTML = none
      ? '<div class="detail-empty">还没有任何版本。<br>文件改完后点右上角「立即记录」，这里就会显示这一版动了什么。</div>'
      : '<div class="detail-empty">从左侧时间线选择一个版本查看详情</div>';
    return;
  }
  const c = d.commit;
  const mark = (d.mark && d.mark.mark) || '';
  const tags = ((d.mark && d.mark.tags) || []).join(', ');
  const ch = d.changes;
  const rows = fileRowsOf(d);
  const isMerge = (c.parents || []).length > 1;
  const fromPeer = isPeerAuthor(c.author);

  detail.innerHTML = `<div class="detail-inner">
    <div class="detail-top">
    <h2>${esc((c.message || '(无说明)').split('\n')[0])}
      ${fromPeer ? '<span class="pill peer-pill" title="这个版本是对方保存的，不是本机记录的">对方保存</span>' : ''}</h2>
    <div class="meta-grid">
      <div class="k">版本</div><div class="mono">${c.id.slice(0, 16)}</div>
      <div class="k">时间</div><div>${fmtTime(c.time)}</div>
      <div class="k">记录人</div><div>${esc(c.author || '')}</div>
      <div class="k">规模</div><div>${(c.stats && c.stats.files) || 0} 个文件 · ${fmtSize(c.stats && c.stats.bytes)}</div>
      <div class="k">上一版</div><div class="mono">${d.parent ? d.parent.slice(0, 16) : '首个版本'}</div>
    </div>
    <div class="actions">
      ${d.parent ? '<button class="act-diff">对比上一版</button>' : ''}
      ${isMerge ? '<button class="act-undo">撤销这次合并</button>' : ''}
      <button class="act-rollback" title="生成一个内容等于此版本的新版本，历史全部保留">回溯到此版本</button>
      <button class="act-mergefrom" title="把这个历史节点合并进当前版本">与这一版合并</button>
      <button class="act-restore" title="把整个项目目录恢复成这一版；多出来的文件会先备份再移除">恢复整个项目到此版本</button>
      <button class="act-csv" title="把这次改动导出成 CSV：哪个文件、改成什么状态、大小变了多少（评审/回复客户用）">导出改动清单</button>
      <button class="act-pack" title="打成 zip，直接发给别人">打包发送</button>
      <button class="act-export">导出到文件夹</button>
      <button class="act-copy">复制版本号</button>
    </div>
    <div class="section-title">标记</div>
    <div class="marks">
      <button class="mk red ${mark === 'red' ? 'on' : ''}" data-m="red" title="红"></button>
      <button class="mk yellow ${mark === 'yellow' ? 'on' : ''}" data-m="yellow" title="黄"></button>
      <button class="mk green ${mark === 'green' ? 'on' : ''}" data-m="green" title="绿"></button>
      <button class="mk none ${!mark ? 'on' : ''}" data-m="none" title="清除"></button>
      <input class="tag-input" placeholder="标签，逗号分隔" value="${esc(tags)}" style="max-width:150px">
      <button class="act-tags" style="padding:5px 9px;font-size:12px">保存</button>
    </div>
    ${ch ? `<div class="chg-summary">${d.parent ? '本次改动：' : '首个版本：'}
      ${[ch.added.length ? `<span class="sa">新增 ${ch.added.length}</span>` : '',
         ch.modified.length ? `<span class="sm">修改 ${ch.modified.length}</span>` : '',
         ch.deleted.length ? `<span class="sd">删除 ${ch.deleted.length}</span>` : ''
        ].filter(Boolean).join(' · ')}
      <span class="muted small">（清单里 <b class="red">红</b> = 这个版本改过，<b class="green">绿</b> = 没改）</span>
    </div>` : ''}
    <div class="section-title file-head">
      <span>这个版本的文件（${rows.length}）</span>
      <span class="file-tools">
        <span class="chip ${S.fileView === 'all' ? 'on' : ''}" data-v="all">全部</span>
        <span class="chip ${S.fileView === 'changed' ? 'on' : ''}" data-v="changed">只看改动</span>
        <span class="chip ${S.fileView === 'model' ? 'on' : ''}" data-v="model">只看模型</span>
        <input class="file-find" placeholder="按文件名过滤" value="${esc(S.fileQuery)}">
      </span>
    </div>
    <div class="file-bar">
      <select class="file-ext" title="按文件类型筛选">
        <option value="">全部类型</option>
        ${extOptions(rows).map((x) => `<option value="${esc(x)}"${
          S.fileExt === x ? ' selected' : ''}>${esc(x || '无扩展名')}（${
          rows.filter((r) => extOf(r.path).toUpperCase() === x.toUpperCase()).length}）</option>`).join('')}
      </select>
      <select class="file-sort" title="排序方式">
        <option value="default"${S.fileSort === 'default' ? ' selected' : ''}>默认顺序</option>
        <option value="name"${S.fileSort === 'name' ? ' selected' : ''}>按文件名</option>
        <option value="mtime"${S.fileSort === 'mtime' ? ' selected' : ''}>按修改日期</option>
        <option value="ctime"${S.fileSort === 'ctime' ? ' selected' : ''}>按创建日期</option>
        <option value="size"${S.fileSort === 'size' ? ' selected' : ''}>按大小</option>
        <option value="status"${S.fileSort === 'status' ? ' selected' : ''}>按改动类型</option>
      </select>
      <button class="sort-dir" title="${S.sortDir > 0 ? '当前升序，点一下改降序' : '当前降序，点一下改升序'}">${S.sortDir > 0 ? '↑' : '↓'}</button>
      <span class="bar-grow"></span>
      <span class="selinfo muted small"></span>
      <button class="sel-all" title="把当前筛选出来的文件全勾上">全选</button>
      <button class="btn-step" disabled title="把勾选的 SolidWorks 文件导出成 STEP">⇩ 导出 STEP</button>
    </div>
    </div>
    <div class="detail-splitter" title="上下拖动，调整下面零件清单的高度（双击复位）"></div>
    <div class="filerows"></div>
  </div>`;
  initDetailSplitter();

  $$('.mk', detail).forEach((el) => {
    el.onclick = async () => {
      try {
        await post('/api/mark', { path: S.path, commit: c.id, mark: el.dataset.m });
        toast('标记已更新');
        await loadTimeline();
        await selectCommit(c.id);
      } catch (e) { toast(e.message, 3500); }
    };
  });
  $('.act-tags', detail).onclick = async () => {
    const v = $('.tag-input', detail).value;
    try {
      await post('/api/mark', { path: S.path, commit: c.id, tags: v.split(/[,，]/).map((x) => x.trim()).filter(Boolean) });
      toast('标签已保存');
      await loadTimeline();
      await selectCommit(c.id);
    } catch (e) { toast(e.message, 3500); }
  };
  const btn = $('.act-copy', detail);
  if (btn) btn.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(c.id); toast('版本号已复制'); };
  const bd = $('.act-diff', detail);
  if (bd) bd.onclick = () => showDiff(d.parent, c.id);
  const bu = $('.act-undo', detail);
  if (bu) bu.onclick = () => confirmUndo(c, (c.parents || [])[0]);
  $('.act-rollback', detail).onclick = () => rollbackTo(c);
  if ($('.act-mergefrom', detail)) $('.act-mergefrom', detail).onclick = () => mergeFrom(c);
  $('.act-restore', detail).onclick = () => confirmRestore(c);
  $('.act-csv', detail).onclick = () => saveChangeCsv(c, d);
  $('.act-pack', detail).onclick = () => packVersion(c.id);
  $('.act-export', detail).onclick = () => doExport(c);
  wireFileList(d, rows, detail, c);
}

/* ---------- 版本文件清单：红=改过，绿=没改 ---------- */

function baseName(p) {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i < 0 ? p : p.slice(i + 1);
}

function dirName(p) {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  return i < 0 ? '' : p.slice(0, i);
}

function fileRowsOf(d) {
  const ch = d.changes;
  const marks = new Map();
  if (ch) {
    (ch.added || []).forEach((x) => marks.set(x.path, {
      st: 'A', oldOid: '', oldSize: null, oid: x.oid, size: x.size,
    }));
    (ch.modified || []).forEach((x) => marks.set(x.path, {
      st: 'M', oldOid: x.old_oid, oldSize: x.old_size, oid: x.new_oid, size: x.new_size,
    }));
    (ch.deleted || []).forEach((x) => marks.set(x.path, {
      st: 'D', oldOid: x.oid, oldSize: x.size, oid: '', size: 0,
    }));
  }
  const rows = [];
  const files = d.files || {};
  for (const [p, v] of Object.entries(files)) {
    const m = marks.get(p);
    rows.push(m
      ? { path: p, ...m }
      : { path: p, st: '=', oid: v.oid, size: v.size, oldOid: '', oldSize: null });
  }
  for (const [p, m] of marks) {
    if (!(p in files)) rows.push({ path: p, ...m });
  }
  const order = { A: 0, M: 1, D: 2, '=': 3 };
  rows.sort((a, b) => (order[a.st] - order[b.st]) || a.path.localeCompare(b.path, 'zh'));
  return rows;
}

function fileRowHtml(r) {
  const changed = r.st !== '=';
  const delta = r.st === 'M' ? (r.size - (r.oldSize || 0))
    : (r.st === 'A' ? r.size : (r.st === 'D' ? -(r.oldSize || 0) : 0));
  let sizeHtml;
  if (r.st === 'M') {
    sizeHtml = `${fmtSize(r.oldSize)} → <b>${fmtSize(r.size)}</b>
      <em class="${delta >= 0 ? 'up' : 'down'}">${delta >= 0 ? '+' : '−'}${fmtSize(Math.abs(delta))}</em>`;
  } else if (r.st === 'A') {
    sizeHtml = `<b>${fmtSize(r.size)}</b>`;
  } else if (r.st === 'D') {
    sizeHtml = `原 ${fmtSize(r.oldSize)}`;
  } else {
    sizeHtml = fmtSize(r.size);
  }
  const badge = { A: '新增', M: '修改', D: '删除' }[r.st] || '';
  const thumbOid = r.st === 'D' ? r.oldOid : r.oid;
  const thumb = canThumb(r.path)
    ? `<span class="fthumb">${lazyThumb(thumbUrl(thumbOid, r.path, 128), '', r.path)}
         <i class="fext">${esc(extOf(r.path))}</i></span>`
    : `<span class="fthumb none"><i class="fext">${esc(extOf(r.path))}</i></span>`;
  const isSW = /\.(sldprt|sldasm|slddrw)$/i.test(r.path || '');
  const isAsm = /\.sldasm$/i.test(r.path || '');
  return `<div class="frow ${changed ? 'is-chg' : 'is-ok'}" data-st="${r.st}"
       data-path="${esc(r.path)}" data-oid="${esc(r.oid || '')}" data-old="${esc(r.oldOid || '')}">
    ${r.st !== 'D' ? `<label class="fcheck" title="勾选后可以批量导出 STEP">
      <input type="checkbox" class="fsel"${S.sel.has(r.path) ? ' checked' : ''}></label>` : ''}
    <span class="fdot" title="${changed ? '这个版本改过' : '没改'}"></span>
    ${thumb}
    <span class="fbody">
      <span class="fline1">
        <b class="fn" title="${esc(r.path)}">${esc(baseName(r.path))}</b>
        ${badge ? `<span class="fbadge ${r.st}">${badge}</span>` : ''}
      </span>
      <span class="fline2">
        <i class="fdir" title="${esc(r.path)}">${esc(dirName(r.path) || '·')}</i>
        <span class="fsize">${sizeHtml}</span>
      </span>
    </span>
    <span class="fops">
      ${r.st !== 'D' ? '<span class="floc" title="在资源管理器里定位这个文件">定位</span>'
        + '<span class="fopen" title="用本机默认程序打开（SolidWorks / DWG 查看器等），双击本行也可以">打开</span>'
        + '<span class="fbr" title="只改这一个零件：从当前版本开一条新分支，切过去并直接打开它">⎇ 单独改</span>'
        + (isSW ? '<span class="fstep" title="把这个零件导出成 STEP（交给别人、进 CAM）">STEP</span>' : '')
        + (isAsm ? '<span class="fbom" title="从这个装配体生成物料清单：Excel + CSV，数量已按层级累乘">BOM</span>' : '') : ''}
      <span class="fhist" title="这个零件的改动历史（哪个版本改过、改了多少）">史</span>
    </span>
    ${r.oid ? `<a class="fdl" title="下载这个版本的文件"
        href="${withToken('/api/file?' + new URLSearchParams({ path: S.path || '', oid: r.oid }).toString())}"
        download>↧</a>` : ''}
  </div>`;
}

/* 单个零件的改动历史：像追光几何那样看“这个零件什么时候被谁改过、改了多少” */
async function showFileHistory(path) {
  let d;
  try {
    d = await api('/api/filelog', { path: S.path, file: path });
  } catch (e) { return toast(e.message, 3500); }
  const rows = d.entries || [];
  const badge = { A: '新增', M: '修改', D: '删除' };
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="muted small" style="margin-bottom:10px">
      共 ${rows.length} 次改动${d.work && d.work.exists
        ? (d.work.changed ? ' · 工作区有未记录的改动' : ' · 工作区与最新版本一致')
        : ' · 工作区里已经没有这个文件'}
    </div>
    <div class="filerows hist">${rows.length ? rows.map((e, i) => {
      const delta = e.st === 'M' ? e.size - e.old_size : (e.st === 'A' ? e.size : -e.old_size);
      const sizeTxt = e.st === 'M'
        ? `${fmtSize(e.old_size)} → <b>${fmtSize(e.size)}</b> <em class="${delta >= 0 ? 'up' : 'down'}">${delta >= 0 ? '+' : '−'}${fmtSize(Math.abs(delta))}</em>`
        : (e.st === 'A' ? `<b>${fmtSize(e.size)}</b>` : `原 ${fmtSize(e.old_size)}`);
      const thumbOid = e.st === 'D' ? e.old_oid : e.oid;
      return `<div class="frow is-chg" data-i="${i}">
        <span class="fthumb">${canThumb(path) ? lazyThumb(thumbUrl(thumbOid, path, 128), '', path) : ''}
          <i class="fext">${esc(extOf(path))}</i></span>
        <span class="fbody">
          <span class="fline1"><b class="fn">${esc(e.message || '(无说明)')}</b>
            <span class="fbadge ${e.st}">${badge[e.st] || ''}</span></span>
          <span class="fline2"><i class="fdir">${fmtTime(e.time)} · ${esc(e.commit.slice(0, 10))}</i>
            <span class="fsize">${sizeTxt}</span>
            <button class="hist-restore" data-i="${i}"
              title="只把这一个文件写回这一版，其它文件不动（现有文件先备份）">↩ 恢复这一版</button></span>
        </span>
      </div>`;
    }).join('') : '<div class="muted small" style="padding:10px">还没有改动记录</div>'}</div>`;
  const m = openModal({
    title: baseName(path) + ' · 改动历史', body, width: 'min(780px,100%)',
    footer: '<button class="cancel">关闭</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  wireLazyThumbs(body);
  $$('.frow', body).forEach((el) => {
    const e = rows[Number(el.dataset.i)];
    if (!e) return;
    el.onclick = () => showModelCompare(path, e.old_oid, e.oid, e.commit);
  });
  // 只把这一个文件退回某一版：删掉的零件也靠它找回来（以前只能整个项目一起恢复）
  $$('.hist-restore', body).forEach((btn) => {
    const e = rows[Number(btn.dataset.i)];
    if (!e) return;
    const target = e.st === 'D' ? e.old_oid : e.oid;
    if (!target) { btn.remove(); return; }
    btn.onclick = async (ev) => {
      ev.stopPropagation();               // 别顺带打开 3D 对比
      const when = fmtTime(e.time);
      const what = (e.message || '').split('\n')[0];
      if (!confirm(`把这一个文件恢复成「${when}」那一版？\n\n${what}\n\n`
        + '工作区里现在的这个文件会先备份到 .zgr/backup，其它文件一个都不动。'
        + '恢复之后按需要再点「立即记录」存成新版本。')) return;
      btn.disabled = true;
      btn.textContent = '恢复中…';
      try {
        const r = await post('/api/restore_file', {
          path: S.path, file: path, oid: target,
        });
        toast(`✓ ${path} 已恢复成 ${when} 那一版${r.backup ? '（原文件已备份）' : ''}`, 6000);
        m.close();
        await selectProject(S.path, true);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = '↩ 恢复这一版';
        toast('恢复失败：' + (err.message || err), 7000);
      }
    };
  });
}

const isSWPath = (p) => /\.(sldprt|sldasm|slddrw)$/i.test(p || '');

/* 文件清单里出现过的类型，按出现次数从多到少 */
function extOptions(rows) {
  const m = new Map();
  rows.forEach((r) => {
    if (!/\.[^./\\]+$/.test(r.path || '')) return;
    const e = extOf(r.path);
    m.set(e, (m.get(e) || 0) + 1);
  });
  return Array.from(m.keys()).sort((a, b) => (m.get(b) - m.get(a)) || a.localeCompare(b));
}

function wireFileList(d, rows, detail, c) {
  const box = $('.filerows', detail);
  if (!box) return;
  // 换了版本以后，之前勾的路径可能已经不存在了
  const alive = new Set(rows.map((r) => r.path));
  Array.from(S.sel).forEach((p) => { if (!alive.has(p)) S.sel.delete(p); });

  const bar = $('.file-bar', detail);
  const selInfo = $('.selinfo', detail);
  const btnStep = $('.btn-step', detail);
  let paintSeq = 0;
  let fetchingTimes = false;

  const passes = (r) => {
    const q = (S.fileQuery || '').trim().toLowerCase();
    if (S.fileView === 'changed' && r.st === '=') return false;
    if (S.fileView === 'model' && !isModel(r.path)) return false;
    if (S.fileExt && extOf(r.path).toUpperCase() !== S.fileExt.toUpperCase()) return false;
    if (q && !r.path.toLowerCase().includes(q)) return false;
    return true;
  };
  const visible = () => rows.filter(passes);

  const sortKey = (r) => {
    const t = S.times[c.id] || {};
    switch (S.fileSort) {
      case 'name': return r.path;
      case 'size': return r.size || 0;
      case 'mtime': return (t[r.path] && t[r.path].m) || 0;
      case 'ctime': return (t[r.path] && t[r.path].c) || 0;
      case 'status': return { A: 0, M: 1, D: 2, '=': 3 }[r.st];
      default: return 0;
    }
  };
  const sorter = (list) => {
    if (S.fileSort === 'default' && S.sortDir > 0) return list;
    const arr = list.slice();
    arr.sort((a, b) => {
      const ka = sortKey(a);
      const kb = sortKey(b);
      let d;
      if (typeof ka === 'string' || typeof kb === 'string') {
        d = String(ka).localeCompare(String(kb), 'zh');
      } else {
        d = ka - kb;
      }
      return (d || a.path.localeCompare(b.path, 'zh')) * (S.sortDir > 0 ? 1 : -1);
    });
    return arr;
  };

  /* 按日期排序要知道工作区里每个文件的修改/创建时间：用到那一刻才问服务端，
     取过一次就记下来（同一个版本不用重复取） */
  const needTimes = () => {
    if (S.fileSort !== 'mtime' && S.fileSort !== 'ctime') return Promise.resolve();
    if (S.times[c.id] || fetchingTimes) return Promise.resolve();
    fetchingTimes = true;
    return post('/api/filetimes', {
      path: S.path, files: rows.filter((r) => r.st !== 'D').map((r) => r.path),
    }).then((r) => { S.times[c.id] = r.times || {}; })
      .catch((e) => toast('取文件时间失败：' + e.message, 4000))
      .finally(() => { fetchingTimes = false; });
  };

  const selSW = () => Array.from(S.sel).filter(isSWPath);
  const targets = () => (S.sel.size ? selSW() : visible().filter(
    (r) => r.st !== 'D' && isSWPath(r.path)).map((r) => r.path));

  const refreshSel = () => {
    if (!btnStep) return;
    const n = targets().length;
    const picked = S.sel.size;
    selInfo.textContent = picked
      ? `已选 ${picked} 个${n < picked ? `（其中 ${n} 个能导 STEP）` : ''}`
      : '';
    btnStep.disabled = n === 0;
    btnStep.textContent = picked
      ? (n ? `⇩ 导出选中的 ${n} 个` : '选中的里没有 SolidWorks 文件')
      : (n ? `⇩ 导出这 ${n} 个` : '⇩ 导出 STEP');
  };

  const paint = async () => {
    const seq = ++paintSeq;
    await needTimes();
    if (seq !== paintSeq) return;
    const list = sorter(visible());
    refreshSel();
    if (!list.length) {
      const filtered = rows.length && (S.fileQuery || S.fileExt || S.fileView !== 'all');
      box.innerHTML = `<div class="muted small" style="padding:8px">${
        !rows.length ? '这个版本没有文件'
          : filtered ? '没有符合当前筛选条件的文件' : '没有文件'}</div>`;
      return;
    }
    /* 一版可能有几千个零件：先铺一屏（120 行），剩下的按帧补。
       行里不写死 src，缩略图交给 IntersectionObserver 按「滑到哪」发请求，
       否则几千个 /api/preview 会一起排队，你滑到的那一屏永远排在最后。*/
    box.innerHTML = '';
    const CHUNK = 120;
    let i = 0;
    const step = () => {
      if (seq !== paintSeq) return;              // 又改了筛选条件，旧的一批作废
      const part = list.slice(i, i + CHUNK);
      if (!part.length) return;
      const frag = document.createElement('div');
      frag.innerHTML = part.map(fileRowHtml).join('');
      while (frag.firstChild) box.appendChild(frag.firstChild);
      wireLazyThumbs(box);
      i += CHUNK;
      if (i < list.length) requestAnimationFrame(step);
    };
    step();
  };
  paint();
  $$('.chip', detail).forEach((el) => {
    el.onclick = () => {
      S.fileView = el.dataset.v;
      $$('.chip', detail).forEach((x) => x.classList.toggle('on', x === el));
      paint();
    };
  });
  const find = $('.file-find', detail);
  if (find) {
    let timer = 0;
    find.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { S.fileQuery = find.value; paint(); }, 180);
    };
  }
  const extSel = bar && $('.file-ext', bar);
  if (extSel) extSel.onchange = () => { S.fileExt = extSel.value; paint(); };
  const sortSel = bar && $('.file-sort', bar);
  if (sortSel) sortSel.onchange = () => { S.fileSort = sortSel.value; paint(); };
  const dirBtn = bar && $('.sort-dir', bar);
  if (dirBtn) {
    dirBtn.onclick = () => {
      S.sortDir = -S.sortDir;
      dirBtn.textContent = S.sortDir > 0 ? '↑' : '↓';
      dirBtn.title = S.sortDir > 0 ? '当前升序，点一下改降序' : '当前降序，点一下改升序';
      paint();
    };
  }
  const selAll = bar && $('.sel-all', bar);
  if (selAll) {
    selAll.onclick = () => {
      const list = visible().filter((r) => r.st !== 'D');
      const allOn = list.length && list.every((r) => S.sel.has(r.path));
      list.forEach((r) => { if (allOn) S.sel.delete(r.path); else S.sel.add(r.path); });
      paint();
    };
  }
  if (btnStep) btnStep.onclick = () => exportStep(targets(), c);
  box.onchange = (ev) => {
    const cb = ev.target.closest ? ev.target.closest('.fsel') : null;
    if (!cb) return;
    const p = cb.closest('.frow').dataset.path;
    if (cb.checked) S.sel.add(p); else S.sel.delete(p);
    refreshSel();
  };
  let rowClickTimer = 0;
  box.onclick = (ev) => {
    if (ev.target.closest('.fcheck')) return;           // 勾选框自己处理，不当成点行
    if (ev.target.closest('.fdl')) return;              // 下载链接不触发对比
    if (ev.target.closest('.fstep')) {                  // 单个零件也能直接导 STEP
      exportStep([ev.target.closest('.frow').dataset.path], c);
      return;
    }
    if (ev.target.closest('.fbom')) {                   // 装配体直接出物料清单
      exportBom(ev.target.closest('.frow').dataset.path, c);
      return;
    }
    if (ev.target.closest('.fopen')) {                  // 「打开」用本机默认程序
      openFile(ev.target.closest('.frow').dataset.path);
      return;
    }
    if (ev.target.closest('.floc')) {                   // 「定位」在资源管理器里选中文件
      revealFile(ev.target.closest('.frow').dataset.path);
      return;
    }
    if (ev.target.closest('.fhist')) {                  // 「史」按钮看这个零件的改动历史
      showFileHistory(ev.target.closest('.frow').dataset.path);
      return;
    }
    if (ev.target.closest('.fbr')) {                    // 「单独改」开一条新分支并打开它
      openPartInNewBranch(ev.target.closest('.frow').dataset.path);
      return;
    }
    const el = ev.target.closest('.frow');
    if (!el) return;
    /* 单击要等一下：双击是「直接打开」，不能顺带弹出两次预览 */
    clearTimeout(rowClickTimer);
    rowClickTimer = setTimeout(() => {
      const path = el.dataset.path;
      if (needMesh(path)) {
        showModelCompare(path, el.dataset.old, el.dataset.oid, c.id);
        return;
      }
      const deleted = el.dataset.st === 'D';
      openPreview(path, el.dataset.st, {
        oid: deleted ? el.dataset.old : el.dataset.oid,
        oldOid: deleted ? '' : el.dataset.old,
        onDiff: (el.dataset.st === 'A' || el.dataset.st === 'M')
          ? () => showDiffText(d.parent, c.id, path) : null,
        onOpen: deleted ? null : () => openFile(path),
      });
    }, 210);
  };
  box.ondblclick = (ev) => {
    if (ev.target.closest('.fdl, .floc, .fopen, .fhist, .fbr, .fstep, .fbom')) return;
    const el = ev.target.closest('.frow');
    if (!el) return;
    clearTimeout(rowClickTimer);
    if (el.dataset.st !== 'D') openFile(el.dataset.path);
  };
}

/* ------------------------------------------------------------------ 导出 STEP */
/* 选中几个零件 → 后台用本机 SolidWorks 逐个转成 STEP → 全部落在一个文件夹里。
   导出的是**当前文件夹里**的这份文件（正看着历史版本时会提示）。 */
function suggestStepDest() {
  if (S.stepDest) return S.stepDest;
  const p = S.path || '';
  const i = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
  if (i <= 0) return '';
  const name = (S.project && S.project.name) || '导出';
  return p.slice(0, i) + p.slice(i, i + 1) + name + '-STEP';
}

/* CSV 单元格：逗号/引号/换行要转义；文本列还要挡掉 Excel 公式注入 ——
   一个零件名如果叫 `=cmd|' /C calc'!A0.sldprt`（同步来的、对方机器上的文件名，
   我们管不着），双击打开这张清单就等于替别人跑了一条命令。
   加个前导单引号，Excel 认作「这是文本」，显示出来还是原名。
   数字列（大小、差异）不加：加了就变成文本，排序/求和全失效。 */
function csvCell(v, numeric) {
  let s = String(v == null ? '' : v);
  if (!numeric && /^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

/* 这一版改了什么 → CSV。评审会、回复客户、交接班都用得上这张表 */
function saveChangeCsv(c, d) {
  const rows = fileRowsOf(d);
  const ch = d.changes || {};
  const rate = { A: '新增', M: '修改', D: '删除', '=': '未变' };
  const esc2 = (v) => csvCell(v);
  const out = [['文件路径', '状态', '旧大小', '新大小', '差异'].map(esc2).join(',')];
  for (const r of rows) {
    if (r.st === '=') continue;                  // 清单只列有变化的，几千行未变的没意义
    const delta = r.st === 'M' ? (r.size - (r.oldSize || 0))
      : (r.st === 'A' ? r.size : -(r.oldSize || 0));
    out.push([esc2(r.path), esc2(rate[r.st] || r.st),
      csvCell(r.st === 'A' ? '' : (r.oldSize || 0), true),
      csvCell(r.st === 'D' ? '' : (r.size || 0), true),
      csvCell(delta, true)].join(','));
  }
  const head = [`版本,${esc2(c.id)}`, `说明,${esc2((c.message || '').split('\n')[0])}`,
    `记录人,${esc2(c.author || '')}`, `时间,${esc2(fmtTime(c.time))}`,
    `统计,新增 ${(ch.added || []).length} / 修改 ${(ch.modified || []).length} / 删除 ${(ch.deleted || []).length}`, ''];
  const blob = new Blob(['﻿' + head.concat(out).join('\r\n')],
    { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `改动清单-${c.id.slice(0, 8)}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast(`改动清单已保存：${out.length - 1} 个文件有变化`, 4000);
}

/* 导出结果清单：交给供应商/工艺的时候，得有一张「哪个零件对应哪个文件、哪些没导成」的表 */
function saveStepCsv(res) {
  const ok = res.ok || [], failed = res.failed || [];
  const esc2 = (v) => csvCell(v);
  const lines = [['零件', '结果', '输出文件', '说明'].map(esc2).join(',')];
  const outs = res.outputs || {};
  for (const p of ok) {
    const rel = (p && p.rel) || p;                 // 服务端成功项是「相对路径」字符串
    lines.push([rel, '成功', (p && p.out) || outs[rel] || '', ''].map(esc2).join(','));
  }
  for (const f of failed) lines.push([f.rel || '（整批）', '失败', f.out || '', f.error || ''].map(esc2).join(','));
  const blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  a.href = URL.createObjectURL(blob);
  a.download = `STEP导出清单-${stamp}.csv`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast(`清单已保存：成功 ${ok.length} 个、失败 ${failed.length} 个`, 4000);
}

async function exportStep(files, commit) {
  const list = (files || []).filter(isSWPath);
  if (!list.length) return toast('选中的文件里没有 SolidWorks 零件/装配体', 3500);
  const body = document.createElement('div');
  const preview = list.slice(0, 4).map(baseName).join('、') + (list.length > 4 ? ' 等' : '');
  const isOld = !!(commit && S.project && commit.id !== S.project.head);
  body.innerHTML = `
    <div class="clone-info">
      <div class="ci-name">把 ${list.length} 个零件导出成 STEP</div>
      <div class="ci-sub">${esc(preview)}</div>
      ${isOld ? '<div class="ci-msg">⚠ 你正在看的是历史版本；导出用的是文件夹里现在这份文件</div>' : ''}
    </div>
    <div class="field"><label>STEP 放到哪个文件夹</label>
      <div class="row" style="margin:0">
        <input class="dest" style="flex:1" value="${esc(suggestStepDest())}"
          placeholder="点「浏览…」选一个文件夹（不存在会自动建）">
        <button class="browse" style="flex:none" title="打开 Windows 的资源管理器对话框选文件夹">浏览…</button>
      </div>
      <div class="muted small dest-hint">一个装配体会导成一个 STEP（把它的子零件都带进去）；
        同名不同目录的零件会带上层目录名，不会互相覆盖。</div>
      <div class="muted small dest-warn" hidden>⚠ 这个文件夹在项目目录里面：导出的 STEP
        会被下一次「记录」一起记进版本，项目体积会成倍增长。建议选项目旁边的文件夹。</div>
    </div>
    <div class="muted small sw-note">导出时会在后台启动 SolidWorks 逐个转格式，一台机器同时只跑一个。
      请先<b>保存并关闭</b>你正在画的 SolidWorks —— 开着会被直接拒绝（不是偷懒，
      是为了绝不关掉你没保存的图）。BOM 和导 STEP 也不能同时进行。</div>
    <div class="cp" hidden>
      <div class="cp-bar"><i style="width:0%"></i></div>
      <div class="cp-text muted small"></div>
    </div>
    <div class="step-fail" hidden></div>`;
  const m = openModal({
    title: '导出 STEP',
    body,
    width: 'min(620px,100%)',
    footer: '<button class="go primary">开始导出</button><button class="cancel">关闭</button>',
  });
  const destEl = $('.dest', body);
  const cp = $('.cp', body);
  const bar = $('.cp-bar i', cp);
  const ptext = $('.cp-text', cp);
  const failBox = $('.step-fail', body);
  const goBtn = $('.go', m.foot);
  const cancelBtn = $('.cancel', m.foot);
  let busy = false;

  const checkInside = guardInsideProject({
    destEl, goBtn, warnEl: $('.dest-warn', body),
    startText: '开始导出', warnText: '仍然导出到项目里',
  });
  $('.browse', body).onclick = async () => {
    const picked = await pickFolder(destEl.value);
    if (picked) {
      destEl.value = picked;
      destEl.dispatchEvent(new Event('input'));
    }
  };
  cancelBtn.onclick = () => {
    if (busy && !confirm('还在导出，关掉窗口的话任务会在后台继续。要关掉窗口吗？')) return;
    m.close();
  };

  goBtn.onclick = async () => {
    const dest = destEl.value.trim();
    if (!dest) return toast('先选一个文件夹');
    if (checkInside()) return;                 // 第一次点只提醒，再点一次才真开始
    busy = true;
    goBtn.disabled = true;
    goBtn.textContent = '正在导出…';
    $('.browse', body).disabled = true;
    destEl.disabled = true;
    cp.hidden = false;
    bar.style.width = '0%';
    ptext.textContent = '正在准备…';
    S.stepDest = dest;
    localStorage.setItem('zgr_step_dest', dest);
    let job;
    try {
      job = (await post('/api/export/step', {
        path: S.path, files: list, dest,
      })).job;
    } catch (e) {
      busy = false;
      goBtn.disabled = false;
      goBtn.textContent = '开始导出';
      $('.browse', body).disabled = false;
      destEl.disabled = false;
      cp.hidden = true;
      return toast('导出失败：' + e.message, 7000);
    }
    pollClone(job.id, (st) => {
      if (m.el.isConnected) {
        const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : 0;
        bar.style.width = (st.state === 'done' ? 100 : pct) + '%';
        ptext.textContent = st.state === 'error'
          ? '出错：' + st.error
          : `${st.phase || ''}${st.total ? ` · ${st.done}/${st.total}` : ''}${
            st.detail ? ' · ' + st.detail : ''}`;
        ptext.classList.toggle('bad', st.state === 'error');
      }
      if (st.state === 'done') finish(st);
      else if (st.state === 'error') {
        busy = false;
        if (m.el.isConnected) {
          goBtn.disabled = false;
          goBtn.textContent = '重试';
          $('.browse', body).disabled = false;
          destEl.disabled = false;
        }
        toast('导出失败：' + st.error, 8000);
      }
    }, '/api/export/step/status');

    function finish(st) {
      const res = st.result || {};
      busy = false;
      const failed = res.failed || [];
      if (m.el.isConnected) {
        goBtn.textContent = '完成';
        goBtn.disabled = true;
        // 「关闭」还是关闭，另给一个直接打开导出目录的按钮
        if (!$('.openfolder', m.foot)) {
          m.foot.insertAdjacentHTML('afterbegin',
            '<button class="openfolder">打开文件夹</button>');
          $('.openfolder', m.foot).onclick = () => {
            post('/api/openfolder', { target: res.dest || destEl.value.trim() })
              .catch((e) => toast('打开文件夹失败：' + e.message, 4000));
          };
        }
        if (failed.length) {
          failBox.hidden = false;
          const retryable = failed.filter((x) => x.rel);
          failBox.innerHTML = `<div class="sf-title">${failed.length} 个没导成：</div>`
            + failed.slice(0, 8).map((x) => `<div class="sf-row">${esc(x.rel || '（整批）')}
              — ${esc(x.error || '')}</div>`).join('')
            + (failed.length > 8 ? `<div class="sf-row muted">…还有 ${failed.length - 8} 个</div>` : '')
            + `<div class="sf-act">
                 ${retryable.length ? `<button class="sf-retry">只重试这 ${retryable.length} 个</button>` : ''}
                 <button class="sf-csv">导出清单 CSV</button>
               </div>`;
          const rb = $('.sf-retry', failBox);
          if (rb) {
            rb.onclick = () => {
              // 失败的多半是「SolidWorks 正忙/缺引用件」，修完再点这个，不用重导两千个
              if (!confirm(`只重新导出这 ${retryable.length} 个失败的吗？成功的那 ${(res.ok || []).length} 个不会动。`)) return;
              m.close();
              exportStep(retryable.map((x) => x.rel));
            };
          }
          $('.sf-csv', failBox).onclick = () => saveStepCsv(res);
        }
      }
      toast(`✓ 导出完成：${res.files || 0} 个 STEP${failed.length ? `，${failed.length} 个失败` : ''}`
        + ` → ${res.dest || dest}`, 7000);
    }
  };
}

/* ------------------------------------------------------------------- 物料清单 */
/* 装配体 → Excel + CSV。读的是本机 SolidWorks 的装配树：子装配装了几份，里面的
   零件就乘几份；同一个图号（没有图号就用文件名）合成一行。
   列的顺序和表头可以在项目根目录放 bom-template.json 改写，不用改代码。 */
const BOM_PREVIEW_ROWS = 400;

/* 导出的东西落在项目目录里面 → 下一次「记录」会把整批 STEP/BOM 记进版本，
   一个项目的 .zgr 立刻胀几倍。这里不硬拦（有人就是想这么放），但要说清楚、并按两次。 */
function insideProject(dest) {
  if (!dest || !S.path) return false;
  const norm = (p) => String(p).replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  const a = norm(dest);
  const b = norm(S.path);
  return a === b || a.startsWith(b + '\\');
}

/* 给「选目录 + 开始」这类对话框装一道二次确认：第一次点只是提醒，再点一次才开始 */
function guardInsideProject(opts) {
  const { destEl, goBtn, warnEl, startText, warnText } = opts;
  let warned = false;
  const refresh = () => {
    const inside = insideProject(destEl.value.trim());
    if (warnEl) warnEl.hidden = !inside;
    if (!inside) {
      warned = false;
      if (goBtn.textContent !== startText) goBtn.textContent = startText;
    } else if (warned && goBtn.textContent !== warnText) {
      goBtn.textContent = warnText;
    }
  };
  destEl.addEventListener('input', refresh);
  return () => {
    // 返回 true 表示「这次别开始，先让用户看清楚」
    if (!insideProject(destEl.value.trim()) || warned) return false;
    warned = true;
    refresh();
    return true;
  };
}

function bomCell(v) {
  if (v == null || v === '') return '';
  if (typeof v === 'number') {
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  }
  return String(v);
}

async function exportBom(rel, commit) {
  if (!/\.sldasm$/i.test(rel || '')) return toast('只有装配体（.sldasm）能出物料清单', 3500);
  const isOld = !!(commit && S.project && commit.id !== S.project.head);
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="clone-info">
      <div class="ci-name">给「${esc(baseName(rel))}」出物料清单</div>
      <div class="ci-sub">逐层读装配树 → Excel(.xlsx) + CSV，数量按层级累乘</div>
      ${isOld ? '<div class="ci-msg">⚠ 你正在看的是历史版本；读的是文件夹里现在这份装配体</div>' : ''}
    </div>
    <div class="field"><label>存到哪个文件夹</label>
      <div class="row" style="margin:0">
        <input class="dest" style="flex:1" value="${esc(S.bomDest || '')}"
          placeholder="留空 = 项目旁边的「项目名-BOM」">
        <button class="browse" style="flex:none" title="打开 Windows 的资源管理器对话框选文件夹">浏览…</button>
      </div>
      <div class="muted small dest-warn" hidden>⚠ 这个文件夹在项目目录里面：生成的 BOM
        文件会被下一次「记录」一起记进版本。建议选项目旁边的文件夹。</div>
    </div>
    <div class="field"><label>怎么合并同一种零件</label>
      <select class="merge">
        <option value="code" selected>按图号合并（推荐：同一个零件只留一行）</option>
        <option value="name">按名称合并（没填图号时用）</option>
        <option value="none">一行一个实例（保留层级，像目录树）</option>
      </select>
    </div>
    <label class="check" style="display:flex;gap:6px;align-items:center;margin-top:4px">
      <input type="checkbox" class="kids" checked> 包含子装配里的零件（不勾只要顶层）</label>
    <div class="muted small sw-note">生成时要用 SolidWorks 读一遍装配树，大装配体可能几分钟。
      如果 SolidWorks 正开着，会<b>借用</b>它来读（不会关掉你打开的图纸，但期间它显示为忙）；
      一台机器同时只能跑一个 SolidWorks 任务，<b>BOM 和导 STEP 不能同时进行</b>。</div>
    <div class="cp" hidden>
      <div class="cp-bar"><i style="width:0%"></i></div>
      <div class="cp-text muted small"></div>
    </div>
    <div class="bom-out" hidden></div>`;
  const m = openModal({
    title: '生成物料清单',
    body,
    width: 'min(760px,100%)',
    footer: '<button class="go primary">开始生成</button><button class="cancel">关闭</button>',
  });
  const destEl = $('.dest', body);
  const mergeEl = $('.merge', body);
  const kidsEl = $('.kids', body);
  const cp = $('.cp', body);
  const bar = $('.cp-bar i', cp);
  const ptext = $('.cp-text', cp);
  const out = $('.bom-out', body);
  const goBtn = $('.go', m.foot);
  let busy = false;

  const checkInside = guardInsideProject({
    destEl, goBtn, warnEl: $('.dest-warn', body),
    startText: '开始生成', warnText: '仍然生成到项目里',
  });
  $('.browse', body).onclick = async () => {
    const picked = await pickFolder(destEl.value);
    if (picked) {
      destEl.value = picked;
      destEl.dispatchEvent(new Event('input'));
    }
  };
  $('.cancel', m.foot).onclick = () => {
    if (busy && !confirm('还在生成，关掉窗口的话任务会在后台继续。要关掉窗口吗？')) return;
    m.close();
  };

  goBtn.onclick = async () => {
    if (checkInside()) return;                 // 第一次点只提醒，再点一次才真开始
    busy = true;
    goBtn.disabled = true;
    goBtn.textContent = '正在生成…';
    destEl.disabled = mergeEl.disabled = kidsEl.disabled = true;
    $('.browse', body).disabled = true;
    cp.hidden = false;
    out.hidden = true;
    bar.style.width = '0%';
    ptext.textContent = '正在启动 SolidWorks…';
    const dest = destEl.value.trim();
    if (dest) { S.bomDest = dest; localStorage.setItem('zgr_bom_dest', dest); }
    let job;
    try {
      job = (await post('/api/bom/export', {
        path: S.path, file: rel, dest,
        include_children: kidsEl.checked,
        merge_by: mergeEl.value,
      })).job;
    } catch (e) {
      reset();
      return toast('生成失败：' + e.message, 7000);
    }
    pollClone(job.id, (st) => {
      if (m.el.isConnected && st.state !== 'done') {
        bar.style.width = (st.state === 'done' ? 100 : '35%');
        ptext.textContent = st.state === 'error'
          ? '出错：' + st.error
          : `${st.phase || '处理中'}…${st.elapsed ? `（已 ${Math.round(st.elapsed)} 秒）` : ''}`;
        ptext.classList.toggle('bad', st.state === 'error');
      }
      if (st.state === 'done') finish(st);
      else if (st.state === 'error') {
        reset();
        toast('生成失败：' + st.error, 8000);
      }
    }, '/api/bom/status');

    function reset() {
      busy = false;
      if (!m.el.isConnected) return;
      goBtn.disabled = false;
      goBtn.textContent = '重试';
      destEl.disabled = mergeEl.disabled = kidsEl.disabled = false;
      $('.browse', body).disabled = false;
      cp.hidden = true;
    }

    function finish(st) {
      const res = st.result || {};
      const cols = st.columns || [];
      const rows = st.rows || [];
      busy = false;
      if (m.el.isConnected) {
        bar.style.width = '100%';
        ptext.textContent = '完成';
        goBtn.textContent = '完成';
        goBtn.disabled = true;
        const more = (st.total_rows || rows.length) - rows.length;
        const df = res.diff;
        const one = (r) => `<li>${esc(r.code || '—')} ${esc(r.name || '')} × ${esc(bomCell(r.qty))}</li>`;
        const cut = (arr) => arr.slice(0, 30).join('') + (arr.length > 30
          ? `<li class="muted">…另有 ${arr.length - 30} 行</li>` : '');
        let diffHtml;
        if (res.diff_skip) {
          diffHtml = `<div class="bom-diff muted small">${esc(res.diff_skip)}
            —— 想对比就把合并方式选成和上次一样的。</div>`;
        } else if (!df) {
          diffHtml = '<div class="bom-diff muted small">这是第一次生成这个装配体的清单——'
            + '下次再生成会自动和这一次比，多了哪些料、少了哪些、数量变了多少。</div>';
        } else {
          const nAll = df.added.length + df.removed.length + df.changed.length;
          diffHtml = `<div class="bom-diff">
            <b>和上一次比</b><span class="muted">（${fmtTime(df.old_time)} 那份）</span>
            ${df.added.length ? `<span class="bd-tag bom-add">新增 ${df.added.length} 行</span>` : ''}
            ${df.removed.length ? `<span class="bd-tag bom-del">少了 ${df.removed.length} 行</span>` : ''}
            ${df.changed.length ? `<span class="bd-tag bom-chg">数量变化 ${df.changed.length} 行</span>` : ''}
            ${nAll ? '' : '<span class="muted">清单和上一次完全一样</span>'}
            ${nAll ? `<details><summary>看明细</summary>
              ${df.added.length ? `<div class="bd-h">新增</div><ul class="bd-list">${cut(df.added.map(one))}</ul>` : ''}
              ${df.removed.length ? `<div class="bd-h">少了</div><ul class="bd-list">${cut(df.removed.map(one))}</ul>` : ''}
              ${df.changed.length ? `<div class="bd-h">数量变化</div><ul class="bd-list">${cut(df.changed.map((c) =>
                `<li>${esc(c.code || '—')} ${esc(c.name || '')}：${esc(bomCell(c.old))} → <b>${esc(bomCell(c.new))}</b> ${esc(c.unit || '')}</li>`))}</ul>` : ''}
            </details>` : ''}
          </div>`;
        }
        out.hidden = false;
        out.innerHTML = `
          ${diffHtml}
          <div class="bom-sum">
            <b>${esc(res.root || baseName(rel))}</b>
            · ${st.total_rows || rows.length} 行
            · 合计数量 ${bomCell(res.total_qty || 0)}
            · 读了 ${res.parts_seen || 0} 个零件实例
            ${res.missing ? `<span class="bom-warn">· ${res.missing} 个零件引用丢失，清单里没有它们${
              (res.missing_names || []).filter(Boolean).length
                ? '：' + esc(res.missing_names.filter(Boolean).slice(0, 6).join('、')) : ''}</span>` : ''}
            ${more > 0 ? `<span class="muted">（下面只预览前 ${rows.length} 行，全部在文件里）</span>` : ''}
          </div>
          ${(res.missing_attrs || []).length ? `<div class="bom-miss">
            <b>这些列有空缺</b>
            <span class="muted">（数据来自零件里的自定义属性，填了以后每次出的清单都全）</span>
            ${res.missing_attrs.map((m) => `<span class="bd-tag bom-chg">${esc(m.label)} ${m.n} 行没填</span>`).join('')}
            <details><summary>看是哪些零件</summary>
              ${res.missing_attrs.map((m) => `<div class="bd-h">没有${esc(m.label)}</div>
                <ul class="bd-list">${m.files.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>`).join('')}
            </details>
          </div>` : ''}
          <div class="bom-wrap">
            <table class="bom-table">
              <thead><tr>${cols.map((c) => `<th>${esc(c[0])}</th>`).join('')}</tr></thead>
              <tbody>${rows.map((r) => `<tr>${cols.map((c) => {
                const v = bomCell(r[c[1]]);
                return `<td${c[1] === 'qty' ? ' class="n"' : ''}>${esc(v)}</td>`;
              }).join('')}</tr>`).join('')}</tbody>
            </table>
          </div>`;
        const dl = (kind, label, cls) => {
          if (!$('.' + cls, m.foot)) {
            m.foot.insertAdjacentHTML('afterbegin',
              `<button class="${cls}">${label}</button>`);
            $('.' + cls, m.foot).onclick = () => downloadUrl(withToken(
              '/api/bom/file?' + new URLSearchParams({ id: st.id, kind }).toString()));
          }
        };
        dl('xlsx', '下载 Excel', 'dl-xlsx');
        dl('csv', '下载 CSV', 'dl-csv');
        if (!$('.openfolder', m.foot)) {
          m.foot.insertAdjacentHTML('afterbegin',
            '<button class="openfolder">打开文件夹</button>');
          $('.openfolder', m.foot).onclick = () => {
            post('/api/openfolder', { target: res.dest || destEl.value.trim() })
              .catch((e) => toast('打开文件夹失败：' + e.message, 4000));
          };
        }
      }
      toast(`✓ 物料清单已生成：${st.total_rows || rows.length} 行 → ${res.dest || dest}`, 7000);
    }
  };
}

/* 触发浏览器下载（图片/文件走服务端已经把 Content-Disposition 设好了） */
function downloadUrl(url) {
  const a = document.createElement('a');
  a.href = url;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function showDiff(a, b) {
  try {
    const d = await api('/api/diff', { path: S.path, a, b });
    const rows = [
      ...d.added.map((x) => ({ st: 'A', ...x })),
      ...d.modified.map((x) => ({ st: 'M', path: x.path, size: x.new_size, old: x.old_size })),
      ...d.deleted.map((x) => ({ st: 'D', ...x })),
    ];
    const body = document.createElement('div');
    body.innerHTML = `
      <div class="muted small" style="margin-bottom:10px">
        新增 ${d.counts.added} · 修改 ${d.counts.modified} · 删除 ${d.counts.deleted}
      </div>
      <div class="changes"></div>`;
    const list = $('.changes', body);
    if (!rows.length) list.innerHTML = '<div class="muted">两个版本内容相同</div>';
    rows.forEach((x) => {
      const el = document.createElement('div');
      el.className = 'chg';
      el.innerHTML = `<span class="st ${x.st}">${x.st}</span>
        <span class="p">${esc(x.path)}</span>
        <span class="sz">${fmtSize(x.size)}</span>`;
      el.onclick = () => showDiffText(x.st === 'A' ? null : a, b, x.path);
      list.appendChild(el);
    });
    openModal({ title: '版本对比', body, width: 'min(760px,100%)' });
  } catch (e) { toast(e.message, 4000); }
}

async function showDiffText(a, b, file) {
  try {
    const d = await api('/api/difftext', { path: S.path, a: a || '', b, file });
    let html = '';
    if (d.binary) {
      html = `<p class="muted">${esc(d.reason)}</p>`;
    } else {
      html = '<div class="diffbox" style="border:1px solid var(--line);border-radius:9px;overflow:auto;max-height:60vh;padding:8px 0">' +
        d.lines.map((ln) => {
          let cls = '';
          if (ln.startsWith('+++') || ln.startsWith('---')) cls = 'head';
          else if (ln.startsWith('@@')) cls = 'hunk';
          else if (ln.startsWith('+')) cls = 'add';
          else if (ln.startsWith('-')) cls = 'del';
          return `<div class="diffline ${cls}">${esc(ln) || '&nbsp;'}</div>`;
        }).join('') + '</div>';
    }
    openModal({ title: file, body: html, width: 'min(900px,100%)' });
  } catch (e) { toast(e.message, 4000); }
}

/* 模型前后对比：默认打开 3D 视图（能转、能叠加、改动标红），图纸类回退到缩略图 */
function showModelCompare(path, beforeOid, afterOid, commit) {
  if (window.openMeshViewer && isModel(path)) {
    // 先把已经缓存的缩略图垫在 3D 画布下面，网格算好之前用户也不是看白屏
    const poster = thumbUrl(afterOid || beforeOid, path, 512);
    window.openMeshViewer({ path: S.path, file: path, before: beforeOid,
                            after: afterOid, commit, poster });
    return;
  }
  showThumbCompare(path, beforeOid, afterOid);
}

/* 缩略图对比：左「上一版」右「此版本」，都按对象号从历史里取图 */
function showThumbCompare(path, beforeOid, afterOid) {
  const pair = [];
  if (beforeOid && beforeOid !== afterOid) pair.push({ label: '上一版', oid: beforeOid });
  if (afterOid) pair.push({ label: '此版本', oid: afterOid });
  if (!pair.length) return toast('没有可用的模型内容');
  const body = document.createElement('div');
  body.innerHTML = `<div class="pv-pair">${pair.map((p) => `
      <figure data-oid="${esc(p.oid)}">
        ${thumbImg(thumbUrl(p.oid, path, 512), 'big', path)}
        <figcaption>${esc(p.label)}</figcaption>
      </figure>`).join('')}</div>
    <p class="muted small">预览图按内容指纹缓存：同一零件在哪台机器、哪个版本都是同一张图。
      没有显示说明该格式暂不支持，可以在装了 SolidWorks 的机器上打开一次后再回来看。</p>`;
  const m = openModal({
    title: path, body, width: 'min(1000px,100%)',
    footer: '<button class="pv-reload">重新生成预览</button><button class="cancel">关闭</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.pv-reload', m.foot).onclick = async () => {
    for (const fig of $$('figure', body)) {
      const img = $('img.thumb', fig);
      if (!img) continue;
      img.classList.remove('broken');
      img.src = thumbUrl(fig.dataset.oid, path, 512) + '&force=1&t=' + Date.now();
    }
    toast('正在重新生成，稍等一下');
  };
}

function confirmUndo(c, target) {
  if (!target) return toast('找不到合并前的版本');
  const m = openModal({
    title: '撤销这次合并',
    body: `<p>会把项目内容回退到 <b>${esc(target.slice(0, 10))}</b>（这次合并之前的版本）。</p>
      <p class="muted small">回退本身也会生成一个新版本，所以合并结果仍留在历史里，随时能找回来；
      被覆盖或删除的文件先备份到 <code>.zgr/backup/</code>。</p>`,
    footer: '<button class="go primary">确认回退</button><button class="cancel">取消</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.go', m.foot).onclick = async () => {
    m.close();
    try {
      const r = await post('/api/undo', { path: S.path, commit: target, backup: true });
      toast('✓ 已回退，生成新版本 ' + r.commit.slice(0, 10), 4000);
      await selectProject(S.path, true);
      selectCommit(r.commit);
    } catch (e) { toast('回退失败：' + e.message, 5000); }
  };
}

/* 恢复整个项目是这套软件里最重的一步：它会改工作区里的一大批文件。
   确认框不再只说「确定恢复？」，而是先把试算结果摆出来——覆盖几个、新增几个、
   删掉几个、要写多大，再让你按。防呆的要点是「让人看见后果」，不是多弹一次框。 */
function confirmRestore(c) {
  const m = openModal({
    title: '恢复整个项目到此版本',
    width: 'min(620px,100%)',
    body: `<p>要把项目目录恢复为 <b>${esc((c.message || c.id.slice(0, 10)).split('\n')[0])}</b>（${fmtTime(c.time)}）。</p>
      <div class="restore-plan muted small">正在核对会动到哪些文件…</div>`,
    footer: '<button class="go danger" disabled>确认恢复</button><button class="cancel">取消</button>',
  });
  const goBtn = $('.go', m.foot);
  const planBox = $('.restore-plan', m.body);
  const sample = (arr, cls) => (arr || []).map((p) => `<li>${esc(p)}</li>`).join('');
  const renderPlan = (p) => {
    const rows = [
      ['覆盖', p.overwrite, '这些文件会被换成这一版的内容'],
      ['新增', p.create, '这一版有、工作区里没有'],
      ['删除', p.remove, '工作区里有、这一版没有（先备份再删）'],
      ['不变', p.same, '内容已经一样，不会重写'],
    ].filter((r) => r[1] > 0);
    const dirtyList = (p.dirty_sample || []).map((d) =>
      `<li>${esc(d.path)}<span class="muted">${d.kind === 'new' ? ' · 还没记录过的新文件' : ' · 记录了之后又改过'}</span></li>`).join('');
    planBox.innerHTML = `
      ${p.busy && p.busy.length ? `<p class="bom-warn"><b>有 ${p.busy.length} 个文件正被别的程序占用</b>
        （多半是 SolidWorks 还开着它们）：恢复会覆盖这些文件，而它们最新的改动现在读不到，
        也就没法先备份。想保险先关掉那个程序再恢复。${esc(p.busy.slice(0, 5).join('、'))}</p>` : ''}
      ${p.dirty ? `<p class="bom-warn"><b>工作区里还有 ${p.dirty} 个改动没记录</b>
        （${p.dirty_mod || 0} 个是记录后又改的、${p.dirty_new || 0} 个是新文件），
        恢复会把它们换掉或删掉。想留着就先关掉这个框、点「立即记录」存一版再回来。
        ${dirtyList ? `<details><summary>是哪几个</summary><ul>${dirtyList}</ul></details>` : ''}</p>` : ''}
      <table class="plan-table">${rows.map((r) =>
        `<tr><th>${r[0]}</th><td><b>${r[1]}</b> 个</td><td class="muted">${r[2]}</td></tr>`).join('')}
      </table>
      <p>一共要写 <b>${fmtSize(p.bytes || 0)}</b>，共 ${p.total} 个文件。</p>
      ${p.blocked && p.blocked.length ? `<p class="bom-warn">有 ${p.blocked.length} 个文件正被别的程序占用或设成了只读，
        恢复会直接中止、一个字节都不动：${esc(p.blocked.slice(0, 5).join('、'))}</p>` : ''}
      ${p.overwrite ? `<details><summary>会覆盖的文件（前 ${(p.overwrite_sample || []).length} 个）</summary>
        <ul>${sample(p.overwrite_sample)}</ul></details>` : ''}
      ${p.remove ? `<details><summary>会删掉的文件（前 ${(p.remove_sample || []).length} 个）</summary>
        <ul>${sample(p.remove_sample)}</ul></details>` : ''}
      <p class="muted">覆盖和删除的旧内容都会先备份到 <code>.zgr/backup/</code>，
        记录页的「回收站」里可以整批复原。</p>`;
  };
  post('/api/restore', { path: S.path, commit: c.id, dry: true }).then((r) => {
    renderPlan(r.plan || {});
    goBtn.disabled = false;
    // 一个文件都不用改的版本，按钮就别做成一按就「什么都没发生」
    if (!(r.plan && (r.plan.overwrite || r.plan.create || r.plan.remove))) {
      goBtn.textContent = '已经就是这个版本';
      goBtn.disabled = true;
    } else if (r.plan.dirty) {
      // 有没记录的改动：按钮上说清楚，别让人以为恢复是「轻轻的」
      goBtn.textContent = `仍要恢复（会覆盖 ${r.plan.dirty} 个未记录的改动）`;
      // 就地给一条出路：先把这些改动记一版，再回来恢复就什么都不丢
      m.foot.insertAdjacentHTML('afterbegin',
        '<button class="snapfirst" title="把这些没记录的改动先存成一个版本，之后照样能恢复">先记录一版</button>');
      $('.snapfirst', m.foot).onclick = () => { m.close(); snapshot('恢复前先存下当前改动'); };
    }
  }).catch((e) => {
    planBox.innerHTML = `<p class="bom-warn">没法先核对：${esc(e.message || String(e))}。
      仍然可以恢复，但看不到会动到哪些文件。</p>`;
    goBtn.disabled = false;
  });
  $('.cancel', m.foot).onclick = m.close;
  goBtn.onclick = async () => {
    goBtn.disabled = true;                 // 点一次就锁住，避免手抖点两遍
    goBtn.textContent = '恢复中…';
    try {
      const r = await post('/api/restore', { path: S.path, commit: c.id, backup: true });
      m.close();
      toast(`✓ 已恢复：写入 ${r.written} 个文件`
        + (r.removed && r.removed.length ? `，移除 ${r.removed.length} 个（已备份）` : ''), 5000);
      await selectProject(S.path, true);
    } catch (e) {
      goBtn.disabled = false;
      goBtn.textContent = '确认恢复';
      toast('恢复失败：' + (e.message || e), 6000);
    }
  };
}

async function doExport(c) {
  const dir = await pickFolder(S.project && S.project.path);
  if (!dir) return;
  try {
    const r = await post('/api/export', { path: S.path, commit: c.id, target: dir });
    toast(`已导出 ${r.written} 个文件到 ${dir}`, 4000);
  } catch (e) { toast('导出失败：' + e.message, 5000); }
}

/* ------------------------------------------------------------------ 未记录变更 */
const PENDING_MAX = 300;

function baseName(p) {
  return String(p || '').split(/[\\/]/).pop();
}
function dirName(p) {
  const s = String(p || '').replace(/\\/g, '/');
  const i = s.lastIndexOf('/');
  return i < 0 ? '' : s.slice(0, i);
}

/* 未记录变更：统一成带体积的条目（兼容老服务端只有路径数组的情况） */
function pendEntries(st) {
  st = st || {};
  if (Array.isArray(st.entries)) return st.entries;
  const out = [];
  (st.added || []).forEach((path) => out.push({ path, st: 'A', old_size: 0, new_size: 0, delta: 0, mtime: 0 }));
  (st.modified || []).forEach((path) => out.push({ path, st: 'M', old_size: 0, new_size: 0, delta: 0, mtime: 0 }));
  (st.deleted || []).forEach((path) => out.push({ path, st: 'D', old_size: 0, new_size: 0, delta: 0, mtime: 0 }));
  return out;
}

function fmtDelta(n) {
  const d = Number(n || 0);
  if (!d) return '';
  const cls = d > 0 ? 'up' : 'down';
  return `<em class="${cls}">${d > 0 ? '+' : '−'}${fmtSize(Math.abs(d))}</em>`;
}

/* 一行体积说明：新增/删除/修改各说清楚「原来多少、现在多少、差多少」 */
function sizeCell(e) {
  if (e.st === 'A') return `新增 <b>${fmtSize(e.new_size)}</b>`;
  if (e.st === 'D') return `原来 <b>${fmtSize(e.old_size)}</b> <em class="down">删除</em>`;
  return `现在 <b>${fmtSize(e.new_size)}</b> ${fmtDelta(e.delta)}`;
}

function renderPendingBar() {
  const bar = $('#pendingBar');
  if (!bar) return;
  const st = S.project && S.project.status;
  const list = pendEntries(st);
  // 被别的程序占着、现在读不出来的文件：状态里它们「没变」，但那可能是假的
  const busy = ((st && st.skipped) || []).filter((x) => x && x.path);
  if (!st || (!list.length && !busy.length)) {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  bar.hidden = false;
  // 路径太长和「被程序占着」不是一回事：前者关掉 SolidWorks 也不会好，
  // 让人去关程序等于白折腾，必须分开说，并给出真能解决的办法。
  const longs = busy.filter((x) => x.long);
  const occupied = busy.filter((x) => !x.long);
  const longNote = longs.length
    ? `<div class="pb-busy">⚠ ${longs.length} 个文件路径太长（如 ${esc(longs[0].path)}），
        SolidWorks 之类打开会失败——把中间几层文件夹改短，或把项目挪到浅一点的目录</div>` : '';
  const busyNote = occupied.length
    ? `<div class="pb-busy">⚠ ${occupied.length} 个文件正被别的程序占着（如 ${esc(occupied[0].path)}），
        这次看不准它们改了什么——关掉那个程序就会自动恢复</div>` : '';
  // 拖了好几天没记录：这些改动一个版本都没有，出事就真找不回来
  const lastT = (S.commits || []).reduce((m, c) => Math.max(m, c.time || 0), 0);
  const days = lastT ? (Date.now() - lastT) / 86400000 : 0;
  const staleNote = (list.length && days >= 1)
    ? `<div class="pb-stale">距上次记录已经 ${Math.floor(days)} 天——
        这些改动还没有任何版本可以退回</div>` : '';
  if (!list.length) {
    bar.innerHTML = longNote + busyNote;
    return;
  }
  const c = st.counts || {};
  const total = (st.totals || {}).delta || 0;
  const open = !!S.pendingBarOpen;
  const files = S.pendingShowAll ? list : list.slice(0, 60);
  bar.innerHTML = longNote + busyNote + staleNote + `<div class="pb-head" title="${open ? '收起' : '展开是哪些文件'}">
      <span class="pb-dot"></span>
      <span class="pb-title">未保存 ${list.length} 个文件</span>
      <span class="pb-sum">新增 ${c.added || 0} · 修改 ${c.modified || 0} · 删除 ${c.deleted || 0}${total ? ' · 合计 ' + (total > 0 ? '+' : '−') + fmtSize(Math.abs(total)) : ''}</span>
      <span class="pb-arrow">${open ? '收起 ▴' : '展开 ▾'}</span>
    </div>` +
    (open ? `<div class="pb-list">${files.map((e) => `
      <div class="pb-row" data-path="${esc(e.path)}" data-st="${esc(e.st)}" title="${esc(e.path)}">
        <span class="pb-st ${esc(e.st)}">${e.st}</span>
        <span class="pb-name">${esc(e.path)}</span>
        <span class="pb-size">${sizeCell(e)}</span>
      </div>`).join('')}${list.length > files.length
        ? `<div class="pb-more">还有 ${list.length - files.length} 个未显示</div>` : ''}</div>` : '');
  $('.pb-head', bar).onclick = () => { S.pendingBarOpen = !S.pendingBarOpen; renderPendingBar(); };
  $$('.pb-row', bar).forEach((row) => {
    row.onclick = () => openPreview(row.dataset.path, row.dataset.st);
  });
}

function renderPending() {
  const box = $('#pendingBody');
  const st = S.project && S.project.status;
  if (!st) { box.innerHTML = '<div class="empty">未加载</div>'; box.dataset.sig = ''; return; }
  if (st.clean) {
    box.innerHTML = '<div class="empty">✓ 所有文件都已记录<br><span class="small">改点东西再回来看看</span></div>';
    box.dataset.sig = '';
    return;
  }
  const list = pendEntries(st);
  const sig = list.map((e) => e.path + '|' + e.st + '|' + e.new_size).join('\n');
  // 3 秒轮询时内容没变就别重建 DOM（否则缩略图会闪）
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const groups = [['新增', 'A'], ['修改', 'M'], ['删除', 'D']];
  let html = '';
  groups.forEach(([name, sign]) => {
    const arr = list.filter((e) => e.st === sign);
    if (!arr.length) return;
    const show = S.pendingShowAll ? arr : arr.slice(0, PENDING_MAX);
    html += `<div class="group"><h4>${name} ${arr.length}</h4><div class="file-grid">` +
      show.map((e) => {
        const p = e.path;
        const thumb = canThumb(p)
          ? lazyThumb(fileThumbUrl(p, 256, sign === 'D', e.mtime), '', p, extOf(p))
          : `<div class="ext-badge">${esc(extOf(p))}</div>`;
        return `<div class="file-card ${sign}" data-path="${esc(p)}" data-st="${sign}"
            title="${esc(p)}">
          <div class="fc-img">${thumb}</div>
          <div class="fc-name">${esc(baseName(p))}</div>
          <div class="fc-sub">${esc(dirName(p))}</div>
          <div class="fc-size">${sizeCell(e)}</div>
          ${sign !== 'D' ? '<button class="fc-loc" title="打开所在位置">↗</button>' : ''}
        </div>`;
      }).join('') + '</div>';
    if (arr.length > show.length) {
      html += `<button class="more">显示全部 ${arr.length} 个</button>`;
    }
  });
  box.innerHTML = html;
  wireLazyThumbs(box);
  $$('.file-card', box).forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('.fc-loc')) { revealFile(el.dataset.path); return; }
      openPreview(el.dataset.path, el.dataset.st);
    };
  });
  $$('.more', box).forEach((b) => { b.onclick = () => { S.pendingShowAll = true; renderPending(); }; });
}

/* 每 3 秒跟一次「未保存」状态：不动硬盘上的东西，只读服务端算好的结果 */
async function pollStatus() {
  if (!S.path || document.hidden || S.statusBusy) return;
  S.statusBusy = true;
  try {
    const d = await api('/api/status', { path: S.path });
    if (!S.project) return;
    const headChanged = d.head && d.head !== S.project.head;
    S.project.status = d;
    S.project.head = d.head || S.project.head;
    const pill = $('#headPill');
    if (pill) {
      pill.textContent = d.head ? d.head.slice(0, 10) : '尚未记录';
      pill.classList.toggle('dirty', !d.clean);
    }
    renderPendingBar();
    renderBanner();
    if ($('#pane-pending') && $('#pane-pending').classList.contains('active')) renderPending();
    const dot = document.querySelector('.proj.active .dot');
    if (dot) dot.classList.toggle('dirty', !d.clean);
    const proj = (S.projects || []).find((p) => p.path.toLowerCase() === S.path.toLowerCase());
    if (proj) proj.dirty = !d.clean;
    if (headChanged) refreshQuiet();
  } catch (e) {
    /* 单次轮询失败不打扰；下一次继续 */
  } finally {
    S.statusBusy = false;
  }
}

/* 文件预览入口：
 *   Markdown / DXF / DWG / STEP / STL / 图片 → preview.js 里内置的开源引擎
 *   SolidWorks 等需要本机出网格的格式 → 缩略图弹窗 */
async function openPreview(rel, st, opts) {
  if (st === 'D' && !(opts && opts.oid)) {
    return toast('这个文件已删除，历史版本里还能看到它', 3200);
  }
  if (needMesh(rel)) return openModelThumb(rel);
  if (window.openFilePreview) {
    window.openFilePreview({
      path: S.path, file: rel, oid: '', st, ...(opts || {}),
    });
    return;
  }
  return openModelThumb(rel);
}

/* SolidWorks 模型的缩略图（工作区当前文件） */
async function openModelThumb(rel) {
  if (!canThumb(rel)) return toast('这个格式暂不支持预览', 3000);
  const m = openModal({
    title: rel,
    width: 'min(760px,100%)',
    body: `<div class="pv-one">${thumbImg(fileThumbUrl(rel, 512, true), 'big', rel)}</div>
      <p class="muted small">预览图由本机 SolidWorks 外壳缩略图生成；没有显示说明该格式暂不支持或文件太大。</p>`,
    footer: '<button class="pv-reload">重新生成</button><button class="cancel">关闭</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  $('.pv-reload', m.foot).onclick = () => {
    const img = $('img.thumb', m.body);
    if (img) { img.classList.remove('broken'); img.src = fileThumbUrl(rel, 512, true) + '&t=' + Date.now(); }
  };
}

/* ------------------------------------------------------------------ 动作 */
async function snapshot(message) {
  if (!S.path) return toast('请先选择项目');
  const m = openModal({
    title: '记录一次',
    body: `<div class="field"><label>这次改了什么？（可留空，留空会自动写成「记录 N 个文件：…」）</label>
        <input class="msg" placeholder="例如：客户确认的第三版立面"></div>
        <div class="err small snap-err"></div>`,
    footer: '<button class="go primary">记录</button><button class="cancel">取消</button>',
  });
  const input = $('.msg', m.body);
  const errBox = $('.snap-err', m.body);
  const goBtn = $('.go', m.foot);
  input.value = message || '';
  setTimeout(() => input.focus(), 30);
  $('.cancel', m.foot).onclick = m.close;
  const go = async () => {
    if (goBtn.disabled) return;              // 手抖连点两下不要记两版
    const msg = input.value;                 // 先留住：失败时还要用的就是这行字
    goBtn.disabled = true;
    goBtn.textContent = '记录中…';
    errBox.textContent = '';
    try {
      const r = await post('/api/snapshot', { path: S.path, message: msg });
      // 成功才关窗。以前是先关再发请求：碰上「有文件还在被写入」被拒，
      // 用户刚敲的那句说明跟着弹窗一起没了，只能等它写完重新打一遍。
      m.close();
      if (r.commit) {
        const sk = (r.skipped || []).filter((x) => x && x.path);
        if (sk.length) {
          // 有文件正在写、这次没收录：必须说出来，别让人以为「已经全存好了」
          toast('✓ 已记录 ' + r.commit.slice(0, 10)
            + `；另有 ${sk.length} 个文件正在写入，这次没收录（${sk[0].path}${sk.length > 1 ? ' 等' : ''}）`, 7000);
        } else {
          toast('✓ 已记录 ' + r.commit.slice(0, 10));
        }
        await selectProject(S.path, true);
        selectCommit(r.commit);
      } else {
        toast('没有变化，无需记录');
      }
    } catch (e) {
      goBtn.disabled = false;
      goBtn.textContent = '重试记录';
      errBox.textContent = e.message;
      input.focus();
    }
  };
  goBtn.onclick = go;
  input.onkeydown = (e) => { if (e.key === 'Enter') go(); };
}

function syncSource() {
  const live = (S.project && S.project.live && S.project.live.url) || '';
  return {
    url: ($('#syncUrl') && $('#syncUrl').value.trim().replace(/\/+$/, '')) || live,
    share: ($('#shareDir') && $('#shareDir').value.trim()) || '',
    token: ($('#syncToken') && $('#syncToken').value.trim()) || null,
  };
}

function saveSyncPrefs() {
  const s = syncSource();
  localStorage.setItem('zgr_sync', JSON.stringify({ url: s.url, share: s.share }));
}

function loadSyncPrefs() {
  try {
    const s = JSON.parse(localStorage.getItem('zgr_sync') || '{}');
    if (s.url && $('#syncUrl')) $('#syncUrl').value = s.url;
    if (s.share && $('#shareDir')) $('#shareDir').value = s.share;
  } catch (e) { /* 忽略损坏的本地设置 */ }
}

async function syncNow(mode, useShare) {
  if (!S.path) return toast('请先选择项目');
  const src = syncSource();
  if (useShare && !src.share) return toast('请填写共享文件夹路径');
  if (!useShare && !src.url) return toast('请填写对方地址');
  saveSyncPrefs();
  const log = $('#syncLog');
  log.textContent = `开始${mode === 'pull' ? '拉取' : mode === 'push' ? '推送' : '双向同步'}…\n`;
  try {
    const r = await post('/api/sync', {
      path: S.path,
      url: useShare ? undefined : src.url,
      share: useShare ? src.share : undefined,
      token: src.token,
      push: mode !== 'pull',
      pull: mode !== 'push',
      checkout: $('#syncCheckout').checked,
      force: $('#syncForce').checked,
    });
    log.textContent += (r.messages || []).map((x) => '· ' + x).join('\n') +
      `\n完成。当前版本 ${(r.head || '空').slice(0, 12)}`;
    toast('同步完成');
    await selectProject(S.path, true);
  } catch (e) {
    log.textContent += '✗ ' + e.message;
    toast('同步失败：' + e.message, 6000);
  }
}

/* ------------------------------------------------------------------ 选择性合并 */
const STATUS_LABEL = {
  conflict: '两边都改了', theirs: '对方改的', theirs_only: '对方新增',
  ours: '本机改的', ours_deleted: '本机这边删了', theirs_deleted: '对方删了',
  same: '内容相同',
};

/* 「一边改了、另一边删了」按「只有一边改过」写会误导：用户会以为对方没动过这个文件。
   这里有基点就说明以前存在过，那就不是「新增」。 */
function mergeEntryLabel(e) {
  if (e.status === 'theirs_only' && e.base) return '对方改了、本机删了';
  if (e.status === 'ours' && !e.theirs && e.base) return '本机改了、对方删了';
  return STATUS_LABEL[e.status] || e.status;
}
const MERGE_MAX = 300;

function mergeOptions(e) {
  const o = !!e.ours;
  const t = !!e.theirs;
  const smart = e.merged ? [['merged', '智能合并结果']] : [];
  if (o && t) {
    return smart.concat([['ours', '保留本机'], ['theirs', '采用对方'],
      ['both', '两边都保留'], ['delete', '删除这个文件']]);
  }
  if (o) return smart.concat([['ours', '保留本机'], ['delete', '跟随对方删除']]);
  if (t) return smart.concat([['theirs', '采用对方'], ['delete', '不要这个文件']]);
  return [['delete', '删除']];
}

async function planMerge() {
  if (!S.path) return toast('请先选择项目');
  const src = syncSource();
  if (!src.url && !src.share) return toast('先填对方地址或共享文件夹');
  saveSyncPrefs();
  const body = $('#mergeBody');
  body.innerHTML = '<div class="muted small">正在检查对方…（大项目首次会下载缺的对象，稍等）</div>';
  $('#btnApplyMerge').disabled = true;
  try {
    const d = await post('/api/merge/plan', {
      path: S.path, url: src.url || undefined, share: src.share || undefined,
      token: src.token, smart: $('#mergeSmart') ? $('#mergeSmart').checked : true,
    });
    S.plan = d;
    S.mergeShowAll = false;
    renderMerge();
  } catch (e) {
    body.innerHTML = `<div class="err small">${esc(e.message)}</div>`;
  }
}

function renderMerge() {
  const d = S.plan;
  const body = $('#mergeBody');
  if (!d) { body.innerHTML = '<div class="muted small">尚未检查。</div>'; return; }
  const changed = d.entries.filter((e) => e.status !== 'same');
  const c = d.counts || {};
  $('#mergeMeta').textContent =
    `对方 ${d.remote_name || ''} · 对方新版本 ${(d.theirs_log || []).length} 个` +
    ` · 差异文件 ${changed.length} 个 · 冲突 ${(d.conflicts || []).length} 个`;
  $('#btnApplyMerge').disabled = !changed.length;
  if (!changed.length) {
    body.innerHTML = '<div class="empty">✓ 对方没有比本机新的内容</div>';
    return;
  }
  // 冲突排最前，其次是两边都动过的，最后是单边变化
  const rank = { conflict: 0, theirs: 1, ours: 1, theirs_deleted: 2, ours_deleted: 2,
    theirs_only: 3 };
  const list = changed.slice().sort((a, b) => (rank[a.status] ?? 4) - (rank[b.status] ?? 4));
  const show = S.mergeShowAll ? list : list.slice(0, MERGE_MAX);
  let html = '';
  if (d.theirs_log && d.theirs_log.length) {
    html += `<details class="remote-log"><summary>对方多出来的 ${d.theirs_log.length} 个版本</summary>` +
      d.theirs_log.map((x) => `<div class="rl"><span class="mono">${x.id.slice(0, 8)}</span>
        <span class="rl-msg">${esc((x.message || '').split('\n')[0])}</span>
        <span class="muted small">${fmtTime(x.time)}</span></div>`).join('') + '</details>';
  }
  const hidden = list.length - show.length;
  if (hidden > 0) {
    // 这句必须放在**列表上面**：放在 300 行下面，等于要用户先滚到底才看见。
    // 以前这里只写「还有 N 个，显示全部」——用户调了两处就点合并，
    // 剩下几百个（含他没看见的冲突）是按默认规则一起合进去的，合完才发现
    // 有个文件被「保留本机」顶掉了。
    html += `<div class="merge-more-note">还有 ${hidden} 个没列出来，
      点「合并」时它们也会一起合进去：<b>冲突的保留本机</b>、<b>只有对方改过的采用对方</b>。
      想逐个确认请先点列表下面的「显示剩下的 ${hidden} 个」。</div>`;
  }
  html += '<div class="merge-list">' + show.map((e) => {
    const opts = mergeOptions(e);
    const pick = opts.some(([v]) => v === e.pick) ? e.pick : opts[0][0];
    const oid = (e.ours && e.ours.oid) || (e.theirs && e.theirs.oid) || '';
    const thumb = canThumb(e.path)
      ? `<span class="m-thumb">${thumbImg(thumbUrl(oid, e.path, 128), '', e.path)}</span>`
      : '<span class="m-thumb"><span class="ext-badge">' + esc(extOf(e.path)) + '</span></span>';
    return `<div class="merge-row ${e.status}" data-path="${esc(e.path)}">
      ${thumb}
      <span class="m-path" title="${esc(e.path)}">${esc(e.path)}</span>
      <span class="m-status">${esc(mergeEntryLabel(e))}${e.auto ? ' · <b class="auto-tag">智能合并</b>' : ''}</span>
      <span class="m-size">${fmtSize(e.size)}</span>
      <select>${opts.map(([v, t]) => `<option value="${v}" ${v === pick ? 'selected' : ''}>${t}</option>`).join('')}</select>
    </div>`;
  }).join('') + '</div>';
  if (hidden > 0) {
    html += `<button class="more">显示剩下的 ${hidden} 个</button>`;
  }
  body.innerHTML = html;
  const mb = $('.more', body);
  if (mb) mb.onclick = () => { S.mergeShowAll = true; renderMerge(); };
}

async function applyMerge() {
  if (!S.plan) return toast('请先检查对方改动');
  const choices = {};
  $$('.merge-row', $('#mergeBody')).forEach((row) => {
    const sel = $('select', row);
    if (sel) choices[row.dataset.path] = sel.value;
  });
  const src = syncSource();
  const btn = $('#btnApplyMerge');
  btn.disabled = true;
  btn.textContent = '合并中…';
  try {
    const r = await post('/api/merge/apply', {
      path: S.path, url: src.url || undefined, share: src.share || undefined,
      token: src.token, choices, checkout: true, backup: true,
      strategy: ($('#mergeSmart') && $('#mergeSmart').checked) ? 'smart' : 'manual',
      message: ($('#mergeMsg') && $('#mergeMsg').value.trim()) || '',
    });
    toast(`✓ 已合并 ${r.files} 个文件，新版本 ${r.commit.slice(0, 10)}`, 4000);
    S.plan = null;
    $('#mergeBody').innerHTML = '<div class="empty">✓ 已合并。可以在时间线里查看，或撤销这次合并</div>';
    $('#mergeMsg').value = '';
    await selectProject(S.path, true);
    selectCommit(r.commit);
  } catch (e) {
    toast('合并失败：' + e.message, 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = '合并所选';
  }
}

/* ------------------------------------------------------------------ 新建/添加 */
async function newProject() {
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="field"><label>项目目录</label>
      <div class="row" style="margin:0">
        <input class="p" placeholder="选择一个空文件夹或已有资料目录" style="flex:1">
        <button class="browse" style="flex:none" title="打开 Windows 的资源管理器对话框选文件夹">浏览…</button>
        <button class="fromfile" style="flex:none"
          title="选一个或多个文件，用它们所在的文件夹当作项目目录">从文件…</button>
      </div>
    </div>
    <div class="field"><label>项目名称</label><input class="n" placeholder="留空取文件夹名"></div>
    <label class="check" style="display:flex;gap:6px;align-items:center">
      <input type="checkbox" class="w"> 开启自动记录（服务运行期间，默认关闭）</label>`;
  const m = openModal({
    title: '新建追光几何项目',
    body,
    footer: '<button class="go primary">创建</button><button class="cancel">取消</button>',
  });
  $('.browse', body).onclick = async () => {
    const p = await pickFolder($('.p', body).value);
    if (p) {
      $('.p', body).value = p;
      if (!$('.n', body).value.trim()) {
        $('.n', body).value = p.split(/[\\/]/).filter(Boolean).pop() || '';
      }
    }
  };
  /* 知道文件在哪、但不知道文件夹路径时用这个：选几个文件，取它们的公共目录 */
  $('.fromfile', body).onclick = async () => {
    const files = await pickFiles($('.p', body).value, true, '选文件（用它们所在的文件夹）');
    if (!files.length) return;
    const dirs = Array.from(new Set(files.map((f) => f.replace(/[\\/][^\\/]*$/, ''))));
    if (dirs.length > 1) {
      return toast(`选的文件在 ${dirs.length} 个不同文件夹里，请只选同一个文件夹里的`, 5000);
    }
    $('.p', body).value = dirs[0];
    if (!$('.n', body).value.trim()) {
      $('.n', body).value = dirs[0].split(/[\\/]/).filter(Boolean).pop() || '';
    }
    toast(`✓ 用这 ${files.length} 个文件所在的文件夹：${dirs[0]}`, 4200);
  };
  $('.cancel', m.foot).onclick = m.close;
  $('.go', m.foot).onclick = async () => {
    const target = $('.p', body).value.trim();
    if (!target) return toast('请选择目录');
    try {
      const r = await post('/api/init', {
        target, name: $('.n', body).value.trim() || undefined,
        watch: $('.w', body).checked,
      });
      m.close();
      toast('✓ 项目已创建');
      await loadProjects();
      selectProject(r.path);
    } catch (e) { toast('创建失败：' + e.message, 5000); }
  };
}

async function addProject() {
  const dir = await pickFolder();
  if (!dir) return;
  try {
    await post('/api/add', { target: dir });
    toast('✓ 已添加');
    await loadProjects();
    selectProject(dir);
  } catch (e) { toast(e.message, 5000); }
}

/* 一键把任意文件夹纳入版本记录：已经是项目就直接加，不是就现场建立 */
async function addFolder() {
  const dir = await pickFolder();
  if (!dir) return;
  try {
    await post('/api/add', { target: dir });
    toast('✓ 已加入项目列表');
  } catch (e) {
    if (!/还不是追光几何项目/.test(e.message)) return toast(e.message, 5000);
    if (!confirm('「' + dir + '」还不是追光几何项目。\n\n现在就为它建立版本记录吗？\n'
                 + '不会移动、不会删除任何文件，只会在文件夹里多一个 .zgr 目录。')) return;
    try {
      // 默认不自动记录：只在点「立即记录」时存版本，时间线才干净
      await post('/api/init', { target: dir, watch: false });
      toast('✓ 已建立项目（只手动记录）', 3500);
    } catch (e2) { return toast('创建失败：' + e2.message, 5000); }
  }
  await loadProjects();
  selectProject(dir);
}

/* ------------------------------------------------------------------ 体检 */
function fsckSummary(rep) {
  const bad = (rep.bad || []).length, miss = (rep.missing || []).length;
  if (rep.ok) return `✓ 体检通过：${rep.objects} 个对象都在`;
  let s = `✖ 体检没通过：`;
  if (miss) s += `缺 ${miss} 个对象`;
  if (bad) s += `${miss ? '，' : ''}坏 ${bad} 个对象`;
  if ((rep.errors || []).length) s += `${miss || bad ? '，' : ''}${rep.errors.length} 条引用问题`;
  return s;
}

/* 项目体检（和 `zgr doctor` 同一份检查）：不是「对象坏没坏」，而是
   「今天这人会不会踩坑」——没记录的东西、对方压着的版本、盘还剩多少。 */
async function runDoctor() {
  if (!S.path) return toast('请先选择项目');
  const btn = $('#btnDoctor');
  if (!btn) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '体检中…';
  try {
    const r = await api('/api/doctor', { path: S.path });
    const rep = r.report || {};
    const body = document.createElement('div');
    const rows = [];
    const row = (cls, mark, msg, todo) => rows.push(
      `<div class="dr-row ${cls}"><div class="dr-msg">${mark} ${esc(msg)}</div>`
      + (todo ? `<div class="dr-todo">→ ${esc(todo)}</div>` : '') + '</div>');
    for (const x of (rep.bad || [])) row('dr-bad', '✖', x.msg, x.todo);
    for (const x of (rep.warn || [])) row('dr-warn', '!', x.msg, x.todo);
    for (const x of (rep.good || [])) row('dr-good', '✔', x.msg, '');
    const head = `<div class="muted small">分支 ${esc(rep.branch || '')} · 最新版本 `
      + `${esc((rep.head || '').slice(0, 10) || '尚未记录')}</div>`;
    const concl = (rep.bad || []).length
      ? `<div class="dr-concl bad">${rep.bad.length} 件事要先处理</div>`
      : ((rep.warn || []).length
        ? `<div class="dr-concl warn">能干活，有 ${rep.warn.length} 条提醒</div>`
        : '<div class="dr-concl good">一切正常</div>');
    body.innerHTML = head + rows.join('') + concl;
    const m = openModal({
      title: '项目体检 · ' + (rep.name || ''),
      body, width: 'min(660px,100%)',
      footer: '<button class="cancel">关闭</button>',
    });
    $('.cancel', m.foot).onclick = m.close;
  } catch (e) {
    toast('体检失败：' + (e.message || e), 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function runFsck() {
  if (!S.path) return toast('请先选择项目');
  const btn = $('#btnFsck');
  const old = btn.textContent;
  btn.disabled = true;
  try {
    btn.textContent = '体检中…';
    const r = await api('/api/fsck', { path: S.path });
    const rep = r.report || {};
    toast(fsckSummary(rep), rep.ok ? 4500 : 8000);
    if (!rep.ok) {
      const lines = [];
      for (const b of (rep.bad || []).slice(0, 8)) lines.push('坏：' + b.oid.slice(0, 12) + '  ' + b.why);
      for (const m of (rep.missing || []).slice(0, 8)) lines.push('缺：' + m.slice(0, 12));
      for (const e of (rep.errors || []).slice(0, 5)) lines.push(e);
      const peer = (S.project && S.project.live && S.project.live.url) || '';
      if (!peer) {
        alert('体检发现问题（图纸一个字节都没动）：\n\n' + lines.join('\n')
              + '\n\n先在「双机同步」里连上另一台电脑，就能把缺的/坏的对象补回来。');
        return;
      }
      if (!confirm('体检发现问题（图纸一个字节都没动）：\n\n' + lines.join('\n')
                   + '\n\n要从对方（' + peer + '）把缺的、坏的对象补回来吗？\n'
                   + '只补对象库，分支和图纸都不动。')) return;
      btn.textContent = '修复中…';
      const fix = await post('/api/repair', { path: S.path, url: peer });
      toast(`补回 ${fix.fetched} 个对象${fix.removed_bad ? '，替换坏对象 ' + fix.removed_bad + ' 个' : ''}`,
            fix.ok ? 5000 : 9000);
      if (!fix.ok) alert('没修完：\n\n' + (fix.problems || []).join('\n'));
      return;
    }
    // 快速检查只看「在不在」，深查才逐个核对内容——磁盘静默损坏只有它能查出来
    if (!confirm(`快速检查通过（${rep.objects} 个对象）。\n\n`
                 + '要不要再做一次「逐字节核对」？\n'
                 + '能发现磁盘悄悄改坏的数据，图纸多的话要几分钟，期间照常用。')) return;
    btn.textContent = '逐字节核对中…';
    const r2 = await api('/api/fsck', { path: S.path, deep: 1 });
    const rep2 = r2.report || {};
    toast(fsckSummary(rep2), rep2.ok ? 5000 : 9000);
    if (!rep2.ok) {
      alert('逐字节核对发现问题：\n\n'
            + (rep2.bad || []).slice(0, 8).map((b) => b.oid.slice(0, 12) + '  ' + b.why).join('\n'));
    }
  } catch (e) {
    toast('体检失败：' + e.message, 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* ------------------------------------------------------------------ 项目体积 */
async function showSizes() {
  if (!S.path) return toast('请先选择项目');
  const body = document.createElement('div');
  body.innerHTML = '<div class="muted small">正在统计…（几十万个对象时要几秒）</div>';
  const m = openModal({
    title: '这个项目占了多大地方', body, width: 'min(620px,100%)',
    footer: '<button class="clean-cache">清理缓存到上限</button>'
      + '<button class="clean-pv">清理预览缓存</button><button class="cancel">关闭</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  const cacheBtn = $('.clean-cache', m.foot);
  cacheBtn.hidden = true;
  cacheBtn.onclick = async () => {
    cacheBtn.disabled = true;
    try {
      const r = await post('/api/cache/prune', { path: S.path });
      const a = (r.previews || {}).removed || 0;
      const b = (r.meshes || {}).removed || 0;
      const over = (r.previews || {}).over || (r.meshes || {}).over;
      toast(a + b > 0 ? `✓ 删掉 ${a + b} 个缓存文件（都在上限以内了）`
        : (over ? '最近 10 分钟生成的缓存不动（可能正在用），稍后会自动清理'
          : '✓ 已经在上限以内，没删任何东西'), 5000);
      m.close();
      showSizes();
    } catch (e) {
      cacheBtn.disabled = false;
      toast('清理失败：' + (e.message || e), 6000);
    }
  };
  const pvBtn = $('.clean-pv', m.foot);
  pvBtn.hidden = true;
  pvBtn.onclick = async () => {
    const size = (rep.previews || {}).bytes || 0;
    if (!confirm(`清掉预览缓存（${fmtSize(size)}）？\n\n`
      + '只是缩略图和 DWG 预览的缓存，下次看图会重新生成，'
      + '版本历史、图纸本身一个字节都不动。')) return;
    pvBtn.disabled = true;
    try {
      await post('/api/preview/clear', { path: S.path });
      toast('✓ 预览缓存已清理', 4000);
      m.close();
      showSizes();
    } catch (e) {
      pvBtn.disabled = false;
      toast('清理失败：' + (e.message || e), 6000);
    }
  };
  let rep, policy = {};
  try {
    const got = await api('/api/size', { path: S.path });
    rep = got.report || {};
    policy = got.cache || {};
  } catch (e) {
    body.innerHTML = `<p class="bom-warn">统计失败：${esc(e.message || e)}</p>`;
    return;
  }
  const total = Math.max(1, rep.total || 0);
  const row = (label, d, note) => {
    const pct = Math.max(0, Math.min(100, Math.round(((d && d.bytes) || 0) * 100 / total)));
    return `<tr><th>${label}</th><td><b>${fmtSize((d && d.bytes) || 0)}</b></td>
      <td class="pct"><span style="width:${pct}%"></span></td>
      <td class="muted">${note}</td></tr>`;
  };
  const hist = rep.objects || {};
  const pv = rep.previews || {};
  const ms = rep.meshes || {};
  const pvMax = Number(policy.preview_max_mb || 0);
  const msMax = Number(policy.mesh_max_mb || 0);
  const over = (pvMax > 0 && (pv.bytes || 0) > pvMax * 1048576)
    || (msMax > 0 && (ms.bytes || 0) > msMax * 1048576);
  pvBtn.hidden = !(pv.bytes > 0);
  pvBtn.textContent = pv.bytes > 0 ? `清理预览缓存（${fmtSize(pv.bytes)}）` : '清理预览缓存';
  // 「清理到上限」只在真的超了的时候才亮出来，免得用户以为缓存坏了才需要点
  cacheBtn.hidden = !(over && policy.auto !== false);
  body.innerHTML = `
    <table class="plan-table size-table">
      ${row('图纸文件', rep.worktree, '当前工作区里的图纸，这个清理不了，也不该清理')}
      ${row('版本历史', hist, '每个版本的完整数据都在这，删掉的分支也算在里面')}
      ${row('回收站', rep.backup, `恢复/覆盖前的备份，共 ${(rep.backup || {}).batches || 0} 批`)}
      ${row('预览缓存', pv, '缩略图和 DWG 预览，删了下次看图会重新生成（不影响任何版本）'
        + (pvMax > 0 ? `；超过 ${pvMax} MB 会自动清最旧的` : '；不限量'))}
      ${row('网格缓存', ms, '3D 预览用的网格，删了下次转模型会重新生成（不影响任何版本）'
        + (msMax > 0 ? `；超过 ${msMax} MB 会自动清最旧的` : '；不限量'))}
      ${row('打包', rep.packs, '「打包发送」生成的 zip')}
      ${row('BOM 快照', rep.bom, '每次生成物料清单留的对比底稿，最多 20 份')}
      ${row('其它', rep.meta_other, '配置、索引、事件记录')}
    </table>
    <p>合计 <b>${fmtSize(rep.total || 0)}</b>${rep.partial
      ? '（太大，只数了一部分，实际只会更多）' : `（用时 ${rep.seconds || 0} 秒）`}。</p>
    <p class="muted small">能清的是「回收站」「预览缓存」「网格缓存」和「版本历史」里
      没人引用的那部分：回收站清旧备份，缓存删了会自动重建，版本历史清的是删掉的分支
      留下的老数据。缓存默认各留 2 GB，超了自动从最旧的删起（可关）；想立刻腾地方就点
      「清理缓存到上限」。</p>`;
}

/* ------------------------------------------------------------------ 跨版本找零件 */
function openSearch() {
  if (!S.path) return toast('请先选择项目');
  const body = document.createElement('div');
  body.innerHTML = `
    <input id="searchQ" class="search-q" type="search" autocomplete="off"
           placeholder="输入零件名或路径片段，例如 侧板、8mm铜扣、钣金">
    <div class="search-hits muted small" id="searchHint">会扫过这个项目的全部版本，第一次要几秒；删掉的老零件也能找到。</div>
    <div class="filerows search-res" id="searchRes"></div>`;
  const m = openModal({
    title: '在所有版本里找零件', body, width: 'min(760px,100%)',
    footer: '<button class="cancel">关闭</button>',
  });
  $('.cancel', m.foot).onclick = m.close;
  const input = $('#searchQ', body);
  const res = $('#searchRes', body);
  const hint = $('#searchHint', body);
  let seq = 0, timer = null, hits = [], sel = -1;
  // 键盘选结果：打着字不该还得去摸鼠标
  const paint = () => {
    $$('.search-row', res).forEach((el, i) => el.classList.toggle('sel', i === sel));
    const cur = $$('.search-row', res)[sel];
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  };
  const openHit = (i) => {
    const e = hits[i];
    if (!e) return;
    m.close();
    showFileHistory(e.path);
  };
  const run = async () => {
    const q = input.value.trim();
    const mine = ++seq;
    if (q.length < 1) {
      res.innerHTML = '';
      hits = []; sel = -1;
      hint.textContent = '会扫过这个项目的全部版本，第一次要几秒；删掉的老零件也能找到。';
      return;
    }
    hint.textContent = '正在扫全部版本…';
    try {
      const r = await api('/api/search', { path: S.path, q });
      if (mine !== seq) return;                 // 打字太快，丢掉过期结果
      const rows = r.rows || [];
      hint.textContent = rows.length
        ? `${r.total} 个匹配${r.total > rows.length ? `（列出前 ${rows.length} 个）` : ''} · 扫过 ${r.scanned} 个文件`
        : `没有匹配（扫过 ${r.scanned} 个文件）`;
      res.innerHTML = rows.map((e, i) => `
        <div class="frow search-row" data-i="${i}">
          <span class="fbody">
            <span class="fline1"><b class="fn">${esc(e.path.split('/').pop())}</b>
              ${e.in_head ? '' : '<span class="pill dead">已删除</span>'}</span>
            <span class="fline2"><i class="fdir" title="${esc(e.path)}">${esc(dirName(e.path) || '·')}</i>
              <span class="fsize">${e.changes} 次改动 · ${fmtSize(e.size)} · ${fmtTime(e.last)}</span></span>
            <span class="fline2"><i class="fdir">${esc((e.last_msg || '').split('\n')[0].slice(0, 40))}</i></span>
          </span>
        </div>`).join('');
      hits = rows; sel = rows.length ? 0 : -1;
      $$('.search-row', res).forEach((el) => {
        el.onclick = () => openHit(Number(el.dataset.i));
      });
      paint();
    } catch (err) {
      if (mine === seq) hint.textContent = '搜索失败：' + (err.message || err);
    }
  };
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(run, 260);               // 打字停一下再查，别每敲一个字扫一遍
  };
  input.onkeydown = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!hits.length) return;
      e.preventDefault();
      sel = e.key === 'ArrowDown'
        ? Math.min(hits.length - 1, sel + 1) : Math.max(0, sel - 1);
      paint();
    } else if (e.key === 'Enter') {
      if (sel >= 0 && hits[sel]) { e.preventDefault(); openHit(sel); }
    }
  };
  setTimeout(() => input.focus(), 60);
}

/* ------------------------------------------------------------------ 清理孤儿对象 */
async function runGc() {
  if (!S.path) return toast('请先选择项目');
  const btn = $('#btnGc');
  const old = btn.textContent;
  btn.disabled = true;
  try {
    btn.textContent = '核对中…';
    const r = await api('/api/gc', { path: S.path });
    const p = (r.plan || {});
    if (!p.total) {
      toast(`没有可回收的东西（没人引用的对象 ${p.dangling || 0} 个，`
        + `其中 ${p.kept_recent || 0} 个是最近 ${p.keep_days || 7} 天内的、`
        + `${p.kept_marked || 0} 个挂在打了标记的版本上，这些都不动）`, 7000);
      return;
    }
    if (!confirm(`可以回收 ${p.total} 个对象，省出约 ${fmtSize(p.bytes)}。\n\n`
                 + '这些是删掉的分支、退回旧版本之后留下的老数据——任何一条分支上都走不到了。\n'
                 + '回收后它们就彻底消失（图纸本身、所有分支上的版本一个都不动）。\n\n'
                 + '确定回收吗？')) return;
    btn.textContent = '回收中…';
    const r2 = await post('/api/gc', { path: S.path, keep_days: p.keep_days });
    const q = (r2.plan || {});
    toast(`✓ 已回收 ${q.removed} 个对象，省出 ${fmtSize(q.bytes)}`, 6000);
    await loadProjects();
  } catch (e) {
    toast('清理失败：' + (e.message || e), 7000);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* 全局快捷键：桌面软件该有的手感（都是「多一步都嫌烦」的高频动作） */
function bindHotkeys() {
  window.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (e.key === 'Escape') {
      // 弹窗自己会处理（stopPropagation）；走到这里说明焦点不在弹窗里
      const ov = document.querySelectorAll('#modalRoot .overlay');
      if (ov.length) {
        const top = ov[ov.length - 1];
        if (typeof top._zgrClose === 'function') top._zgrClose(); else top.remove();
        e.preventDefault();
      }
      return;
    }
    if (mod && (e.key === 'f' || e.key === 'F')) {   // 找零件
      e.preventDefault();
      openSearch();
    } else if (mod && e.key === 'Enter') {          // 记录一版
      e.preventDefault();
      if (!document.querySelector('#modalRoot .overlay')) snapshot();
    } else if (e.key === '?' && !mod) {             // 快捷键说明（打字时不抢）
      const t = e.target;
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA'
        || t.tagName === 'SELECT' || t.isContentEditable);
      if (!typing) { e.preventDefault(); showHotkeyHelp(); }
    }
  });
}

/* 快捷键说明：写给「不想记」的人——按 ? 就能看，不用翻文档 */
function showHotkeyHelp() {
  if (document.querySelector('#modalRoot .overlay')) return;   // 已经开着别的窗就别叠
  const rows = [
    ['Ctrl + F', '找零件（跨所有版本按文件名搜，删掉的也能找到）'],
    ['Ctrl + Enter', '记录一版（拍下当前文件夹的快照）'],
    ['搜索框里 ↓ / ↑ / Enter', '在「全历史」搜索结果里走、打开这一版'],
    ['Esc', '关闭当前弹窗（焦点在输入框里也管用）'],
    ['?', '打开这张快捷键说明'],
  ];
  const body = document.createElement('div');
  body.innerHTML = `<table class="plan-table">${rows.map(([k, v]) =>
    `<tr><td style="white-space:nowrap"><kbd>${esc(k)}</kbd></td><td>${esc(v)}</td></tr>`).join('')}
    </table><p class="hint">在输入框里打字时不会触发快捷键。</p>`;
  const m = openModal({
    title: '键盘快捷键', body, width: 'min(560px,100%)',
    footer: '<button class="go primary">知道了</button>',
  });
  $('.go', m.foot).onclick = m.close;
}

/* ------------------------------------------------------------------ 事件绑定 */
function bind() {
  $('#btnRefresh').onclick = async () => {
    await loadProjects();
    if (S.path) await selectProject(S.path, true);
    loadPeerProjects(true);
    toast('已刷新');
  };
  $('#btnSnapshot').onclick = () => snapshot();
  const peerBtn = $('#btnPeerRefresh');
  if (peerBtn) peerBtn.onclick = () => loadPeerProjects(false);
  $('#btnNew').onclick = newProject;
  $('#btnAdd').onclick = addProject;
  $('#btnAddFolder').onclick = addFolder;
  const doctorBtn = $('#btnDoctor');
  if (doctorBtn) doctorBtn.onclick = runDoctor;
  const fsckBtn = $('#btnFsck');
  if (fsckBtn) fsckBtn.onclick = runFsck;
  const gcBtn = $('#btnGc');
  if (gcBtn) gcBtn.onclick = runGc;
  const searchBtn = $('#btnSearch');
  if (searchBtn) searchBtn.onclick = openSearch;
  const sizeBtn = $('#btnSize');
  if (sizeBtn) sizeBtn.onclick = showSizes;
  // 「🛠 工具」菜单：五个工具平时收着，点开才占地方；选完自动收起
  const toolsBtn = $('#btnTools');
  const toolsMenu = $('#toolsMenu');
  if (toolsBtn && toolsMenu) {
    const closeTools = () => {
      toolsMenu.hidden = true;
      toolsBtn.setAttribute('aria-expanded', 'false');
    };
    toolsBtn.onclick = (e) => {
      e.stopPropagation();
      const willOpen = toolsMenu.hidden;
      toolsMenu.hidden = !willOpen;
      toolsBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    };
    document.addEventListener('click', (e) => {
      if (toolsMenu.hidden) return;
      if (toolsMenu.contains(e.target) || toolsBtn.contains(e.target)) return;
      closeTools();
    });
    toolsMenu.querySelectorAll('button').forEach((b) => b.addEventListener('click', closeTools));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !toolsMenu.hidden) closeTools();
    });
  }
  $('#btnLiveOn').onclick = () => saveLive(true);
  if ($('#liveMode')) $('#liveMode').onchange = applyLiveMode;
  $('#btnLiveOff').onclick = () => saveLive(false);
  $('#btnAutoPair').onclick = async () => {
    if (!S.path || S.pairBusy) return;
    S.pairBusy = true;
    const btn = $('#btnAutoPair');
    btn.disabled = true;
    btn.textContent = '正在扫描局域网…';
    try {
      const d = await post('/api/autopair', { path: S.path });
      applyLive(d, true);
      if (d.running) {
        toast(`✓ 已自动配对：${d.peer_author || d.peer_host || d.url}`, 4000);
        loadFeed(false);
      } else {
        toast('这台局域网里没找到有同名项目的另一台电脑（确认对方开着追光几何、在同一网段）', 6000);
      }
    } catch (e) {
      toast(e.message, 5000);
    } finally {
      S.pairBusy = false;
      btn.disabled = false;
      btn.textContent = '立即查找对方';
    }
  };
  $('#btnLoadBackups').onclick = loadBackups;
  $('#btnPackLatest').onclick = () => packVersion(null, false);
  $('#btnSaveAuthor').onclick = async () => {
    if (!S.path) return toast('请先选择项目');
    try {
      const r = await post('/api/author', { path: S.path, author: $('#authorName').value.trim() });
      $('#authorName').value = r.author;
      if (S.project) S.project.author = r.author;
      // 名字一改，「只看我的」和「对方保存」的判定都跟着变：当场重画，
      // 别让人看着旧名字的标注发呆
      syncMineButton();
      renderTimeline();
      toast('✓ 记录人已保存：' + r.author);
    } catch (e) { toast(e.message, 4000); }
  };
  $('#liveUrl').addEventListener('input', () => { $('#liveUrl').dataset.touched = '1'; });
  $('#liveUrl').addEventListener('change', () => {
    if ($('#syncUrl')) $('#syncUrl').value = $('#liveUrl').value.trim();
    saveSyncPrefs();
  });
  $('#btnCommit').onclick = async () => {
    const btn = $('#btnCommit');
    // 大项目记一版要等一两秒：这期间按钮必须变成「记录中…」并且不吃第二次点击，
    // 否则连点 / 手快回车会排进第二次记录请求，用户看到「已记录」却不知道记了几次
    if (btn.disabled) return;
    const oldText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '记录中…';
    try {
      const r = await post('/api/snapshot', { path: S.path, message: $('#commitMsg').value });
      $('#commitMsg').value = '';
      if (r.commit) {
        toast('✓ 已记录 ' + r.commit.slice(0, 10));
        await selectProject(S.path, true);
        selectCommit(r.commit);
      } else toast('没有变化');
    } catch (e) { toast('记录失败：' + e.message, 5000); }
    finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  };
  $('#commitMsg').onkeydown = (e) => { if (e.key === 'Enter') $('#btnCommit').click(); };
  $('#searchBox').onkeydown = (e) => {
    if (e.key !== 'ArrowDown') return;
    const first = $('.find-row', $('#findBox'));
    if (first) { e.preventDefault(); first.focus(); }
  };
  $('#searchBox').oninput = (e) => {
    S.filter = e.target.value;
    renderTimeline();
    scheduleFind();
  };
  if ($('#btnMineOnly')) $('#btnMineOnly').onclick = toggleMineOnly;
  syncMineButton();
  // 「今天」那一行本身是个开关。用 button，所以回车/空格天然可切换
  if ($('#dayNote')) $('#dayNote').onclick = toggleToday;
  if ($('#btnCopyToday')) $('#btnCopyToday').onclick = copyToday;
  $$('#tabs button').forEach((b) => {
    b.onclick = () => {
      S.tab = b.dataset.tab;
      $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
      $$('.tab-pane').forEach((p) => p.classList.toggle('active', p.id === 'pane-' + S.tab));
      if (S.tab === 'settings' && S.path) loadBackups();
      if (S.tab === 'sync' && S.path) { loadFeed(false); loadRemotes(); }
    };
  });
  $('#btnAddRemote').onclick = saveRemote;
  $('#remoteKind').onchange = showRemoteFields;
  showRemoteFields();
  $('#btnPull').onclick = () => syncNow('pull');
  $('#btnPush').onclick = () => syncNow('push');
  $('#btnBoth').onclick = () => syncNow('both');
  $('#btnSharePull').onclick = () => syncNow('pull', true);
  $('#btnSharePush').onclick = () => syncNow('push', true);
  $('#btnPlanMerge').onclick = planMerge;
  $('#btnApplyMerge').onclick = applyMerge;
  $('#syncUrl').addEventListener('change', saveSyncPrefs);
  $('#shareDir').addEventListener('change', saveSyncPrefs);
  $('#btnSaveToken').onclick = () => {
    S.token = $('#uiToken').value.trim();
    if (S.token) localStorage.setItem('zgr_token', S.token);
    else localStorage.removeItem('zgr_token');
    toast(S.token ? '✓ 令牌已保存' : '✓ 已清除令牌');
  };
  $('#btnSaveWatch').onclick = async () => {
    try {
      await post('/api/watch', {
        path: S.path, on: $('#watchOn').checked,
        interval: Number($('#watchInterval').value) || 30,
      });
      toast($('#watchOn').checked ? '✓ 自动记录已开启' : '自动记录已关闭');
    } catch (e) { toast(e.message, 4000); }
  };
  $('#btnSaveIgnore').onclick = async () => {
    try {
      const lines = $('#ignoreBox').value.split('\n').map((x) => x.trim()).filter(Boolean);
      await post('/api/ignore', { path: S.path, ignore: lines });
      toast('✓ 忽略规则已保存');
      await selectProject(S.path, true);
    } catch (e) { toast(e.message, 4000); }
  };
  $('#btnOpenFolder').onclick = () => openProjectFolder(S.path);
  $('#btnOpenProject').onclick = () => openProjectFolder(S.path);
  $('#btnRemove').onclick = () => removeProject(S.path);
  window.addEventListener('hashchange', () => bootFromHash());
}

function hashPath() {
  const m = /#p=([^&]+)/.exec(location.hash);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]);
  } catch (e) {
    return m[1];          // 地址栏被手工改坏时不要让整页崩掉
  }
}

function bootFromHash() {
  const path = hashPath();
  if (path && path !== S.path) selectProject(path, true);
}

(async function boot() {
  bind();
  bindHotkeys();
  loadSyncPrefs();
  $('#uiToken').value = S.token;
  const q = new URLSearchParams(location.search);
  const qt = q.get('token');
  if (qt) {                       // 支持别人发来带令牌的链接
    S.token = qt;
    localStorage.setItem('zgr_token', qt);
    $('#uiToken').value = qt;
  }
  try {
    await loadProjects();
  } catch (e) {
    toast('服务未响应：' + e.message, 6000);
    return;
  }
  // 顺带看看局域网里另一台电脑有没有新建项目（对方没开机就悄悄算了，不打扰）
  loadPeerProjects(true);
  const path = hashPath();
  if (path) return selectProject(path, true);
  const first = S.projects.find((p) => p.is_repo);
  if (first) return selectProject(first.path);
  if (!S.projects.length) {
    $('#timeline').innerHTML = '<div class="empty">还没有项目<br><button class="primary" onclick="document.getElementById(\'btnNew\').click()">新建第一个项目</button></div>';
  }
})();

/* 3D 弹窗里的「看图」按钮要能切回二维缩略图对比 */
window.showThumbCompare = showThumbCompare;
