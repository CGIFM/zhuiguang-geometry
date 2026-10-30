/* earcut 的 UMD 包由 index.html 以普通脚本加载，这里只把全局转成模块默认导出 */
const e = globalThis.earcut;
export default (e && e.default) ? e.default : e;
