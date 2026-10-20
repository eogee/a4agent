/* i18n 覆盖率自检：
 *   1) 从 frontend 源码里抽出「中文文案片段」（字符串字面量 + HTML 文本 + 常见属性）；
 *   2) 在沙箱里加载 frontend/js/i18n.js，逐条问它译成英文后是否还是中文；
 *   3) 打印漏译清单，退出码非 0 便于挂到 CI。
 *
 * 用法： node tools/i18n-check.mjs
 * 说明：源码里「文案 + 变量」拼出来的整句，抽出的是碎片，必然报未命中；
 *       这类以 PATTERNS 通配规则兜底，最终以运行时 window.i18n.audit() 为准。
 */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const FILES = [
  'frontend/index.html',
  'frontend/js/app.js',
  'frontend/js/llama.js',
  'frontend/js/tasks.js',
  'frontend/js/phone.js',
  'frontend/js/dirpicker.js',
];
const CJK = /[\u3400-\u9fff\uf900-\ufaff]/;

/* ---------- 抽取 ---------- */
function prevCode(src, i) {
  while (i >= 0 && /\s/.test(src[i])) i--;
  return i >= 0 ? src[i] : '';
}

function nextCode(src, i) {
  while (i < src.length && /\s/.test(src[i])) i++;
  return i < src.length ? src[i] : '';
}

function* literals(src) {
  let i = 0;
  const n = src.length;
  while (i < n) {
    const c = src[i];
    if (c === '/' && src[i + 1] === '/') { while (i < n && src[i] !== '\n') i++; continue; }
    if (c === '/' && src[i + 1] === '*') {
      i += 2;
      while (i + 1 < n && !(src[i] === '*' && src[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      const q = c;
      const before = prevCode(src, i - 1);
      i++;
      const buf = [];
      while (i < n) {
        const ch = src[i];
        if (ch === '\\') { buf.push(src.slice(i, i + 2)); i += 2; continue; }
        if (ch === q) { i++; break; }
        if (ch === '\n' && q !== '`') break;
        buf.push(ch);
        i++;
      }
      // 与 '+' 相邻的字面量是「文案 + 变量」的碎片，交给运行时通配规则处理
      const after = nextCode(src, i);
      const concat = before === '+' || after === '+';
      yield { text: buf.join(''), concat };
      continue;
    }
    i++;
  }
}

const ATTR = /\b(?:title|placeholder|alt|aria-label|data-tip)\s*=\s*"([^"]*)"/g;

function segments(lit) {
  const out = [];
  for (const m of lit.matchAll(ATTR)) if (CJK.test(m[1])) out.push(m[1]);
  const text = lit.replace(/<[^>]*>/g, '\n');
  for (const part of text.split('\n')) {
    const p = part.trim();
    if (CJK.test(p)) out.push(p);
  }
  return out;
}

function clean(seg) {
  return seg
    .replace(/\\\\/g, '\\')          // 源码里的转义反斜杠还原成运行时的单个
    .replace(/^["'>]+/, '')          // 抽取时带上的标签残渣
    .replace(/["']?$/, '')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/\s+/g, ' ')
    .trim();
}

/* ---------- 加载 i18n.js ---------- */
const noop = () => {};
const ctx = {
  console,
  setTimeout,
  navigator: { language: 'en-US' },
  localStorage: { getItem: () => 'en', setItem: noop },
  MutationObserver: null,
  requestAnimationFrame: (f) => f(),
  document: {
    readyState: 'complete',
    documentElement: { classList: { add: noop, remove: noop }, setAttribute: noop },
    addEventListener: noop,
    body: null,
    getElementById: () => null,
    createTreeWalker: () => ({ nextNode: () => null }),
    querySelectorAll: () => [],
  },
  NodeFilter: { SHOW_TEXT: 4, SHOW_ELEMENT: 1, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 },
};
ctx.window = ctx;
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(root, 'frontend/js/i18n.js'), 'utf8'), ctx);
const t = ctx.i18n.t;

/* ---------- 比对 ---------- */
const IGNORE = /^(中|EN|Language \/ 语言|中文)$/;  // 语言开关本身固定双语，不参与翻译
const seen = new Set();
const missing = [];
let skipped = 0;
for (const f of FILES) {
  const src = fs.readFileSync(path.join(root, f), 'utf8');
  for (const lit of literals(src)) {
    if (lit.concat) { skipped++; continue; }
    for (const raw of segments(lit.text)) {
      const seg = clean(raw);
      if (!seg || !CJK.test(seg) || IGNORE.test(seg) || seen.has(seg)) continue;
      seen.add(seg);
      const out = t(seg);
      if (out === seg) missing.push(`${f}: ${seg}`);
    }
  }
}

console.log(`检查 ${seen.size} 条独立文案（另有 ${skipped} 处拼接碎片由通配规则兜底），未命中字典 ${missing.length} 条`);
for (const m of missing) console.log('  -', m);
process.exitCode = missing.length ? 1 : 0;
