// DSH 通知链路联调：真起 a4agent 服务 + 假 ntfy，跑通插件↔后端全流程。
//
// 用法：
//   node tools/verify_dsh_hook.mjs            # 跑全部用例
//   node tools/verify_dsh_hook.mjs passthru   # 只跑「终端优先」放行
//   node tools/verify_dsh_hook.mjs outmode    # 只跑「外出模式」闭环
//   node tools/verify_dsh_hook.mjs timeout    // 只跑「超时降级」
//
// 覆盖：
//   passthru 终端优先：三个 hook 都放行原生，且真的往返到了后端
//   outmode  外出模式：提问被拦截、手机作答后返回 DSH 契约；审批映射 outcome
//   timeout  手机不回复：到点放行原生，绝不把 DSH 卡死
//
// 依赖：node + 项目 .venv（脚本用 ./.venv/Scripts/python.exe 起服务）。
// 假 ntfy 实现了真实协议的两个面：POST / 发布、GET /{topic}-response/json
// 轮询订阅（与 backend/app/hooks/response.py 对齐），因此不碰真实 ntfy。

import { spawn } from 'node:child_process';
import { mkdtempSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const net = require('node:net');

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const HOOK = join(ROOT, 'resources', 'dsh-hook', 'index.js');

let failures = 0;
function check(name, ok, detail = '') {
  console.log(`  ${ok ? '通过' : '失败'}  ${name}${detail ? '  — ' + detail : ''}`);
  if (!ok) failures++;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
    srv.on('error', reject);
  });
}

/**
 * 起一套隔离环境：假 ntfy + 真 a4agent 后端 + 已加载插件的 Cordis 上下文。
 * mode: 'home' 终端优先 | 'out' 外出模式；timeout 单位秒（会被钳到 >=15）。
 * @param {{mode: string, timeout?: number, silentNtfy?: boolean}} opts
 */
async function setup({ mode, timeout = 15, silentNtfy = false }) {
  const dataDir = join(mkdtempSync(join(tmpdir(), 'a4ag-dsh-')), 'a4ag');
  mkdirSync(dataDir, { recursive: true });
  // 插件与后端进程必须指向同一数据目录：插件靠它发现 desktop.json 里的端口
  process.env.A4AGENT_DATA_DIR = dataDir;

  // 假 ntfy
  let lastPush = null;
  const sent = [];
  const ntfy = createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      if (req.method === 'POST' && url.pathname.endsWith('-response')) {
        sent.push({ event: 'message', message: body, ts: Math.floor(Date.now() / 1000) });
        res.writeHead(200); res.end('OK');
        return;
      }
      if (req.method === 'GET' && url.pathname.endsWith('-response/json')) {
        const since = parseInt(url.searchParams.get('since') || '0', 10);
        const lines = sent.filter((m) => (m.ts || 0) >= since).map((m) => JSON.stringify(m));
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(lines.join('\n') + '\n');
        return;
      }
      if (req.method === 'POST' && url.pathname === '/' && !silentNtfy) {
        lastPush = JSON.parse(body || '{}');
      }
      res.writeHead(200); res.end('OK');
    });
  });
  await new Promise((r) => ntfy.listen(0, '127.0.0.1', r));
  const ntfyPort = ntfy.address().port;

  writeFileSync(join(dataDir, 'phone_config.json'), JSON.stringify({
    enabled: true, server: `http://127.0.0.1:${ntfyPort}`, topic: 'a4ag-dsh', token: '',
    events: { success: true, failed: true, timeout: true, cancelled: false },
    hook: { mode, timeout, plan_timeout: 300 }, desktop: false,
  }), 'utf-8');

  const port = await freePort();
  writeFileSync(join(dataDir, 'desktop.json'),
    JSON.stringify({ port, pid: process.pid }), 'utf-8');

  const srv = spawn(join(ROOT, '.venv', 'Scripts', 'python.exe'), ['-c', `
import uvicorn
from backend.app.main import app
uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=${port}, log_level='error')).run()
`], { cwd: ROOT, env: { ...process.env, A4AGENT_DATA_DIR: dataDir }, stdio: 'ignore' });

  for (let i = 0; i < 120; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/v1/phone/hook/status`);
      if (r.ok) break;
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 250));
  }

  const { apply } = await import(`file:///${HOOK.replace(/\\/g, '/')}`);
  const listeners = [];
  apply({
    logger: { info: () => {}, warn: (m) => console.log('    [warn]', m) },
    on: (e, f) => listeners.push([e, f]),
  }, {});
  const dispatch = (ev, ...a) => listeners.filter(([e]) => e === ev).map(([, f]) => f(...a));

  /** 持续自动作答：每道题/审批的推送到达后都回答（多题是逐题等待的）。 */
  const autoReply = (payload, ms) => {
    const seen = new Set();
    const timer = setInterval(() => {
      const act = (lastPush?.actions || [])[0];
      if (!act) return;
      const reqId = JSON.parse(act.body).requestId;
      if (seen.has(reqId)) return;
      seen.add(reqId);
      fetch(`http://127.0.0.1:${ntfyPort}/a4ag-dsh-response`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requestId: reqId, ...payload }),
      }).catch(() => {});
    }, 200);
    setTimeout(() => clearInterval(timer), ms);
    return () => clearInterval(timer);
  };

  const close = () => { srv.kill(); ntfy.close(); };
  return { dispatch, autoReply, close, get lastPush() { return lastPush; } };
}

// ---------------- 用例 ----------------

async function casePassthru() {
  console.log('\n[终端优先] 三个 hook 都应放行原生（不阻塞终端）');
  const env = await setup({ mode: 'home' });
  try {
    let n1 = false;
    const r1 = await (await env.dispatch('tools/execute',
      { name: 'ask_user_question', arguments: { questions: [{ id: 'q1', question: '继续？', options: [{ label: 'A' }] }] } },
      async () => { n1 = true; return 'NEXT'; }))[0];
    check('ask_user_question 放行', n1 === true && r1 === 'NEXT', `next=${n1}`);

    let n2 = false;
    await (await env.dispatch('approval/request', { toolName: 'Bash', reason: 'ls' },
      async () => { n2 = true; return 'NEXT'; }))[0];
    check('approval/request 放行', n2 === true);

    let n3 = false;
    const r3 = await (await env.dispatch('tools/execute', { name: 'read_file', arguments: {} },
      async () => { n3 = true; return 'NEXT'; }))[0];
    check('无关工具透传', n3 === true && r3 === 'NEXT');
    check('终端优先不推送手机', env.lastPush === null);
  } finally { env.close(); }
}

async function caseOutMode() {
  console.log('\n[外出模式] 提问/审批应被拦截并等手机作答');
  const env = await setup({ mode: 'out' });
  try {
    const stop = env.autoReply({ answer: 'A' }, 8000);
    const t0 = Date.now();
    const ask = await (await env.dispatch('tools/execute',
      { name: 'ask_user_question', arguments: { questions: [
          { id: 'q1', question: '选哪个？', header: '确认', options: [{ label: 'A' }, { label: 'B' }] },
          { id: 'q2', question: '再选', options: [{ label: 'C' }, { label: 'D' }] }] } },
      async () => 'NATIVE'))[0];
    stop();
    const answers = ask?.value?.answers;
    check('提问被拦截（未放行原生）', ask !== 'NATIVE');
    check('返回 DSH answers 契约', Array.isArray(answers) && answers.length === 2,
      JSON.stringify(answers));
    check('答案映射正确', answers?.[0]?.id === 'q1' && answers[0].selected?.[0] === 'A');
    check('响应及时（<8s）', (Date.now() - t0) / 1000 < 8, `${((Date.now() - t0) / 1000).toFixed(1)}s`);

    const stop2 = env.autoReply({ approved: false }, 8000);
    const appr = await (await env.dispatch('approval/request',
      { toolName: 'Bash', reason: '删除文件' }, async () => 'NATIVE'))[0];
    stop2();
    check('审批 Deny → rejected', appr === 'rejected', String(appr));
  } finally { env.close(); }
}

async function caseTimeout() {
  console.log('\n[超时降级] 手机不回复须放行原生，绝不卡死 DSH');
  const env = await setup({ mode: 'out', timeout: 15, silentNtfy: true });
  try {
    let t0 = Date.now();
    const r1 = await (await env.dispatch('tools/execute',
      { name: 'ask_user_question', arguments: { questions: [{ id: 'q1', question: '继续？', options: [{ label: 'A' }] }] } },
      async () => 'NATIVE'))[0];
    const dt1 = (Date.now() - t0) / 1000;
    check('提问超时后放行', r1 === 'NATIVE', `${dt1.toFixed(1)}s`);
    check('在 timeout+余量内返回', dt1 < 25, `${dt1.toFixed(1)}s`);

    t0 = Date.now();
    const r2 = await (await env.dispatch('approval/request', { toolName: 'Bash', reason: 'x' },
      async () => 'NATIVE'))[0];
    const dt2 = (Date.now() - t0) / 1000;
    check('审批超时后放行', r2 === 'NATIVE', `${dt2.toFixed(1)}s`);
  } finally { env.close(); }
}

const CASES = { passthru: casePassthru, outmode: caseOutMode, timeout: caseTimeout };
const pick = process.argv[2];
const run = pick ? { [pick]: CASES[pick] } : CASES;
if (pick && !CASES[pick]) {
  console.error(`未知用例：${pick}（可选：${Object.keys(CASES).join(' / ')}）`);
  process.exit(2);
}

for (const [name, fn] of Object.entries(run)) {
  try { await fn(); } catch (e) {
    failures++;
    console.log(`  失败  ${name} 抛出异常：${e.message}`);
  }
}
console.log(failures ? `\n共 ${failures} 项失败` : '\n全部通过');
process.exit(failures ? 1 : 0);
