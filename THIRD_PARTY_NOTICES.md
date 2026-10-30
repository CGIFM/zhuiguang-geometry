# 第三方组件与许可

本项目本体为 MIT。下面这些组件随仓库或随发布物一起分发，
各自的许可在此列出。**LibreDWG 不在其中**——它是 GPL-3.0，
和 MIT 不同，所以做成可选下载（见 `tools/fetch_libredwg.py`）。

| 组件 | 位置 | 许可 | 说明 |
|---|---|---|---|
| three.js | `web/vendor/three/` | MIT | 三维渲染 |
| occt-import-js / OpenCascade | `web/vendor/occt/` | LGPL-2.1（occt-import-js 包装为 MIT） | STEP / IGES 解析（WASM） |
| dxf-viewer | `web/vendor/dxf-viewer/` | MIT | DXF 渲染 |
| marked | `web/vendor/deps/marked.esm.js` | MIT | Markdown 渲染 |
| DOMPurify | `web/vendor/deps/purify.es.mjs` | Apache-2.0 / MPL-2.0 | HTML 净化 |
| opentype.js | `web/vendor/deps/opentype.module.js` | MIT | 字体解析 |
| earcut | `web/vendor/deps/earcut.min.js` | ISC | 三角化 |
| loglevel | `web/vendor/deps/loglevel.min.js` | MIT | 日志 |
| Microsoft WebView2 SDK | `desktop/vendor/` | Microsoft 可再分发条款 | 仅用于桌面壳 |

## 关于 LibreDWG（GPL-3.0）

`dwg2dxf` 只在你想预览 DWG 时需要。注意：**把它放进你的发布物并一起分发后，
你的发布物整体要遵守 GPL-3.0**。只在本机内部使用则不受影响。
