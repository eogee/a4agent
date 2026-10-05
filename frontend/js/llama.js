/* 本地模型（llama.cpp 推理控制台）—— 原 a4agent 功能合并。
 * 向导：获取引擎 → 检测硬件 → 模型目录 → 默认模型 → 端口；
 * 主界面：服务控制、模型库、推理设置、运行日志、接入信息。
 */
layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var element = layui.element;
  var API = '/api/v1/llama';

  var loaded = false;          // 首次点开本页时初始化
  var active = false;          // 本页是否处于前台（控制日志轮询）
  var pollTimer = null;        // 状态/日志轮询
  var lastLogId = 0;
  var lastStatus = null;

  /* ---------- 工具 ---------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function apiGet(path) {
    return fetch(API + path).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || '请求失败'); });
      return r.json();
    });
  }

  function apiSend(path, method, body) {
    return fetch(API + path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || '请求失败'); });
      return r.json();
    });
  }

  function fmtBytes(n) {
    if (!n || n <= 0) return '0 B';
    if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
    if (n >= 1024 * 1024) return Math.round(n / 1024 / 1024) + ' MB';
    return Math.round(n / 1024) + ' KB';
  }

  function stateBadge(state) {
    var map = {
      running: ['运行中', 'llama-badge-running'],
      starting: ['启动中', 'llama-badge-starting'],
      failed: ['失败', 'llama-badge-failed'],
      stopped: ['已停止', 'llama-badge-stopped']
    };
    var m = map[state] || map.stopped;
    return '<span class="llama-badge ' + m[1] + '">' + m[0] + '</span>';
  }

  function copyText(text, tip) {
    function done() { layer.msg(tip || '已复制', { icon: 1, time: 1200 }); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { fallback(); });
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
      document.body.removeChild(ta);
    }
  }

  /* ---------- 原生对话框（仅 pywebview 桌面环境） ----------
   * pickFolder()/pickFile() 返回 Promise：
   *   resolve(path)  用户选择了路径
   *   resolve(null)  用户取消
   *   resolve(undefined) 当前不是桌面环境（调用方退回手动输入）
   */
  function nativePick(method) {
    return new Promise(function (resolve) {
      var wv = window.pywebview;
      if (!wv) { resolve(undefined); return; }
      var call = function (api) {
        try {
          api[method]().then(function (p) { resolve(p || null); },
            function () { resolve(null); });
        } catch (e) { resolve(null); }
      };
      if (wv.api) { call(wv.api); return; }
      // 桥未就绪：等 pywebviewready，避免早点击无响应
      var onReady = function () {
        window.removeEventListener('pywebviewready', onReady);
        if (window.pywebview && window.pywebview.api) call(window.pywebview.api);
        else resolve(undefined);
      };
      window.addEventListener('pywebviewready', onReady);
    });
  }

  function pickFolder() { return nativePick('select_folder'); }
  function pickFile() { return nativePick('select_file'); }

  function isDesktop() { return !!window.pywebview; }

  /* 添加目录的统一逻辑：桌面环境优先弹原生对话框，浏览器环境读输入框 */
  function addModelDir(inputEl, onDone) {
    function submit(path) {
      apiSend('/models/dirs', 'POST', { path: path }).then(function (m) {
        if (inputEl) inputEl.value = '';
        onDone(m);
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    }
    if (isDesktop()) {
      pickFolder().then(function (p) {
        if (p) submit(p);
      });
    } else {
      var p = inputEl ? inputEl.value.trim() : '';
      if (!p) { layer.msg('请输入目录路径', { icon: 0 }); return; }
      submit(p);
    }
  }

  /* ---------- 入口：懒加载 ---------- */
  // 不能再用 element.on('tab(main-tab)')：layui 对同一 filter 是覆盖式注册，
  // 会把 app.js 的技能/MCP 懒加载监听器顶掉，这里改听 app.js 派发的自定义事件。
  window.addEventListener('main-tab-changed', function (e) {
    var isLlama = e.detail.index === 4;
    active = isLlama;
    if (isLlama && !loaded) {
      loaded = true;
      boot();
    }
    if (!isLlama && pollTimer) { stopPolling(); }
    if (isLlama && lastStatus) { startPolling(); }
  });

  function boot() {
    var box = document.getElementById('llama-content');
    apiGet('/status').then(function (s) {
      lastStatus = s;
      if (!s.wizard_done) {
        renderWizardEntry(box, s);
      } else {
        renderMain(box, s);
      }
    }).catch(function (e) {
      box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  /* ---------- 向导入口卡 ---------- */
  function renderWizardEntry(box, s) {
    box.innerHTML =
      '<div class="llama-welcome">' +
      '  <div class="llama-welcome-title">本地大模型推理控制台</div>' +
      '  <div class="llama-welcome-desc">把任意 .gguf 模型一键变成 OpenAI 兼容的本地 API 服务' +
      '（基于 llama.cpp llama-server）。首次使用请运行配置向导：' +
      '自动获取推理引擎、检测显卡、选择模型。</div>' +
      (s.engine_present ? '<div class="llama-welcome-hint">✔ 检测到本机已有推理引擎，向导中将自动复用</div>' : '') +
      '  <button class="layui-btn layui-btn-normal" id="llama-btn-wizard">运行首次配置向导</button>' +
      '</div>';
    document.getElementById('llama-btn-wizard').onclick = function () { openWizard(); };
  }

  /* ---------- 主界面 ---------- */
  function renderMain(box, s) {
    box.innerHTML =
      '<div class="llama-topline">' +
      '  <div class="llama-topline-left">' + stateBadge(s.state) +
      '    <span class="llama-topline-model" id="llama-top-model">' + escapeHtml(s.default_model_name || '未设置默认模型') + '</span>' +
      '    <span class="llama-topline-meta">端口 ' + s.port + ' · ' + (s.host === '0.0.0.0' ? '局域网开放' : '仅本机') + '</span>' +
      '    <span class="llama-topline-meta" id="llama-top-engine" title="' + escapeHtml(s.engine_dir) + '">引擎 ' + (s.engine_present ? '就绪' : '缺失') + '</span>' +
      '  </div>' +
      '  <div class="llama-topline-actions">' +
      '    <a href="javascript:;" class="update-link" id="llama-link-wizard">配置向导</a>' +
      '    <button class="layui-btn layui-btn-normal layui-btn-sm" id="llama-btn-start">启动服务</button>' +
      '    <button class="layui-btn layui-btn-primary layui-btn-sm" id="llama-btn-stop">停止</button>' +
      '  </div>' +
      '</div>' +
      '<div class="llama-progress" id="llama-progress" style="display:none">' +
      '  <div class="llama-progress-text" id="llama-progress-text"></div>' +
      '  <div class="llama-progress-bar"><div class="llama-progress-inner" id="llama-progress-inner"></div></div>' +
      '  <button class="layui-btn layui-btn-xs" id="llama-btn-dl-cancel">取消下载</button>' +
      '</div>' +
      '<div class="llama-grid">' +
      '  <div class="llama-card"><div class="llama-card-head">接入信息<button class="layui-btn layui-btn-normal layui-btn-xs" id="llama-btn-integrate">接入配置方案</button></div><div id="llama-connect"><div class="empty-tip">加载中…</div></div></div>' +
      '  <div class="llama-card"><div class="llama-card-head">模型库<button class="layui-btn layui-btn-xs" id="llama-btn-rescan">重新扫描</button></div>' +
      '    <div class="llama-dir-row"><input class="layui-input llama-input" id="llama-dir-input" placeholder="输入包含 .gguf 模型的目录路径"><button class="layui-btn layui-btn-sm" id="llama-btn-add-dir">添加目录</button></div>' +
      '    <div id="llama-dirs" class="llama-dirs"></div>' +
      '    <div id="llama-models"></div>' +
      '  </div>' +
      '  <div class="llama-card"><div class="llama-card-head">运行日志</div><div class="llama-logs-wrap"><div class="llama-logs" id="llama-logs"><div class="llama-log-line llama-log-empty">暂无日志</div></div></div></div>' +
      '  <div class="llama-card"><div class="llama-card-head">推理设置<button class="layui-btn layui-btn-sm" id="llama-btn-save-settings">保存设置</button></div><div id="llama-settings"><div class="empty-tip">加载中…</div></div></div>' +
      '</div>';

    bindMainEvents();
    if (isDesktop()) {
      var dirInput = document.getElementById('llama-dir-input');
      if (dirInput) dirInput.placeholder = '点击「添加目录」选择模型文件夹';
    }
    loadModels();
    loadConnect();
    loadSettings();
    refreshStatus();
    startPolling();
  }

  function bindMainEvents() {
    document.getElementById('llama-btn-start').onclick = function () {
      apiSend('/start', 'POST').then(function () {
        layer.msg('正在启动…', { icon: 1, time: 1000 });
        refreshStatus();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    };
    document.getElementById('llama-btn-stop').onclick = function () {
      apiSend('/stop', 'POST').then(function () {
        layer.msg('已停止', { icon: 1, time: 1000 });
        refreshStatus();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    };
    document.getElementById('llama-link-wizard').onclick = function () { openWizard(); };
    document.getElementById('llama-btn-add-dir').onclick = function () {
      addModelDir(document.getElementById('llama-dir-input'), function () {
        loadModels();
      });
    };
    document.getElementById('llama-btn-rescan').onclick = function () { loadModels(true); };
    document.getElementById('llama-btn-save-settings').onclick = saveSettings;
    document.getElementById('llama-btn-integrate').onclick = integrateToConfig;
    document.getElementById('llama-btn-dl-cancel').onclick = function () {
      apiSend('/engine/cancel', 'POST').then(function () { layer.msg('已取消', { icon: 1 }); });
    };
  }

  /* ---------- 状态与日志轮询 ---------- */
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      if (!active) return;
      refreshStatus();
      refreshLogs();
    }, 1500);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function refreshStatus() {
    apiGet('/status').then(function (s) {
      lastStatus = s;
      var badge = document.querySelector('#llama-content .llama-topline');
      if (!badge) return;
      var holder = document.querySelector('#llama-content .llama-badge');
      if (holder) holder.outerHTML = stateBadge(s.state);
      var btnStart = document.getElementById('llama-btn-start');
      var btnStop = document.getElementById('llama-btn-stop');
      if (btnStart) btnStart.disabled = s.state === 'running' || s.state === 'starting';
      if (btnStop) btnStop.disabled = s.state === 'stopped';
      // 下载进度
      var prog = document.getElementById('llama-progress');
      if (prog) {
        if (s.downloading) {
          prog.style.display = '';
          var d = s.download_progress || {};
          var text = document.getElementById('llama-progress-text');
          var inner = document.getElementById('llama-progress-inner');
          text.textContent = d.stage + (d.total_bytes > 0
            ? '  ' + fmtBytes(d.received_bytes) + ' / ' + fmtBytes(d.total_bytes) : '');
          var pct = d.total_bytes > 0 ? Math.min(100, Math.round(100 * d.received_bytes / d.total_bytes)) : 0;
          inner.style.width = (d.indeterminate ? 100 : pct) + '%';
          inner.classList.toggle('llama-progress-indet', !!d.indeterminate);
        } else {
          prog.style.display = 'none';
        }
      }
    }).catch(function () { /* 服务未起等场景静默 */ });
  }

  function refreshLogs() {
    apiGet('/logs?after_id=' + lastLogId).then(function (r) {
      if (!r.lines.length) return;
      var box = document.getElementById('llama-logs');
      if (!box) return;
      var empty = box.querySelector('.llama-log-empty');
      if (empty) empty.remove();
      var html = [];
      r.lines.forEach(function (l) {
        lastLogId = l.id;
        html.push('<div class="llama-log-line">' + escapeHtml(l.text) + '</div>');
      });
      box.insertAdjacentHTML('beforeend', html.join(''));
      box.scrollTop = box.scrollHeight;
    }).catch(function () { });
  }

  /* ---------- 模型库 ---------- */
  function loadModels(force) {
    var scan = force ? apiSend('/models/scan', 'POST') : Promise.resolve();
    scan.then(function () { return apiGet('/models'); }).then(function (m) {
      var dirsBox = document.getElementById('llama-dirs');
      var listBox = document.getElementById('llama-models');
      if (!dirsBox || !listBox) return;
      dirsBox.innerHTML = (m.dirs || []).map(function (d) {
        return '<span class="llama-dir-chip" title="' + escapeHtml(d) + '">' + escapeHtml(d) +
          '<a href="javascript:;" class="llama-dir-remove" data-path="' + escapeHtml(d) + '">✕</a></span>';
      }).join('') || '<span class="llama-dir-empty">尚未添加模型目录</span>';
      dirsBox.querySelectorAll('.llama-dir-remove').forEach(function (a) {
        a.onclick = function () {
          apiSend('/models/dirs/remove', 'POST', { path: a.getAttribute('data-path') })
            .then(loadModels).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        };
      });

      var okModels = (m.models || []).filter(function (x) { return x.ok; });
      var rows = (m.models || []).map(function (x) {
        var name = x.file_path.split(/[\\/]/).pop();
        var defaultBtn = x.is_default
          ? '<span class="llama-default-flag">默认</span>'
          : (x.ok ? '<a href="javascript:;" class="llama-set-default" data-path="' + escapeHtml(x.file_path) + '">设为默认</a>' : '');
        return '<tr class="' + (x.ok ? '' : 'llama-row-bad') + '" title="' + escapeHtml(x.ok ? x.file_path : x.error) + '">' +
          '<td class="llama-td-name">' + escapeHtml(name) + '</td>' +
          '<td>' + (x.ok ? x.size_gb + ' GB' : '-') + '</td>' +
          '<td>' + (x.ok ? escapeHtml(x.quant_label) : '-') + '</td>' +
          '<td>' + (x.ok && x.native_context > 0 ? Math.round(x.native_context / 1024) + 'k' : '-') + '</td>' +
          '<td>' + (x.has_nextn_tensors ? '✓' : '-') + '</td>' +
          '<td>' + defaultBtn + '</td></tr>';
      }).join('');
      listBox.innerHTML = okModels.length
        ? '<table class="llama-table"><thead><tr><th>文件</th><th>大小</th><th>量化</th><th>原生上下文</th><th>MTP</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
        : '<div class="llama-dir-empty">⚠ 所选目录下没有找到可用的 .gguf 模型文件</div>';
      listBox.querySelectorAll('.llama-set-default').forEach(function (a) {
        a.onclick = function () {
          apiSend('/models/default', 'POST', { path: a.getAttribute('data-path') })
            .then(function () { layer.msg('已设为默认模型', { icon: 1 }); loadModels(); refreshStatus(); loadConnect(); })
            .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        };
      });
    }).catch(function (e) {
      layer.msg('模型库加载失败：' + e.message, { icon: 2 });
    });
  }

  /* ---------- 接入信息 ---------- */
  function loadConnect() {
    apiGet('/connect').then(function (c) {
      var box = document.getElementById('llama-connect');
      if (!box) return;
      function block(label, value) {
        return '<div class="llama-kv"><span class="llama-kv-label">' + label + '</span>' +
          '<code class="llama-kv-value">' + escapeHtml(value) + '</code>' +
          '<a href="javascript:;" class="llama-copy" data-copy="' + escapeHtml(value) + '">复制</a></div>';
      }
      var lanLine = c.lan_base_url
        ? block('局域网地址', c.lan_base_url)
        : '<div class="llama-kv"><span class="llama-kv-label">局域网</span><span class="llama-kv-note">未开放（在推理设置里切换监听为 0.0.0.0）</span></div>';
      box.innerHTML =
        block('Base URL', c.base_url + '/v1') +
        block('Chat Completions', c.chat_url) +
        block('决策模型端点', c.systemone_url || '') +
        block('模型名 (model)', c.model || '—') +
        lanLine +
        '<div class="llama-code-label">curl</div>' +
        '<pre class="llama-code"><code>' + escapeHtml(c.curl_example) + '</code></pre>' +
        '<div class="llama-code-label">openai SDK (Python)</div>' +
        '<pre class="llama-code"><code>' + escapeHtml(c.sdk_example) + '</code></pre>';
      box.querySelectorAll('.llama-copy').forEach(function (a) {
        a.onclick = function () { copyText(a.getAttribute('data-copy')); };
      });
    }).catch(function () { });
  }

  /* 接入配置方案：勾选要写入的应用（本地服务讲 OpenAI Chat Completions，
   * Claude Code / Codex / dsh 经本地翻译代理，ZCode 与 pi 直连） */
  var INTEGRATE_TARGETS = [
    { key: 'claude', label: 'Claude Code', on: true },
    { key: 'codex', label: 'Codex', on: false },
    { key: 'dsh', label: 'dsh', on: false },
    { key: 'zcode', label: 'ZCode', on: false },
    { key: 'pi', label: 'pi', on: false }
  ];

  function integrateToConfig() {
    var s = lastStatus;
    if (!s || !s.default_model_path) {
      layer.msg('请先在模型库中设置默认模型', { icon: 0 });
      return;
    }
    var boxes = INTEGRATE_TARGETS.map(function (t) {
      return '<input type="checkbox" name="it_' + t.key + '" title="' + t.label +
        '" lay-skin="primary"' + (t.on ? ' checked' : '') + '>';
    }).join('');
    layer.open({
      type: 1,
      title: '接入配置方案',
      area: ['440px', 'auto'],
      content: '<div class="layui-form" lay-filter="llama-integrate" style="padding:20px 24px;">' +
        '<p style="font-size:14px;margin-bottom:12px;">将创建（或更新）一个指向本地 llama-server 的配置方案，' +
        '模型为当前默认模型，并写入勾选的应用：</p>' +
        '<div class="target-checkboxes">' + boxes + '</div>' +
        '<p style="font-size:12px;color:#8a8e94;margin-top:12px;">' +
        '本地服务讲 OpenAI Chat Completions：Claude Code / Codex / dsh 经本地翻译代理，ZCode 与 pi 直连。</p>' +
        '</div>',
      btn: ['接入', '取消'],
      success: function () { form.render('checkbox', 'llama-integrate'); },
      yes: function (index) {
        var picked = INTEGRATE_TARGETS.filter(function (t) {
          var el = document.querySelector('input[name="it_' + t.key + '"]');
          return el && el.checked;
        }).map(function (t) { return t.key; });
        if (!picked.length) { layer.msg('请至少选择一个应用', { icon: 2 }); return; }
        apiSend('/integrate', 'POST', { targets: picked.join(','), activate: false }).then(function (r) {
          layer.close(index);
          layer.msg(r.activated ? '已创建并切换' : '已创建配置方案，请到「配置方案」页切换生效', { icon: 1, time: 2500 });
        }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      }
    });
  }

  /* ---------- 推理设置 ---------- */
  function loadSettings() {
    apiGet('/settings').then(function (st) {
      var box = document.getElementById('llama-settings');
      if (!box) return;
      var i = st.infer;
      var kvOptions = ['f16', 'q8_0', 'q4_0'].map(function (v) {
        return '<option value="' + v + '"' + (i.kv_level === v ? ' selected' : '') + '>' + v + '</option>';
      }).join('');
      function row(label, input) {
        return '<div class="llama-form-row"><label class="llama-form-label">' + label + '</label>' + input + '</div>';
      }
      box.innerHTML =
        row('监听', '<label class="llama-check"><input type="radio" name="llm_host" value="127.0.0.1"' + (st.host !== '0.0.0.0' ? ' checked' : '') + '>仅本机 127.0.0.1</label>' +
          '<label class="llama-check"><input type="radio" name="llm_host" value="0.0.0.0"' + (st.host === '0.0.0.0' ? ' checked' : '') + '>允许局域网 0.0.0.0</label>') +
        row('端口', '<input class="layui-input llama-input-sm" id="llm_port" type="number" min="1024" max="65535" value="' + st.port + '">') +
        row('默认模型', '<input class="layui-input llama-input" id="llm_model" readonly value="' + escapeHtml(st.default_model_path || '') + '">') +
        row('投影模型 mmproj', '<input class="layui-input llama-input" id="llm_mmproj" placeholder="多模态投影模型路径，纯文本留空" value="' + escapeHtml(st.mmproj_path || '') + '">' +
          '<button class="layui-btn layui-btn-sm" id="llm-mmproj-browse">浏览</button>') +
        row('上下文长度', '<input class="layui-input llama-input-sm" id="llm_ctx" type="number" min="512" step="512" value="' + i.context_tokens + '">') +
        row('KV 缓存', '<select class="llama-select" id="llm_kv">' + kvOptions + '</select>') +
        row('Flash Attention', '<div class="layui-form"><input type="checkbox" id="llm_fa" lay-skin="primary" title="开启"' + (i.flash_attention ? ' checked' : '') + '></div>') +
        row('GPU 层数 -ngl', '<input class="layui-input llama-input-sm" id="llm_ngl" type="number" min="0" max="99" value="' + i.ngl + '"> <span class="llama-form-hint">99 = 全部 offload</span>') +
        row('线程数', '<input class="layui-input llama-input-sm" id="llm_threads" type="number" min="0" value="' + i.threads + '"> <span class="llama-form-hint">0 = 自动</span>') +
        row('MTP 投机解码', '<input class="layui-input llama-input-sm" id="llm_mtp" type="number" min="0" max="8" value="' + i.mtp_steps + '"> <span class="llama-form-hint">0 = 关闭；需引擎与模型支持</span>') +
        row('API Key', '<input class="layui-input llama-input" id="llm_apikey" placeholder="留空 = 不鉴权" value="' + escapeHtml(i.api_key || '') + '">' +
          '<button class="layui-btn layui-btn-sm" id="llm-apikey-gen">生成</button>') +
        row('内存裁剪', '<div class="layui-form"><input type="checkbox" id="llm_trim" lay-skin="primary" title="运行就绪后定时释放文件缓存"' + (st.run.auto_trim_ram ? ' checked' : '') + '></div>') +
        row('附加参数', '<input class="layui-input llama-input" id="llm_extra" placeholder="原样追加的 llama-server 参数" value="' + escapeHtml(i.extra_args || '') + '">');
      document.getElementById('llama-btn-save-settings').onclick = saveSettings;
      form.render('checkbox');
      var mmprojBrowse = document.getElementById('llm-mmproj-browse');
      if (mmprojBrowse) {
        mmprojBrowse.onclick = function () {
          if (!isDesktop()) {
            layer.msg('浏览器模式下请手动粘贴文件路径', { icon: 0 });
            return;
          }
          pickFile().then(function (p) {
            if (p) document.getElementById('llm_mmproj').value = p;
          });
        };
      }
      // 生成随机 API Key（与 a4agent 逻辑一致：24 字节随机数 → sk- 前缀）
      var keyGen = document.getElementById('llm-apikey-gen');
      if (keyGen) {
        keyGen.onclick = function () {
          var bytes = new Uint8Array(24);
          crypto.getRandomValues(bytes);
          var hex = '';
          bytes.forEach(function (b) { hex += b.toString(16).padStart(2, '0'); });
          document.getElementById('llm_apikey').value = 'sk-' + hex;
          layer.msg('已生成，点击「保存设置」生效；服务运行中需重启服务加载新 Key', { icon: 1, time: 3000 });
        };
      }
    }).catch(function () { });
  }

  function saveSettings() {
    function val(id) { return document.getElementById(id).value.trim(); }
    function checked(id) { return document.getElementById(id).checked; }
    var host = (document.querySelector('input[name="llm_host"]:checked') || {}).value || '127.0.0.1';
    apiSend('/settings', 'POST', {
      host: host,
      port: Number(val('llm_port')) || 8080,
      mmproj_path: val('llm_mmproj'),
      infer: {
        context_tokens: Number(val('llm_ctx')) || 32768,
        kv_level: val('llm_kv'),
        flash_attention: checked('llm_fa'),
        ngl: Number(val('llm_ngl')),
        threads: Number(val('llm_threads')),
        mtp_steps: Number(val('llm_mtp')),
        api_key: val('llm_apikey'),
        extra_args: val('llm_extra')
      },
      run: { auto_trim_ram: checked('llm_trim') }
    }).then(function () {
      layer.msg('设置已保存', { icon: 1, time: 1200 });
      refreshStatus();
      loadConnect();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ═════════════════ 首次配置向导 ═════════════════ */

  var wiz = null;   // 向导运行时状态

  function openWizard() {
    wiz = { step: 0, steps: [], hardware: null, models: null, defaultModel: '', downloading: false };
    apiGet('/hardware').then(function (h) {
      wiz.hardware = h;
      wiz.steps = [];
      if (!h.engine_present) wiz.steps.push('engine');
      wiz.steps.push('hw', 'dirs', 'model', 'port');
      wiz.defaultModel = h.default_model_path || '';
      // layui 2.9 的 layer.open 对 jQuery 对象 content 不生效，这里直接传 HTML 字符串
      var content = '<div class="llama-wizard">' +
        '<div class="llama-wizard-title" id="wiz-title"></div>' +
        '<div class="llama-wizard-body" id="wiz-body"></div>' +
        '<div class="llama-wizard-error" id="wiz-error"></div>' +
        '<div class="llama-wizard-foot">' +
        '<button class="layui-btn layui-btn-primary layui-btn-sm" id="wiz-back">&lt; 上一步</button>' +
        '<button class="layui-btn layui-btn-normal layui-btn-sm" id="wiz-next">下一步 &gt;</button>' +
        '</div></div>';
      layer.open({
        type: 1,
        title: 'a4agent 本地模型 · 首次配置向导',
        area: ['780px', '600px'],
        content: content,
        end: function () { wiz = null; }
      });
      showWizStep(0);
    }).catch(function (e) {
      layer.msg('硬件检测失败：' + e.message, { icon: 2 });
    });
  }

  function wizError(msg) {
    var el = document.getElementById('wiz-error');
    if (el) el.textContent = msg || '';
  }

  function showWizStep(i) {
    if (!wiz) return;
    wiz.step = i;
    wizError('');
    var step = wiz.steps[i];
    document.getElementById('wiz-title').textContent = '步骤 ' + (i + 1) + '/' + wiz.steps.length + ' · ' +
      ({ engine: '获取推理引擎', hw: '检测硬件', dirs: '选择模型目录', model: '选择默认模型', port: '服务端口' }[step]);
    document.getElementById('wiz-back').disabled = i <= 0;
    document.getElementById('wiz-next').textContent = i === wiz.steps.length - 1 ? '完成' : '下一步 >';
    if (step === 'engine') wizRenderEngine();
    else if (step === 'hw') wizRenderHardware();
    else if (step === 'dirs') wizRenderDirs();
    else if (step === 'model') wizRenderModel();
    else if (step === 'port') wizRenderPort();
    // lay-skin 皮肤控件需手动渲染（与 app.js confirmSwitch 一致）
    form.render('checkbox');
  }

  /* 步骤：获取引擎 */
  function wizRenderEngine() {
    var h = wiz.hardware;
    var body = document.getElementById('wiz-body');
    var radios = h.packs.map(function (p) {
      var rec = p.id === h.recommended_pack_id;
      return '<label class="llama-wizard-pack"><input type="radio" name="wiz_pack" value="' + p.id + '"' + (rec ? ' checked' : '') + '>' +
        '<b>' + escapeHtml(p.label) + '</b> — 约 ' + p.size_label +
        '<span class="llama-wizard-pack-note">' + escapeHtml(p.note) + (rec ? ' · 推荐' : '') + '</span></label>';
    }).join('');
    body.innerHTML =
      '<div class="llama-wizard-intro">轻量安装不内置推理引擎（llama-server）。下面按你的显卡列出 llama.cpp 官方发布的引擎包' +
      '（钉定版本 ' + escapeHtml(h.repo_tag) + '，取自 github.com/ggml-org/llama.cpp 官方 Release），选择后自动下载安装；也可以离线安装或暂时跳过。</div>' +
      radios +
      '<label class="llama-wizard-pack"><input type="radio" name="wiz_pack" value="__offline">离线安装：指定本机已有的引擎目录（含 llama-server.exe）</label>' +
      '<div class="llama-dir-row" id="wiz-offline-row" style="display:none;padding-left:24px">' +
      '<input class="layui-input llama-input" id="wiz-offline-dir" placeholder="包含 llama-server.exe 的目录">' +
      '</div>' +
      '<label class="llama-wizard-pack"><input type="radio" name="wiz_pack" value="__skip">暂时跳过（稍后在设置里指定引擎目录）</label>' +
      '<div class="llama-wizard-dlstatus" id="wiz-dlstatus"></div>' +
      '<div class="llama-progress-bar llama-progress-bar-lg"><div class="llama-progress-inner" id="wiz-dlbar" style="width:0%"></div></div>';
    body.querySelector('input[value="__offline"]').onchange = function (e) {
      document.getElementById('wiz-offline-row').style.display = e.target.checked ? '' : 'none';
    };
  }

  function wizEngineNext() {
    var choice = (document.querySelector('input[name="wiz_pack"]:checked') || {}).value;
    if (!choice) { wizError('请选择一个引擎包。'); return; }
    if (choice === '__offline') {
      var dir = (document.getElementById('wiz-offline-dir') || {}).value || '';
      apiSend('/engine/offline', 'POST', { dir: dir.trim() }).then(function () {
        showWizStep(wiz.step + 1);
      }).catch(function (e) { wizError(e.message); });
      return;
    }
    if (choice === '__skip') { showWizStep(wiz.step + 1); return; }
    // 在线下载
    if (wiz.downloading) return;
    wiz.downloading = true;
    document.getElementById('wiz-next').disabled = true;
    document.getElementById('wiz-back').disabled = true;
    apiSend('/engine/download', 'POST', { pack_id: choice }).then(function () {
      pollWizDownload();
    }).catch(function (e) {
      wiz.downloading = false;
      document.getElementById('wiz-next').disabled = false;
      document.getElementById('wiz-back').disabled = false;
      wizError('下载失败：' + e.message + '（可改用离线安装，或检查网络后重试）');
    });
  }

  function pollWizDownload() {
    var tick = function () {
      if (!wiz) return; // 向导已关闭
      apiGet('/engine/progress').then(function (d) {
        var status = document.getElementById('wiz-dlstatus');
        var bar = document.getElementById('wiz-dlbar');
        if (!status || !bar) return;
        status.textContent = d.stage + (d.total_bytes > 0
          ? '   ' + fmtBytes(d.received_bytes) + ' / ' + fmtBytes(d.total_bytes) : '');
        var pct = d.total_bytes > 0 ? Math.min(100, Math.round(100 * d.received_bytes / d.total_bytes)) : 0;
        bar.style.width = (d.indeterminate ? 100 : pct) + '%';
        bar.classList.toggle('llama-progress-indet', !!d.indeterminate);
        if (!d.running) {
          wiz.downloading = false;
          if (d.error) {
            document.getElementById('wiz-next').disabled = false;
            document.getElementById('wiz-back').disabled = false;
            wizError(d.error + '（可改用离线安装，或检查网络后重试）');
          } else if (d.done) {
            status.textContent = '✔ 引擎安装完成。';
            setTimeout(function () { if (wiz) showWizStep(wiz.step + 1); }, 400);
          } else {
            // 已取消
            document.getElementById('wiz-next').disabled = false;
            document.getElementById('wiz-back').disabled = false;
          }
          return;
        }
        setTimeout(tick, 400);
      }).catch(function () { setTimeout(tick, 800); });
    };
    tick();
  }

  /* 步骤：硬件 */
  function wizRenderHardware() {
    var h = wiz.hardware;
    var body = document.getElementById('wiz-body');
    var gpuLines = (h.gpus || []).map(function (g) {
      return '<div class="llama-wizard-gpu-line">[' + escapeHtml(g.vendor) + '] ' + escapeHtml(g.name) +
        ' — 显存 ' + g.vram_gb + ' GB' + (g.driver_version ? ' · 驱动 ' + escapeHtml(g.driver_version) : '') + '</div>';
    }).join('') || '<div class="llama-wizard-gpu-line">未检测到独立显卡（将以 CPU 模式运行，速度受限）</div>';
    body.innerHTML =
      '<div class="llama-wizard-panel"><div class="llama-wizard-panel-title">检测到的显卡</div>' + gpuLines + '</div>' +
      '<div class="llama-wizard-panel"><div class="llama-wizard-panel-title">为你推荐的推理预设</div>' +
      '<div>预设档位：<b>' + escapeHtml(h.preset.label) + '</b></div>' +
      '<div>上下文 ' + h.preset.context_tokens + ' tokens · KV 缓存 ' + escapeHtml(h.preset.kv_level) + '</div>' +
      '<div class="llama-wizard-note">' + escapeHtml(h.preset.note) + '</div></div>' +
      '<div class="llama-wizard-note">预设只是起点：稍后可以随时在「推理设置」里调整上下文长度、KV 缓存级别、MTP 等所有参数。</div>';
    wiz.preset = h.preset;
  }

  /* 步骤：模型目录 */
  function wizRenderDirs() {
    var body = document.getElementById('wiz-body');
    body.innerHTML =
      '<div class="llama-dir-row"><input class="layui-input llama-input" id="wiz-dir-input" placeholder="输入包含 .gguf 模型文件的目录路径">' +
      '<button class="layui-btn layui-btn-sm" id="wiz-btn-add-dir">添加目录</button></div>' +
      '<div id="wiz-dirs" class="llama-dirs"></div>' +
      '<div id="wiz-models"></div>' +
      '<div class="llama-wizard-note" id="wiz-scan-tip">请先添加模型所在目录。</div>';
    document.getElementById('wiz-btn-add-dir').onclick = function () {
      addModelDir(document.getElementById('wiz-dir-input'), function (m) {
        wiz.models = m;
        wizRenderDirsList();
      });
    };
    var wizDirInput = document.getElementById('wiz-dir-input');
    if (isDesktop() && wizDirInput) {
      wizDirInput.placeholder = '点击「添加目录」选择模型文件夹';
    }
    apiGet('/models').then(function (m) { wiz.models = m; wizRenderDirsList(); });
  }

  function wizRenderDirsList() {
    var m = wiz.models || { dirs: [], models: [] };
    var dirsBox = document.getElementById('wiz-dirs');
    var listBox = document.getElementById('wiz-models');
    if (!dirsBox || !listBox) return;
    dirsBox.innerHTML = (m.dirs || []).map(function (d) {
      return '<span class="llama-dir-chip" title="' + escapeHtml(d) + '">' + escapeHtml(d) +
        '<a href="javascript:;" class="llama-dir-remove" data-path="' + escapeHtml(d) + '">✕</a></span>';
    }).join('') || '<span class="llama-dir-empty">尚未添加模型目录</span>';
    dirsBox.querySelectorAll('.llama-dir-remove').forEach(function (a) {
      a.onclick = function () {
        apiSend('/models/dirs/remove', 'POST', { path: a.getAttribute('data-path') })
          .then(function (m2) { wiz.models = m2; wizRenderDirsList(); })
          .catch(function (e) { wizError(e.message); });
      };
    });
    var ok = (m.models || []).filter(function (x) { return x.ok; });
    var rows = (m.models || []).map(function (x) {
      var name = x.file_path.split(/[\\/]/).pop();
      return '<tr class="' + (x.ok ? '' : 'llama-row-bad') + '" title="' + escapeHtml(x.ok ? x.file_path : x.error) + '">' +
        '<td class="llama-td-name">' + escapeHtml(name) + '</td>' +
        '<td>' + (x.ok ? x.size_gb + ' GB' : '-') + '</td>' +
        '<td>' + (x.ok ? escapeHtml(x.quant_label) : '-') + '</td>' +
        '<td>' + (x.ok && x.native_context > 0 ? Math.round(x.native_context / 1024) + 'k' : '-') + '</td>' +
        '<td>' + (x.has_nextn_tensors ? '✓' : '-') + '</td></tr>';
    }).join('');
    listBox.innerHTML = ok.length
      ? '<table class="llama-table"><thead><tr><th>文件</th><th>大小</th><th>量化</th><th>原生上下文</th><th>MTP</th></tr></thead><tbody>' + rows + '</tbody></table>'
      : '';
    document.getElementById('wiz-scan-tip').textContent = ok.length
      ? '已找到 ' + ok.length + ' 个可用模型（灰色行解析失败，将被忽略）。'
      : '⚠ 所选目录下没有找到可用的 .gguf 模型文件。';
  }

  /* 步骤：默认模型 */
  function wizRenderModel() {
    var body = document.getElementById('wiz-body');
    var ok = ((wiz.models || {}).models || []).filter(function (x) { return x.ok; });
    // 优先 Ornith-1.5-9B（与 a4agent 行为一致），其次第一个
    var preferred = -1;
    ok.forEach(function (m, i) {
      var name = m.file_path.split(/[\\/]/).pop().toLowerCase();
      if (preferred < 0 && name.indexOf('ornith') !== -1 && name.indexOf('9b') !== -1) preferred = i;
    });
    var idx = preferred >= 0 ? preferred : (ok.length ? 0 : -1);
    wiz.defaultModel = idx >= 0 ? ok[idx].file_path : '';
    var options = ok.map(function (m, i) {
      var name = m.file_path.split(/[\\/]/).pop();
      return '<option value="' + escapeHtml(m.file_path) + '"' + (i === idx ? ' selected' : '') + '>' + escapeHtml(name) + '</option>';
    }).join('');
    body.innerHTML =
      '<div class="llama-wizard-intro">默认启动的模型：</div>' +
      '<select class="llama-select llama-select-wide" id="wiz-model-select">' + options + '</select>' +
      '<div class="llama-wizard-panel" id="wiz-model-info" style="margin-top:12px"></div>';
    document.getElementById('wiz-model-select').onchange = function (e) {
      wiz.defaultModel = e.target.value;
      wizModelInfo();
    };
    wizModelInfo();
  }

  function wizModelInfo() {
    var info = document.getElementById('wiz-model-info');
    if (!wiz.defaultModel) { info.innerHTML = ''; return; }
    var preset = wiz.preset || { context_tokens: 32768, kv_level: 'q4_0', label: '' };
    apiSend('/models/risk', 'POST', {
      path: wiz.defaultModel,
      context_tokens: preset.context_tokens,
      kv_level: preset.kv_level
    }).then(function (r) {
      var m = r.model;
      var nativeCtx = m.native_context > 0 ? Math.round(m.native_context / 1024) + 'k' : '未知';
      info.innerHTML =
        '<div>文件：' + escapeHtml(m.file_path) + '</div>' +
        '<div>架构 ' + escapeHtml(m.arch) + ' · 层数 ' + m.layers + ' · 量化 ' + escapeHtml(m.quant_label) +
        ' · 大小 ' + (m.file_size / (1024 * 1024 * 1024)).toFixed(1) + ' GB</div>' +
        '<div>原生上下文 ' + nativeCtx + (m.has_nextn_tensors ? ' · 含 MTP 层（可在设置里开启投机解码）' : '') + '</div>' +
        '<div class="' + (r.risk ? 'llama-wizard-risk' : 'llama-wizard-ok') + '">' +
        escapeHtml(r.risk || '✔ 以当前预设运行没有显存风险。') + '</div>';
    }).catch(function (e) {
      info.innerHTML = '<div class="llama-wizard-risk">' + escapeHtml(e.message) + '</div>';
    });
  }

  /* 步骤：端口 */
  function wizRenderPort() {
    var body = document.getElementById('wiz-body');
    var preset = wiz.preset || { label: '-', context_tokens: 32768, kv_level: 'q4_0' };
    var modelName = wiz.defaultModel ? wiz.defaultModel.split(/[\\/]/).pop() : '-';
    body.innerHTML =
      '<div class="llama-wizard-intro">服务端口：</div>' +
      '<input class="layui-input llama-input-sm" id="wiz-port" type="number" min="1024" max="65535" value="8080">' +
      '<div class="llama-wizard-panel" style="margin-top:12px">' +
      '<div class="llama-wizard-panel-title">配置摘要</div>' +
      '<div>默认模型：' + escapeHtml(modelName) + '</div>' +
      '<div>服务端口：<span id="wiz-summary-port">8080</span>（API: http://127.0.0.1:<span id="wiz-summary-port2">8080</span>/ ）</div>' +
      '<div>推理预设：' + escapeHtml(preset.label) + ' —— 上下文 ' + preset.context_tokens + ', KV ' + escapeHtml(preset.kv_level) + '</div>' +
      '</div>' +
      '<div class="layui-form llama-form-plain"><input type="checkbox" id="wiz-start" lay-skin="primary" title="完成后立即启动服务" checked></div>';
    document.getElementById('wiz-port').oninput = function (e) {
      var t1 = document.getElementById('wiz-summary-port');
      var t2 = document.getElementById('wiz-summary-port2');
      if (t1) t1.textContent = e.target.value;
      if (t2) t2.textContent = e.target.value;
    };
  }

  function wizFinish() {
    var port = Number((document.getElementById('wiz-port') || {}).value) || 8080;
    var startNow = !!(document.getElementById('wiz-start') || {}).checked;
    var preset = wiz.preset || null;
    var defaultModel = wiz.defaultModel;
    apiSend('/wizard/finish', 'POST', {
      port: port,
      default_model_path: defaultModel,
      preset: preset ? { context_tokens: preset.context_tokens, kv_level: preset.kv_level, label: preset.label } : null
    }).then(function () {
      layer.closeAll();  // 触发 end 回调把 wiz 置空，默认模型先取局部变量
      layer.msg('配置完成', { icon: 1, time: 1200 });
      loaded = false;
      boot();
      if (startNow && defaultModel) {
        // boot 重绘后再启动
        setTimeout(function () {
          apiSend('/start', 'POST').catch(function () { });
        }, 500);
      }
    }).catch(function (e) { wizError(e.message); });
  }

  /* 向导按钮事件（layer 每次重建 DOM，用事件委托） */
  document.addEventListener('click', function (ev) {
    if (ev.target.id === 'wiz-next') {
      var step = wiz && wiz.steps[wiz.step];
      if (!wiz || wiz.downloading) return;
      if (step === 'engine') wizEngineNext();
      else if (step === 'dirs') {
        var ok = ((wiz.models || {}).models || []).filter(function (x) { return x.ok; });
        if (!ok.length) { wizError('所选目录中未找到有效的 .gguf 模型文件，请检查目录或添加其他目录。'); return; }
        showWizStep(wiz.step + 1);
      } else if (step === 'model') {
        if (!wiz.defaultModel) { wizError('请选择一个默认模型。'); return; }
        apiSend('/models/default', 'POST', { path: wiz.defaultModel }).then(function () {
          showWizStep(wiz.step + 1);
        }).catch(function (e) { wizError(e.message); });
      } else if (step === 'port') wizFinish();
      else showWizStep(wiz.step + 1);
    } else if (ev.target.id === 'wiz-back') {
      if (wiz && wiz.step > 0 && !wiz.downloading) showWizStep(wiz.step - 1);
    }
  });
});
