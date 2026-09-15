layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var element = layui.element;
  var $ = layui.$;
  var API = '/api/v1';

  var S = {
    status: null,
    models: [],
    selected: '',          // 模型页当前选中的文件路径
    lastLogId: 0,
    modelsLoaded: false,
    settingsLoaded: false,
    dirOpen: false,        // 目录选择器是否打开（事件委托用）
    updLayer: null,        // 更新下载进度层
    logStick: true         // 日志是否跟随滚动
  };

  /* ---------- 工具 ---------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function apiGet(path) {
    return fetch(API + path).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || j.detail || '请求失败'); });
      return r.json();
    });
  }

  function apiSend(path, method, body) {
    return fetch(API + path, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || j.detail || '请求失败'); });
      return r.json();
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  function q(id) { return document.getElementById(id); }

  /* ---------- 事件流（SSE：日志 / 状态 / 更新进度） ---------- */
  function connectEvents() {
    var es = new EventSource(API + '/logs');
    es.addEventListener('log', function (e) {
      var d = JSON.parse(e.data);
      if (d.id <= S.lastLogId) return;
      S.lastLogId = d.id;
      appendLog(d.text);
    });
    es.addEventListener('state', function (e) {
      applyStatus(JSON.parse(e.data));
    });
    es.addEventListener('update', function (e) {
      handleUpdateEvent(JSON.parse(e.data));
    });
    // EventSource 断线自动重连；重连后的 backlog 由 /logs/recent 补齐
    es.onopen = function () {
      apiGet('/logs/recent?after=' + S.lastLogId).then(function (r) {
        (r.lines || []).forEach(function (l) {
          if (l.id > S.lastLogId) { S.lastLogId = l.id; appendLog(l.text); }
        });
      }).catch(function () { });
    };
  }

  function appendLog(text) {
    var box = q('log-box');
    var empty = box.querySelector('.log-empty');
    if (empty) empty.remove();
    var div = document.createElement('div');
    div.className = 'log-line';
    if (/^\[(错误|失败)\]/.test(text) || /exit code/.test(text)) div.classList.add('log-error');
    else if (/^\[(就绪|裁剪)\]/.test(text) || /health ok/.test(text)) div.classList.add('log-ok');
    var m = String(text).match(/^(\d{2}:\d{2}:\d{2})\s+(.*)$/s);
    if (m) {
      var t = document.createElement('span');
      t.className = 'log-time';
      t.textContent = m[1];
      div.appendChild(t);
      div.appendChild(document.createTextNode(m[2]));
    } else {
      div.textContent = text;
    }
    box.appendChild(div);
    // 控制最大行数，避免长跑内存膨胀
    while (box.childElementCount > 1500) box.removeChild(box.firstChild);
    if (S.logStick) box.scrollTop = box.scrollHeight;
  }

  /* ---------- 状态渲染 ---------- */
  var STATE_TEXT = { stopped: '已停止', starting: '启动中…', running: '运行中', failed: '失败' };

  function applyStatus(st) {
    S.status = st;
    var state = st.state;
    var text = STATE_TEXT[state] || state;
    if (state === 'running' && st.default_model_name) text += ' · ' + st.default_model_name;

    var head = q('status-text');
    head.textContent = text;
    head.className = 'status-text status-' + state;

    var dot = q('hero-dot');
    dot.className = 'status-dot dot-' + state;
    var title = q('hero-title');
    title.textContent = text.replace(/ · .*/, '');
    title.className = 'status-hero-title t-' + state;

    var meta = [];
    meta.push('接口地址 ' + (st.base_url || '-'));
    meta.push('默认模型 ' + (st.default_model_name || '（未设置）'));
    if (st.version) meta.push('v' + st.version);
    q('hero-meta').textContent = meta.join('　·　');

    var busy = state === 'running' || state === 'starting';
    q('btn-start').disabled = busy;
    q('btn-stop').disabled = !busy;
    var toggle = q('btn-toggle-service');
    toggle.textContent = busy ? '停止服务' : '启动服务';

    q('engine-banner').style.display = st.engine_missing ? 'flex' : 'none';
  }

  function toggleService() {
    var busy = S.status && (S.status.state === 'running' || S.status.state === 'starting');
    if (busy) {
      apiSend('/stop', 'POST').then(function () {
        layer.msg('正在停止服务…', { icon: 1 });
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    } else {
      apiSend('/start', 'POST').then(function () {
        layer.msg('服务启动中，请看下方日志', { icon: 1, time: 2000 });
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      element.tabChange('main-tab', 'status');
    }
  }

  /* ---------- 模型页 ---------- */
  function loadModels() {
    return apiGet('/models').then(function (r) {
      S.models = r.models || [];
      if (S.selected && !S.models.some(function (m) { return m.file_path === S.selected; }))
        S.selected = '';
      renderModels(r.default_model, r.model_dirs || []);
      S.modelsLoaded = true;
    }).catch(function (e) {
      q('models-tbody').innerHTML = '<tr><td colspan="5"><div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div></td></tr>';
    });
  }

  function renderModels(defaultModel, modelDirs) {
    var tbody = q('models-tbody');
    if (!S.models.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty-tip">未找到 .gguf 模型，请先「添加目录」</div></td></tr>';
    } else {
      tbody.innerHTML = S.models.map(function (m) {
        var isDefault = m.file_path === defaultModel;
        var isSelected = m.file_path === S.selected;
        var name = '<div class="model-name"><span>' + escapeHtml(m.file_name) + '</span>' +
          (isDefault ? '<span class="default-pill">默认</span>' : '') + '</div>';
        return '<tr data-path="' + escapeHtml(m.file_path) + '" class="' + (isSelected ? 'row-selected' : '') + '">' +
          '<td>' + name + '<div class="form-hint" style="margin-top:2px">' + escapeHtml(m.file_path) + '</div></td>' +
          '<td class="dim">' + escapeHtml(m.size_text) + '</td>' +
          '<td>' + escapeHtml(m.quant) + '</td>' +
          '<td class="dim">' + escapeHtml(m.ctx_text) + '</td>' +
          '<td class="' + (m.mtp ? 'ok-yes' : 'dim') + '">' + (m.mtp ? '✓' : '-') + '</td>' +
          '</tr>';
      }).join('');
    }

    var hint = q('default-model-hint');
    hint.textContent = defaultModel
      ? '默认模型：' + defaultModel
      : '默认模型：（未设置——选中一行后点「设为默认模型」）';
    if (!defaultModel && modelDirs && !modelDirs.length)
      hint.textContent = '默认模型：（未设置——请先「添加目录」扫描本地 .gguf 模型）';
  }

  $(document).on('click', '#models-tbody tr[data-path]', function () {
    S.selected = this.getAttribute('data-path');
    document.querySelectorAll('#models-tbody tr').forEach(function (tr) {
      tr.classList.toggle('row-selected', tr.getAttribute('data-path') === S.selected);
    });
  });

  function setDefaultModel() {
    if (!S.selected) { layer.msg('请先点选一个有效模型。', { icon: 0 }); return; }
    apiSend('/models/default', 'POST', { path: S.selected }).then(function () {
      layer.msg('已设为默认模型', { icon: 1 });
      loadModels();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  function addModelDir() {
    openDirPicker('', function (dir) {
      apiSend('/models/dirs', 'POST', { path: dir }).then(function () {
        layer.msg('已添加目录并重新扫描', { icon: 1 });
        loadModels();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
  }

  /* ---------- 目录选择器（玻璃柜抽屉） ---------- */
  function openDirPicker(startPath, onPick) {
    var cur = startPath || '';
    S.dirOpen = true;

    function frame(inner) {
      return '<div class="dir-panel" id="dir-body">' + inner + '</div>';
    }

    function listHtml(d) {
      var html = '<div class="dir-path">' +
        '<span class="dir-up" id="dir-go-up">' + (d.parent ? '← 上一级' : '') + '</span>' +
        '<span>' + escapeHtml(d.current || '此电脑（选择磁盘分区）') + '</span></div><div class="dir-list">';
      if (!d.entries.length) html += '<div class="dir-empty">此目录为空</div>';
      d.entries.forEach(function (en) {
        html += '<div class="dir-item" data-path="' + escapeHtml(en.path) + '">' +
          '<i class="layui-icon layui-icon-folder"></i>' +
          '<span class="dir-name">' + escapeHtml(en.name) + '</span></div>';
      });
      html += '</div>';
      return frame(html);
    }

    apiGet('/dirs?path=' + encodeURIComponent(cur)).then(function (d) {
      cur = d.current;
      layer.open({
        type: 1,
        title: '选择目录',
        area: '580px',
        content: listHtml(d),
        btn: ['选择当前目录', '取消'],
        yes: function (index) {
          S.dirOpen = false;
          layer.close(index);
          onPick(cur);
        },
        end: function () { S.dirOpen = false; }
      });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });

    function navigate(path) {
      apiGet('/dirs?path=' + encodeURIComponent(path)).then(function (d) {
        cur = d.current;
        $('#dir-body').closest('.layui-layer-content').html(listHtml(d));
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    }

    $(document).off('click.dirpick').on('click.dirpick', '.dir-item', function () {
      if (!S.dirOpen) return;
      navigate(this.getAttribute('data-path'));
    });
    $(document).off('click.dirup').on('click.dirup', '#dir-go-up', function () {
      if (!S.dirOpen) return;
      apiGet('/dirs?path=' + encodeURIComponent(cur)).then(function (d) {
        if (d.parent) navigate(d.parent);
      }).catch(function () { });
    });
  }

  /* ---------- 设置页 ---------- */
  function fillSettings(s) {
    q('f-port').value = s.port;
    q('f-lan').checked = !!s.lan;
    q('f-engine-dir').value = s.engine_dir;
    q('f-ctx').value = s.context_tokens;
    q('f-kv').value = s.kv_level;
    q('f-fa').checked = !!s.flash_attention;
    q('f-ngl').value = s.ngl;
    q('f-mtp').value = s.mtp_steps;
    q('f-threads').value = s.threads;
    q('f-ub').value = s.ubatch;
    q('f-par').value = s.parallel;
    q('f-extra').value = s.extra_args;
    q('f-trim').checked = !!s.auto_trim_ram;
    q('f-trim-sec').value = s.trim_delay_seconds;
    if (!s.caps_mtp) {
      q('f-mtp').value = 0;
      q('f-mtp').disabled = true;
      q('mtp-hint').textContent = '当前引擎不支持 MTP，步数已锁定为关闭';
    } else {
      q('f-mtp').disabled = false;
      q('mtp-hint').textContent = '0 = 关闭；需引擎与模型支持';
    }
    if (s.version) q('version-text').textContent = '当前版本 v' + s.version;
    form.render(null, 'settings-form');
  }

  function loadSettings() {
    return apiGet('/settings').then(function (s) {
      fillSettings(s);
      S.settingsLoaded = true;
    }).catch(function (e) { layer.msg('设置加载失败：' + e.message, { icon: 2 }); });
  }

  function collectSettings() {
    function num(id, min, max, fb) {
      var v = parseInt(q(id).value, 10);
      if (isNaN(v)) v = fb;
      return Math.min(max, Math.max(min, v));
    }
    return {
      port: num('f-port', 1024, 65535, 8080),
      lan: q('f-lan').checked,
      engine_dir: q('f-engine-dir').value.trim(),
      context_tokens: num('f-ctx', 512, 1048576, 32768),
      kv_level: q('f-kv').value || 'q4_0',
      flash_attention: q('f-fa').checked,
      ngl: num('f-ngl', 0, 999, 99),
      threads: num('f-threads', 0, 256, 0),
      ubatch: num('f-ub', 16, 16384, 512),
      parallel: num('f-par', 1, 16, 1),
      mtp_steps: num('f-mtp', 0, 10, 0),
      extra_args: q('f-extra').value.trim(),
      auto_trim_ram: q('f-trim').checked,
      trim_delay_seconds: num('f-trim-sec', 5, 3600, 90)
    };
  }

  function saveSettings() {
    apiSend('/settings', 'POST', collectSettings()).then(function () {
      layer.msg('设置已保存。若服务正在运行，需要「停止 → 启动」才会应用新参数。', { icon: 1, time: 3600 });
      loadStatus();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---------- 接入页 ---------- */
  function loadConnect() {
    return apiGet('/connect').then(function (c) {
      q('v-base-url').textContent = c.base_url;
      q('v-chat-url').textContent = c.chat_url;
      q('v-model-id').textContent = c.model_id;
      q('code-curl').textContent = c.curl;
      q('code-py').textContent = c.python;

      var notice = q('lan-notice');
      if (c.lan && c.lan_url) {
        notice.style.display = 'flex';
        notice.textContent = '已允许局域网访问：其他设备可改用 ' + c.lan_url;
      } else {
        notice.style.display = 'none';
      }
    }).catch(function () { });
  }

  $(document).on('click', '.copy-link', function () {
    var el = q(this.getAttribute('data-copy'));
    if (!el) return;
    copyText(el.textContent).then(function () {
      layer.msg('已复制到剪贴板', { icon: 1 });
    }).catch(function () { layer.msg('复制失败', { icon: 2 }); });
  });

  /* ---------- 软件更新 ---------- */
  function checkUpdate() {
    layer.load(2, { shade: [0.1, '#fff'] });
    apiSend('/update/check', 'POST').then(function (r) { layer.closeAll('loading'); return r; }).catch(function (e) {
      layer.closeAll('loading');
      throw e;
    }).then(function (r) {
      switch (r.status) {
        case 'update_available':
          var notes = r.notes
            ? '<div class="update-notes">' + escapeHtml(r.notes) + '</div>'
            : '';
          layer.confirm(
            '<div class="update-panel"><p>发现新版本 <b>v' + escapeHtml(r.latest_version) +
            '</b><span class="update-versions">（当前 v' + escapeHtml(r.current_version) + '）</span></p>' + notes + '</div>',
            { title: '软件更新', btn: ['下载更新', '暂不'] },
            function (index) { layer.close(index); installUpdate(r.latest_version); }
          );
          break;
        case 'up_to_date':
          layer.msg('已是最新版本 v' + r.current_version, { icon: 1 });
          break;
        case 'ignored':
          layer.msg('已跳过版本 v' + r.latest_version + '（当前 v' + r.current_version + '）', { icon: 0 });
          break;
        default:
          layer.msg(r.error || '检查更新失败', { icon: 2, time: 5000 });
      }
      if (r.current_version) q('version-text').textContent = '当前版本 v' + r.current_version;
    }).catch(function (e) { layer.msg(e.message, { icon: 2, time: 5000 }); });
  }

  function installUpdate(version) {
    S.updLayer = layer.open({
      type: 1,
      title: '正在下载更新 v' + escapeHtml(version),
      area: '420px',
      closeBtn: 1,
      content: '<div class="update-panel" style="width:372px">' +
        '<div class="layui-progress" lay-filter="upd-bar"><div class="layui-progress-bar" lay-percent="0%"></div></div>' +
        '<div class="update-download-status" id="upd-status">准备中…</div></div>'
    });
    apiSend('/update/install', 'POST', {}).catch(function (e) {
      if (S.updLayer != null) { layer.close(S.updLayer); S.updLayer = null; }
      layer.msg('下载失败：' + e.message, { icon: 2, time: 5000 });
    });
  }

  function handleUpdateEvent(d) {
    if (d.phase === 'progress') {
      element.progress('upd-bar', d.percent + '%');
      var st = q('upd-status');
      if (st) st.textContent = d.received_mb + ' / ' + d.total_mb + ' MB';
    } else if (d.phase === 'done') {
      if (S.updLayer != null) { layer.close(S.updLayer); S.updLayer = null; }
      layer.confirm(
        '<div class="update-panel"><p>v' + escapeHtml(d.version) + ' 安装包下载完成并已通过校验。</p>' +
        '<p>立即安装并重启应用？安装器会先关闭当前服务。</p></div>',
        { title: '安装更新', btn: ['立即安装', '稍后'] },
        function (index) {
          layer.close(index);
          layer.msg('安装器已启动，应用即将退出…', { icon: 16, time: 0, shade: 0.2 });
          apiSend('/update/apply', 'POST', {}).catch(function (e) {
            layer.closeAll();
            layer.msg(e.message, { icon: 2, time: 5000 });
          });
        });
    } else if (d.phase === 'error') {
      if (S.updLayer != null) { layer.close(S.updLayer); S.updLayer = null; }
      layer.msg('下载失败：' + (d.error || '未知错误'), { icon: 2, time: 5000 });
    }
  }

  /* ---------- 向导 / WebUI ---------- */
  function runWizard() {
    apiSend('/wizard', 'POST', {}).then(function () {
      layer.msg('已打开配置向导（在应用主窗口中完成）', { icon: 1 });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  function openWebUi() {
    if (!S.status || !S.status.port) { layer.msg('请先启动服务', { icon: 0 }); return; }
    window.open('http://127.0.0.1:' + S.status.port + '/', '_blank');
  }

  /* ---------- 事件绑定 ---------- */
  q('btn-toggle-service').addEventListener('click', toggleService);
  q('btn-start').addEventListener('click', toggleService);
  q('btn-stop').addEventListener('click', toggleService);
  q('btn-open-webui').addEventListener('click', openWebUi);
  q('btn-check-update').addEventListener('click', checkUpdate);
  q('btn-check-update-2').addEventListener('click', checkUpdate);
  q('btn-banner-wizard').addEventListener('click', runWizard);
  q('btn-wizard').addEventListener('click', runWizard);
  q('btn-add-dir').addEventListener('click', addModelDir);
  q('btn-rescan').addEventListener('click', function () {
    loadModels().then(function () { layer.msg('已重新扫描', { icon: 1, time: 1200 }); });
  });
  q('btn-set-default').addEventListener('click', setDefaultModel);
  q('btn-browse-engine').addEventListener('click', function () {
    openDirPicker(q('f-engine-dir').value, function (dir) {
      q('f-engine-dir').value = dir;
      form.render(null, 'settings-form');
    });
  });
  q('btn-save-settings').addEventListener('click', saveSettings);

  // 日志跟随滚动：用户向上翻阅时暂停自动滚动，回到底部恢复
  q('log-box').addEventListener('scroll', function () {
    var b = this;
    S.logStick = b.scrollTop + b.clientHeight >= b.scrollHeight - 48;
  });

  element.on('tab(main-tab)', function () {
    var id = this.getAttribute('lay-id');
    if (id === 'models') loadModels();
    else if (id === 'settings') { if (!S.settingsLoaded) loadSettings(); }
    else if (id === 'connect') loadConnect();
  });

  /* ---------- 启动 ---------- */
  connectEvents();
  loadStatus();
  apiGet('/logs/recent').then(function (r) {
    (r.lines || []).forEach(function (l) {
      if (l.id > S.lastLogId) { S.lastLogId = l.id; appendLog(l.text); }
    });
  }).catch(function () { });

  function loadStatus() {
    return apiGet('/status').then(applyStatus).catch(function () { });
  }
});
