#!/usr/bin/env node
/**
 * a4agent 更新清单生成 + 双平台 Release 发布工具（Gitee / GitHub）。
 *
 * 单入口：对「已配置 token」的平台统一执行
 *   ensure release → 删除同名旧资产 → 上传安装包 → 上传 latest.json（Ed25519 签名）→ 更新发布说明。
 * 未配置 token 的平台自动跳过。
 *
 * 用法：
 *   node tools/update-manifest.js --installer installer/out/a4agent-Lite-setup-v0.2.1.exe \
 *        --version 0.2.1 [--body-file installer/out/RELEASE-NOTES-v0.2.1.md] [--prerelease]
 *
 * Token 来源（优先级从高到低）：
 *   环境变量 A4AGENT_GITEE_TOKEN / A4AGENT_GITHUB_TOKEN
 *   或令牌文件：--token-file 或默认 ../a4api/.claude/"git release token.txt"（每行 <token> 平台名）
 *
 * 签名密钥：env A4AGENT_UPDATE_KEY_FILE 或 .claude/keys/update-signing.pem（相对仓库根）。
 * 缺私钥时只生成未签名清单会直接报错退出——未签名的清单客户端一律拒收，没有意义。
 *
 * 载荷算法与 src/Core/Update/Updater.cs 的 BuildPayload 严格一致（固定字段顺序 +
 * 4 字节大端长度前缀，命名空间 a4agent-update）；URL 不入签名，两份清单字节一致、共用同一签名。
 * 无第三方依赖（Node 18+，原生 fetch / FormData）。
 */
'use strict';

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const REPO = 'eogee/a4agent';
const GH_API_VERSION = '2022-11-28';
const NAMESPACE = 'a4agent-update';
const PAYLOAD_VERSION = '1';
const SCHEMA_VERSION = '1';
const INSTALLER_PREFIX = 'a4agent-Lite-setup-v';
const ROOT = path.resolve(__dirname, '..');

/* ---------------- 清单生成与签名 ---------------- */

function sha256Of(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex').toLowerCase();
}

function buildPayload(manifest) {
  const chunks = [];
  const push = (s) => {
    const data = Buffer.from(String(s), 'utf-8');
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length, 0);
    chunks.push(len, data);
  };
  const assets = [...(manifest.assets || [])].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  push(NAMESPACE);
  push(PAYLOAD_VERSION);
  push(String(manifest.schema_version));
  push(String(manifest.version));
  push(String(manifest.min_version));
  push(String(manifest.published_at));
  push(manifest.prerelease ? '1' : '0');
  push(String(manifest.notes));
  push(String(manifest.notes_url));
  for (const a of assets) {
    push(String(a.name));
    push(String(a.sha256).toLowerCase());
    push(String(a.size));
  }
  return Buffer.concat(chunks);
}

function signPayload(payload, keyFile) {
  const key = crypto.createPrivateKey(fs.readFileSync(keyFile));
  return crypto.sign(null, payload, key).toString('base64');
}

function buildManifestText({ version, body, prerelease, installer, keyFile }) {
  const assetName = path.basename(installer);
  const expected = `${INSTALLER_PREFIX}${version}.exe`;
  if (assetName !== expected)
    console.warn(`⚠ 安装包名 ${assetName} 与客户端预期 ${expected} 不一致——客户端将找不到更新资产！`);
  const sha = sha256Of(installer);
  const size = fs.statSync(installer).size;
  const tag = `v${version}`;
  const manifest = {
    schema_version: SCHEMA_VERSION,
    version,
    min_version: '0.0.0',
    prerelease: !!prerelease,
    published_at: new Date().toISOString(),
    notes: (body || '').trim(),
    notes_url: `https://github.com/${REPO}/releases/tag/${tag}`,
    assets: [
      { name: assetName, size, sha256: sha, url: `https://github.com/${REPO}/releases/download/${tag}/${assetName}` },
      { name: assetName, size, sha256: sha, url: `https://gitee.com/${REPO}/releases/download/${tag}/${assetName}` },
    ],
  };
  manifest.signature = signPayload(buildPayload(manifest), keyFile);
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

/* ---------------- Token 解析 ---------------- */

function loadTokens(tokenFile) {
  const candidates = [
    process.env.A4AGENT_TOKEN_FILE,
    tokenFile,
    path.join(path.dirname(ROOT), 'a4api', '.claude', 'git release token.txt'),
  ].filter(Boolean);
  const file = candidates.find((f) => fs.existsSync(f));
  const out = {};
  if (file) {
    for (const line of fs.readFileSync(file, 'utf-8').split(/\r?\n/)) {
      const cols = line.trim().split(/\s+/);
      if (cols.length >= 2) out[cols[cols.length - 1].toLowerCase()] = cols[0];
    }
  }
  return {
    gitee: process.env.A4AGENT_GITEE_TOKEN || out.gitee,
    github: process.env.A4AGENT_GITHUB_TOKEN || out.github,
  };
}

/* ---------------- HTTP 与平台适配（移植自 a4api release.js，去掉 GitCode） ---------------- */

function errBody(data) {
  if (data == null) return '';
  if (typeof data === 'string') return data;
  return JSON.stringify(data);
}

async function http(url, { method = 'GET', headers = {}, body, form, raw } = {}) {
  const opts = { method, headers: { ...headers } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json;charset=UTF-8';
    opts.body = JSON.stringify(body);
  } else if (form !== undefined) {
    opts.body = form;
  } else if (raw !== undefined) {
    opts.body = raw;
  }
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch { data = text; } }
  return { status: resp.status, ok: resp.ok, data };
}

const gitee = {
  name: 'Gitee',
  base: 'https://gitee.com/api/v5',
  async api(token, p, { method = 'GET', body, form } = {}) {
    const sep = p.includes('?') ? '&' : '?';
    const { status, ok, data } = await http(`${this.base}/${p}${sep}access_token=${encodeURIComponent(token)}`,
      { method, body, form });
    if (!ok) throw new Error(`Gitee ${method} ${p} -> HTTP ${status}: ${errBody(data).slice(0, 300)}`);
    return data;
  },
  async ensureRelease(ctx) {
    const { token, repo, tag } = ctx;
    let rid = null;
    try { const d = await this.api(token, `repos/${repo}/releases/tags/${tag}`); if (d && d.id) rid = d.id; } catch { }
    if (!rid) {
      const list = await this.api(token, `repos/${repo}/releases?per_page=100`) || [];
      const hit = list.find((r) => r.tag_name === tag);
      if (hit && hit.id) rid = hit.id;
    }
    if (!rid) {
      const created = await this.api(token, `repos/${repo}/releases`, {
        method: 'POST',
        body: { tag_name: tag, name: ctx.name, body: ctx.body || '', prerelease: !!ctx.prerelease, target_commitish: 'master' },
      });
      rid = created.id;
    }
    return rid;
  },
  async deleteSameName(ctx, assetName) {
    const files = await this.api(ctx.token, `repos/${ctx.repo}/releases/${ctx.rid}/attach_files`) || [];
    for (const f of files) {
      if (f.name === assetName) {
        await this.api(ctx.token, `repos/${ctx.repo}/releases/${ctx.rid}/attach_files/${f.id}`, { method: 'DELETE' });
        console.log(`      已删除旧资产 ${f.name} (id=${f.id})`);
      }
    }
  },
  async upload(ctx, assetPath, assetName) {
    const form = new FormData();
    form.append('file', new Blob([fs.readFileSync(assetPath)]), assetName);
    return this.api(ctx.token,
      `repos/${ctx.repo}/releases/${ctx.rid}/attach_files?name=${encodeURIComponent(assetName)}`,
      { method: 'POST', form });
  },
  async patchBody(ctx) {
    // Gitee PATCH 强制 tag_name / name / body 三字段齐全
    return this.api(ctx.token, `repos/${ctx.repo}/releases/${ctx.rid}`, {
      method: 'PATCH',
      body: { tag_name: ctx.tag, name: ctx.name, body: ctx.body || '', prerelease: !!ctx.prerelease },
    });
  },
};

const github = {
  name: 'GitHub',
  base: 'https://api.github.com',
  uploadBase: 'https://uploads.github.com',
  headers(token) {
    return {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': GH_API_VERSION,
    };
  },
  async api(token, p, { method = 'GET', body, raw, rawType } = {}) {
    const url = p.startsWith('http') ? p : `${this.base}${p}`;
    const headers = this.headers(token);
    if (raw !== undefined) { headers['Content-Type'] = rawType; headers['Content-Length'] = String(raw.length); }
    const { status, ok, data } = await http(url, { method, headers, body, raw });
    if (!ok) throw new Error(`GitHub ${method} ${p} -> HTTP ${status}: ${errBody(data).slice(0, 300)}`);
    return data;
  },
  async ensureRelease(ctx) {
    const { token, repo, tag } = ctx;
    let rid = null;
    try { const d = await this.api(token, `/repos/${repo}/releases/tags/${tag}`); if (d && d.id) rid = d.id; } catch { }
    if (!rid) {
      const created = await this.api(token, `/repos/${repo}/releases`, {
        method: 'POST',
        body: { tag_name: tag, name: ctx.name, body: ctx.body || '', prerelease: !!ctx.prerelease, draft: false },
      });
      rid = created.id;
    }
    return rid;
  },
  async deleteSameName(ctx, assetName) {
    const rel = await this.api(ctx.token, `/repos/${ctx.repo}/releases/${ctx.rid}`);
    for (const a of (rel.assets || [])) {
      if (a.name === assetName) {
        await this.api(ctx.token, `/repos/${ctx.repo}/releases/assets/${a.id}`, { method: 'DELETE' });
        console.log(`      已删除旧资产 ${a.name} (id=${a.id})`);
      }
    }
  },
  async upload(ctx, assetPath, assetName) {
    const url = `${this.uploadBase}/repos/${ctx.repo}/releases/${ctx.rid}/assets?name=${encodeURIComponent(assetName)}`;
    return this.api(ctx.token, url, { method: 'POST', raw: fs.readFileSync(assetPath), rawType: 'application/octet-stream' });
  },
  async patchBody(ctx) {
    return this.api(ctx.token, `/repos/${ctx.repo}/releases/${ctx.rid}`, {
      method: 'PATCH',
      body: { tag_name: ctx.tag, name: ctx.name, body: ctx.body || '', prerelease: !!ctx.prerelease },
    });
  },
};

/* ---------------- 编排 ---------------- */

function parseArgs(argv) {
  const opts = {};
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--installer': opts.installer = argv[++i]; break;
      case '--version': opts.version = argv[++i]; break;
      case '--body-file': opts.bodyFile = argv[++i]; break;
      case '--token-file': opts.tokenFile = argv[++i]; break;
      case '--key-file': opts.keyFile = argv[++i]; break;
      case '--manifest-out': opts.manifestOut = argv[++i]; break;
      case '--prerelease': opts.prerelease = true; break;
      case '--manifest-only': opts.manifestOnly = true; break;
      default: console.error(`未知参数：${argv[i]}`); process.exit(2);
    }
  }
  if (!opts.installer || !opts.version) {
    console.error('用法：node tools/update-manifest.js --installer <安装包> --version <x.y.z> [--body-file <md>] [--prerelease] [--manifest-only] [--token-file <f>] [--key-file <f>]');
    process.exit(2);
  }
  return opts;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));  const installer = path.resolve(opts.installer);
  if (!fs.existsSync(installer)) { console.error(`找不到安装包：${installer}`); process.exit(1); }

  const keyFile = opts.keyFile || process.env.A4AGENT_UPDATE_KEY_FILE
    || path.join(ROOT, '.claude', 'keys', 'update-signing.pem');
  if (!fs.existsSync(keyFile)) {
    console.error(`✗ 未找到更新签名私钥：${keyFile}`);
    console.error('  生成：openssl genpkey -algorithm ed25519 -out .claude/keys/update-signing.pem');
    console.error('  未签名的清单客户端一律拒收，故不继续发布。');
    process.exit(1);
  }

  const body = opts.bodyFile ? fs.readFileSync(path.resolve(opts.bodyFile), 'utf-8') : '';
  const manifestText = buildManifestText({ version: opts.version, body, prerelease: opts.prerelease, installer, keyFile });
  const manifestPath = opts.manifestOut || path.join(path.dirname(installer), 'latest.json');
  fs.writeFileSync(manifestPath, manifestText, 'utf-8');
  console.log(`latest.json 已生成：${manifestPath}（签名密钥 ${path.basename(keyFile)}）`);
  if (opts.manifestOnly) return;

  const tokens = loadTokens(opts.tokenFile);
  const tag = `v${opts.version}`;
  const releaseName = `${tag} ${body.split('\n').find((l) => l.trim() && !l.startsWith('#') && !l.startsWith('|'))?.trim() || ''}`.trim();
  const assetName = path.basename(installer);

  for (const [platform, token] of [['gitee', tokens.gitee], ['github', tokens.github]]) {
    if (!token) { console.log(`\n>>> ${platform}：未配置 token，跳过`); continue; }
    const impl = platform === 'gitee' ? gitee : github;
    console.log(`\n>>> ${platform} <<<`);
    try {
      const ctx = { token, repo: REPO, tag, name: releaseName || tag, body, prerelease: !!opts.prerelease, rid: null };
      console.log('  [1/5] 查找/创建 Release ...');
      ctx.rid = await impl.ensureRelease(ctx);
      console.log(`  [2/5] 删除同名旧资产 ...`);
      await impl.deleteSameName(ctx, assetName);
      await impl.deleteSameName(ctx, 'latest.json');
      console.log(`  [3/5] 上传 ${assetName} ...`);
      await impl.upload(ctx, installer, assetName);
      console.log(`  [4/5] 上传 latest.json ...`);
      await impl.upload(ctx, manifestPath, 'latest.json');
      console.log(`  [5/5] 更新发布说明 ...`);
      await impl.patchBody(ctx);
      console.log(`  ✔ ${platform} 发布完成`);
    } catch (e) {
      console.error(`  ✗ ${platform} 失败：${e.message}`);
      process.exitCode = 1;
    }
  }
}

if (require.main === module) {
  main().catch((e) => { console.error(e); process.exit(1); });
}

// 供 Smoke --updatetest 跨语言互验：node 签名 → C# 验签
module.exports = { buildPayload, signPayload, buildManifestText };
