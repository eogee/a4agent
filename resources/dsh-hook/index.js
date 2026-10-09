// a4agent-dsh-hook — DSH 的 a4agent 通知插件
//
// DSH 没有 Claude Code 那套「外部 hook 命令 + stdin JSON」协议，hook 必须
// 跑在 dsh 进程内。所以本插件是一个 Cordis 插件，被写进
// ~/.dsh/profiles/*/cordis.patch.yml 后由 dsh 启动时加载：
//
//   session/event    turn/end(completed)  → 任务完成通知
//   tools/execute    ask_user_question    → 手机点选/文字作答
//   approval/request 权限审批              → 手机 Approve/Deny
//
// 交互一律回调 a4agent 常驻服务的本地 HTTP 接口（配置、ntfy 推送、作答等待
// 都在 Python 侧），本插件不直连 ntfy、不读 a4phone 配置——手机端订阅的始终
// 是 a4agent 自己的话题。
//
// 长轮询语义：提问/审批会把 HTTP 请求挂住，直到用户作答或 a4agent 超时。
// 任何异常（a4agent 未启动、超时、推送失败）都返回 null，由插件放行 DSH 原生
// 交互，绝不把 DSH 卡死。

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

const name = 'a4agent-dsh-hook';

// 监听 tools/execute 与 approval/request 需要注入宿主服务；
// session/event 直接 ctx.on 即可（与 dsh-session-telemetry 一致）。
const inject = ['tools', 'approval'];

// 记录每个会话最近的 AI 文本输出，供任务完成时随推送带出。
const lastAssistantOutput = new Map();

// a4agent 实例端口在桌面进程启动时写入 desktop.json；端口每次运行都可能变，
// 因此不缓存，失败时重读（长轮询期间用户可能刚启动 a4agent）。
// 候选顺序对齐 Python 侧 get_data_dir()：A4AGENT_DATA_DIR 覆盖 → 打包态
// %APPDATA%\a4agent → 开发态 <repo>/backend/database。
function findA4agentBase() {
  const candidates = [];
  if (process.env.A4AGENT_DESKTOP_URL) candidates.push(process.env.A4AGENT_DESKTOP_URL);
  if (process.env.A4AGENT_DATA_DIR) candidates.push(join(process.env.A4AGENT_DATA_DIR, 'desktop.json'));
  const appData = process.env.APPDATA;
  if (appData) candidates.push(join(appData, 'a4agent', 'desktop.json'));
  candidates.push(join(homedir(), '.a4agent', 'desktop.json'));
  // 开发态（从源码跑 a4agent）：backend/app/database.py 的同级目录
  candidates.push(join(import.meta.dirname, '..', '..', 'backend', 'database', 'desktop.json'));

  for (const file of candidates) {
    // 先按显式 URL 处理。必须用 startsWith 判定：`new URL('C:\\...')` 在
    // Windows 上不抛错，而是返回 origin === 'null'，用 try/catch 拦不住，
    // 会把字符串 "null" 当成 base 拼出 "null/api/v1/..."。
    if (/^https?:\/\//i.test(file)) {
      try {
        return new URL(file).origin;
      } catch { /* 非法 URL，继续按文件路径处理 */ }
      continue;
    }
    try {
      const { port } = JSON.parse(readFileSync(file, 'utf-8'));
      if (port) return `http://127.0.0.1:${port}`;
    } catch { /* 读不到就试下一个 */ }
  }
  return null;
}

/**
 * 调用 a4agent 本地接口。失败一律吞掉返回 null——插件的任何异常都不能
 * 让 DSH 会话失败。
 */
async function callA4agent(path, body, timeoutMs) {
  const base = findA4agentBase();
  if (!base) {
    process.stderr.write(
      '[a4agent-dsh-hook] 未找到 a4agent 运行实例（desktop.json），跳过通知；' +
      '请先启动 a4agent 桌面端\n');
    return null;
  }
  try {
    return await fetch(`${base}/api/v1/phone/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
      signal: AbortSignal.timeout(timeoutMs),
    }).then((r) => (r.ok ? r.json() : null));
  } catch (error) {
    const reason = error?.name === 'TimeoutError'
      ? `请求超时（${Math.round(timeoutMs / 1000)}s）`
      : String(error?.message || error);
    process.stderr.write(`[a4agent-dsh-hook] 调用 a4agent ${path} 失败: ${reason}\n`);
    return null;
  }
}

// a4agent 侧 hook.timeout 默认 60s、plan_timeout 300s；这里留足余量，
// 客户端超时必须大于服务端等待，否则先断开的一方白等。
const DEFAULT_TIMEOUT_MS = 75_000;

/** 任务完成：桌面弹窗 + 手机推送（含 AI 最后输出）。不阻塞。 */
async function handleTaskComplete(session, turn) {
  const shortId = session?.id ? session.id.slice(0, 8) : '—';
  await callA4agent('dsh/task-complete', {
    sessionId: session?.id ?? null,
    turn: turn ?? null,
    lastOutput: session?.id ? (lastAssistantOutput.get(session.id) ?? '') : '',
  }, 10_000);
  process.stderr.write(`[a4agent-dsh-hook] 任务完成 ${shortId} turn=${turn}\n`);
}

/**
 * 提问作答：拦截 ask_user_question，交给 a4agent 推手机并等待作答。
 * @returns {Promise<{answers: Array<object>} | null>} 手机已作答返回契约对象；
 *   超时/未启用/失败返回 null，交由 DSH 走原生提问。
 */
async function handleAskUserQuestion(exec) {
  const args = exec.arguments || {};
  const questions = Array.isArray(args.questions) ? args.questions : [];
  if (!questions.length) return null;

  const result = await callA4agent(
    'dsh/question',
    { questions },
    DEFAULT_TIMEOUT_MS,
  );
  return result && Array.isArray(result.answers) && result.answers.length
    ? result
    : null;
}

/**
 * 权限审批：拦截 approval/request，交给 a4agent 推手机。
 * @returns {Promise<string | null>} 'allowed-once' | 'rejected'；
 *   超时/未启用/失败返回 null，交由 DSH 走原生审批。
 */
async function handleApprovalRequest(req) {
  const outcome = await callA4agent(
    'dsh/permission',
    { toolName: req?.toolName ?? null, reason: req?.reason ?? null },
    DEFAULT_TIMEOUT_MS,
  );
  return outcome && typeof outcome.outcome === 'string' ? outcome.outcome : null;
}

/**
 * 插件入口。
 * @param ctx cordis 插件上下文
 * @param config 插件配置 { phone?, taskComplete?, questionAsked?, permissionRequest? }
 */
function apply(ctx, config = {}) {
  const phoneEnabled = config.phone !== false;
  const enableTaskComplete = config.taskComplete !== false;
  const enableQuestionAsked = config.questionAsked !== false;
  const enablePermissionRequest = config.permissionRequest !== false;

  // ── 会话事件流：任务完成 + 缓存 AI 最后输出 ──────────────────────────
  if (enableTaskComplete) {
    ctx.on('session/event', (session, event) => {
      const { type, data } = event ?? {};

      if (type === 'assistant/message') {
        const message = data?.message;
        const texts = (message?.content ?? [])
          .filter((b) => b?.type === 'text' && b.text)
          .map((b) => b.text);
        if (texts.length && session?.id) {
          lastAssistantOutput.set(session.id, texts.join('\n'));
        }
        return;
      }

      // subagent 轮次结束同样触发 turn/end，一并推送会刷屏：只推顶层会话。
      if (type === 'turn/end' && data?.reason?.kind === 'completed'
          && !session?.header?.parentSession) {
        handleTaskComplete(session, data.turn).catch((error) =>
          ctx.logger.warn(`a4agent-dsh-hook: 任务完成通知失败: ${String(error)}`));
      }
    });
  }

  // ── 拦截 ask_user_question → 手机作答 ───────────────────────────────
  // tools/execute 是 around-dispatch waterfall：返回自定义结果替换原生提问，
  // 调用 next() 放行原生交互。
  if (enableQuestionAsked && phoneEnabled) {
    ctx.on('tools/execute', async (exec, next) => {
      if (exec.name !== 'ask_user_question') return next();
      try {
        const answers = await handleAskUserQuestion(exec);
        if (!answers) return next();
        return {
          isError: false,
          value: answers,
          content: [{ type: 'text', text: JSON.stringify(answers) }],
        };
      } catch (error) {
        ctx.logger.warn(`a4agent-dsh-hook: 提问处理失败，回退原生: ${String(error)}`);
        return next();
      }
    });
  }

  // ── 拦截 approval/request → 手机审批 ────────────────────────────────
  if (enablePermissionRequest && phoneEnabled) {
    ctx.on('approval/request', async (req, next) => {
      try {
        const outcome = await handleApprovalRequest(req);
        if (!outcome) return next();
        return outcome;
      } catch (error) {
        ctx.logger.warn(`a4agent-dsh-hook: 审批处理失败，回退原生: ${String(error)}`);
        return next();
      }
    });
  }

  ctx.logger.info('a4agent-dsh-hook: 已挂载 hook（task-complete / question-asked / permission-request）');
  return () => {};
}

export { apply, inject, name };
