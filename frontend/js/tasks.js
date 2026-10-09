/* 任务下发（无头 Agent 任务）—— 引擎探测 / 下发 / 队列轮询 / 产出查看。
 * 下发与执行分离：POST 立即返回 task_id，本页只轮询状态与产出。
 */
layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var API = '/api/v1/tasks';

  var loaded = false;
  var active = false;
  var pollTimer = null;
  var engines = [];
  var activeConfig = null;
  var workdir = '';         // 已选工作目录，空 = 交给后端回落到用户主目录
  var lastTasks = {};      // id -> status，用于识别「刚结束」的任务并提示
  var firstLoad = true;

  /* ---------- 工具 ---------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function parseError(j, status) {
    var detail = j && j.detail;
    if (detail && typeof detail === 'object') detail = detail.message || JSON.stringify(detail);
    return detail || ('请求失败（HTTP ' + status + '）');
  }

  function apiGet(path) {
    return fetch(API + path).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(parseError(j, r.status)); },
        function () { throw new Error('请求失败（HTTP ' + r.status + '）'); });
      return r.json();
    });
  }

  function apiSend(path, method, body) {
    return fetch(API + path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(parseError(j, r.status)); },
        function () { throw new Error('请求失败（HTTP ' + r.status + '）'); });
      if (r.status === 204) return null;
      return r.json();
    });
  }

  function fmtTime(s) { return s ? String(s).slice(5, 19) : '—'; }

  function fmtDuration(sec) {
    if (sec == null) return '—';
    if (sec < 60) return sec + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  }

  var STATUS = {
    pending: ['排队中', 'task-status-pending'],
    running: ['执行中', 'task-status-running'],
    success: ['已完成', 'task-status-success'],
    failed: ['失败', 'task-status-failed'],
    timeout: ['超时', 'task-status-timeout'],
    cancelled: ['已取消', 'task-status-cancelled'],
    precheck_failed: ['预检未通过', 'task-status-failed']
  };

  function statusBadge(status) {
    var m = STATUS[status] || [status, 'task-status-pending'];
    return '<span class="task-status ' + m[1] + '">' + escapeHtml(m[0]) + '</span>';
  }

  /* ---------- 初始化 ---------- */
  function boot() {
    renderShell();
    loadEngines();
    loadTasks();
  }

  function renderShell() {
    var box = document.getElementById('tasks-content');
    if (!box) return;
    box.innerHTML =
      '<div class="task-form-head">下发任务' +
        '<span class="beta-badge" title="当前为测试版本，功能仍在调整">Beta</span>' +
        '<span class="task-active-config" id="task-active-config"></span></div>' +
      '<div class="task-form">' +
        '<textarea id="task-prompt" class="layui-input task-prompt" rows="4" ' +
          'placeholder="要交给 Agent 的任务描述，例如：审查当前项目的错误处理，列出最值得修的三处"></textarea>' +
        '<div class="task-form-row">' +
          '<label>引擎</label><div id="task-engines" class="task-engine-radios"></div>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>使用模型</label><span id="task-current-model" class="task-current-model">以实测为准</span>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>工作目录</label>' +
          '<button class="layui-btn layui-btn-sm" id="task-pick-dir">选择文件夹…</button>' +
          '<span id="task-workdir-text"></span>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>超时</label>' +
          '<select id="task-timeout" class="layui-input">' +
            '<option value="300">5 分钟</option>' +
            '<option value="600" selected>10 分钟</option>' +
            '<option value="1800">30 分钟</option>' +
            '<option value="3600">60 分钟</option>' +
          '</select>' +
          '<button class="layui-btn layui-btn-normal" id="task-submit">下发任务</button>' +
          '<span class="task-hint">下发前自动检测引擎、服务商连通与端配置一致性</span>' +
        '</div>' +
      '</div>' +
      '<div class="task-queue-head">任务队列' +
        '<button class="layui-btn layui-btn-primary layui-btn-xs" id="task-refresh">刷新</button></div>' +
      '<div id="task-list" class="task-list"><div class="empty-tip">加载中…</div></div>';

    document.getElementById('task-submit').onclick = submitTask;
    document.getElementById('task-refresh').onclick = loadTasks;
    document.getElementById('task-pick-dir').onclick = openDirPicker;
    var engineBox = document.getElementById('task-engines');
    if (engineBox) engineBox.addEventListener('change', updateCurrentModel);
    renderWorkdir();
  }

  function updateCurrentModel() {
    var el = document.getElementById('task-current-model');
    if (!el) return;
    var tool = selectedTool();
    var hit = null;
    for (var i = 0; i < (engines || []).length; i++) {
      if (engines[i].tool === tool) { hit = engines[i]; break; }
    }
    var model = hit && hit.current_model ? hit.current_model : '';
    el.textContent = model || '以实测为准';
    el.classList.toggle('is-empty', !model);
  }

  function renderWorkdir() {
    var el = document.getElementById('task-workdir-text');
    if (!el) return;
    if (!workdir) {
      el.className = 'task-workdir is-empty';
      el.textContent = '未选择 · 引擎在用户主目录执行';
      return;
    }
    el.className = 'task-workdir';
    el.innerHTML = '<span class="task-workdir-path" title="' + escapeHtml(workdir) + '">' +
      escapeHtml(workdir) + '</span>' +
      '<button type="button" class="task-workdir-clear" id="task-clear-dir">清除</button>';
    document.getElementById('task-clear-dir').onclick = function () {
      workdir = '';
      renderWorkdir();
    };
  }

  /* ---------- 目录浏览弹窗 ---------- */
  /* 浏览器打开时 JS 拿不到本机绝对路径、原生对话框只在桌面壳存在，
   * 所以两种形态都走后端列目录（fs.py），保持同一套交互。 */
  function listDirs(path) {
    return fetch('/api/v1/fs/dirs?path=' + encodeURIComponent(path || '')).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(parseError(j, r.status)); },
        function () { throw new Error('请求失败（HTTP ' + r.status + '）'); });
      return r.json();
    });
  }

  function openDirPicker() {
    var state = { path: '', parent: null, home: '', drives: [], dirs: [], truncated: false };

    function render() {
      var box = document.getElementById('dir-list');
      if (!box) return;
      document.getElementById('dir-current').textContent = state.path || '我的电脑';
      document.getElementById('dir-up').disabled = state.parent === null;
      var drives = document.getElementById('dir-drives');
      drives.innerHTML = (state.drives || []).map(function (d) {
        return '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" ' +
          'data-dir-drive="' + escapeHtml(d) + '">' + escapeHtml(d) + '</button>';
      }).join('');
      drives.hidden = !(state.drives || []).length;
      box.innerHTML = (state.dirs || []).length
        ? state.dirs.map(function (d) {
            return '<button type="button" class="dir-item" data-dir-path="' + escapeHtml(d.path) +
              '" title="' + escapeHtml(d.path) + '">' + escapeHtml(d.name) + '</button>';
          }).join('') + (state.truncated ? '<div class="dir-more">子目录过多，仅列出前面部分</div>' : '')
        : '<div class="empty-tip">没有子文件夹</div>';
      box.querySelectorAll('[data-dir-path]').forEach(function (b) {
        b.onclick = function () { load(b.getAttribute('data-dir-path')); };
      });
      drives.querySelectorAll('[data-dir-drive]').forEach(function (b) {
        b.onclick = function () { load(b.getAttribute('data-dir-drive')); };
      });
    }

    function load(p) {
      listDirs(p).then(function (data) {
        state = data;
        render();
      }).catch(function (e) {
        var cur = document.getElementById('dir-current');
        if (cur) cur.textContent = p === '~' ? '用户主目录' : p;
        var box = document.getElementById('dir-list');
        if (box) box.innerHTML = '<div class="empty-tip">读取失败：' + escapeHtml(e.message) + '</div>';
      });
    }

    layer.open({
      type: 1,
      title: '选择工作目录',
      area: ['620px', '520px'],
      btn: ['选择此文件夹', '取消'],
      content: '<div class="dir-picker">' +
        '<div class="dir-bar">' +
          '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" id="dir-up">上级</button>' +
          '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" id="dir-home">主目录</button>' +
          '<span class="dir-current" id="dir-current">加载中…</span>' +
        '</div>' +
        '<div class="dir-drives" id="dir-drives" hidden></div>' +
        '<div class="dir-list" id="dir-list"></div>' +
        '<div class="dir-tip">点文件夹进入下一级；进到目标文件夹后点「选择此文件夹」，' +
          '该文件夹下的内容就是引擎的工作目录</div>' +
      '</div>',
      success: function (layero) {
        layero.find('#dir-up').on('click', function () {
          if (state.parent !== null) load(state.parent);
        });
        layero.find('#dir-home').on('click', function () { load('~'); });
        load(workdir || '~');
      },
      yes: function (index) {
        if (!state.path) { layer.msg('请先进入一个文件夹', { icon: 0 }); return; }
        layer.close(index);
        workdir = state.path;
        renderWorkdir();
      }
    });
  }

  /* ---------- 引擎与方案 ---------- */
  function loadEngines() {
    apiGet('/engines').then(function (data) {
      engines = data.engines || [];
      activeConfig = data.active_config || null;
      var cfg = document.getElementById('task-active-config');
      if (cfg) {
        cfg.textContent = activeConfig
          ? '当前方案：' + activeConfig.name + ' · ' + activeConfig.model
          : '尚未切换生效方案';
        cfg.classList.toggle('is-empty', !activeConfig);
      }
      var box = document.getElementById('task-engines');
      var firstInstalled = -1;
      engines.forEach(function (e, i) { if (firstInstalled === -1 && e.installed) firstInstalled = i; });
      box.innerHTML = engines.map(function (e, i) {
        var dis = e.installed ? '' : ' disabled';
        var cls = e.installed ? '' : ' is-off';
        var tip = e.installed ? (e.version ? ' v' + e.version : '') : '未安装';
        return '<label class="task-engine' + cls + '"' +
          (e.error ? ' title="' + escapeHtml(e.error) + '"' : '') + '>' +
          '<input type="radio" name="task-tool" value="' + e.tool + '" ' +
          (i === firstInstalled ? 'checked' : '') + dis + '>' +
          '<span>' + escapeHtml(e.label) + '</span>' +
          '<em>' + escapeHtml(tip) + '</em></label>';
      }).join('');
      updateCurrentModel();
      renderTasks(lastTaskList || []);
    }).catch(function (e) {
      layer.msg('引擎探测失败：' + e.message, { icon: 2 });
    });
  }

  function selectedTool() {
    var el = document.querySelector('input[name="task-tool"]:checked');
    return el ? el.value : '';
  }

  /* ---------- 下发 ---------- */
  function submitTask() {
    var prompt = (document.getElementById('task-prompt').value || '').trim();
    if (!prompt) { layer.msg('请先填写任务描述', { icon: 2 }); return; }
    var tool = selectedTool();
    if (!tool) { layer.msg('请选择引擎', { icon: 2 }); return; }
    var body = {
      prompt: prompt,
      tool: tool,
      working_dir: workdir || null,
      timeout_seconds: Number(document.getElementById('task-timeout').value)
    };
    var btn = document.getElementById('task-submit');
    btn.disabled = true;
    apiSend('', 'POST', body).then(function (task) {
      btn.disabled = false;
      document.getElementById('task-prompt').value = '';
      layer.msg('任务 #' + task.id + ' 已入队', { icon: 1 });
      loadTasks();
    }).catch(function (e) {
      btn.disabled = false;
      // 预检失败后端不入队、不落列表行，弹窗即全部信息
      layer.alert(e.message, { title: '预检未通过', icon: 2 });
    });
  }

  /* ---------- 队列 ---------- */
  var lastTaskList = [];

  function loadTasks() {
    apiGet('').then(function (list) {
      lastTaskList = list || [];
      announceFinished(lastTaskList);
      renderTasks(lastTaskList);
      syncPolling();
    }).catch(function (e) {
      var box = document.getElementById('task-list');
      if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
      stopPolling();
    });
  }

  function announceFinished(list) {
    var seen = {};
    list.forEach(function (t) {
      seen[t.id] = t.status;
      var prev = lastTasks[t.id];
      if (!firstLoad && prev && (prev === 'pending' || prev === 'running') && t.status !== prev) {
        // 不限当前 tab：用户停在别的页也能收到「任务已结束」的应用内提示
        // （窗口隐藏时的任务栏闪烁由桌面壳经 task_notify 负责）
        layer.msg('任务 #' + t.id + '：' + (STATUS[t.status] ? STATUS[t.status][0] : t.status),
          { icon: t.status === 'success' ? 1 : 2 });
      }
    });
    lastTasks = seen;
    firstLoad = false;
  }

  function renderTasks(list) {
    var box = document.getElementById('task-list');
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '<div class="empty-tip">还没有任务。填写任务描述并点「下发任务」，' +
        '引擎会在后台跑完并留下产出</div>';
      return;
    }
    box.innerHTML = list.map(function (t) {
      var live = t.status === 'pending' || t.status === 'running';
      var reason = t.status === 'precheck_failed' ? (t.error_summary || '') : '';
      return '<div class="task-card" data-id="' + t.id + '">' +
        '<div class="task-card-main">' +
          '<div class="task-card-title">' + statusBadge(t.status) +
            '<span class="target-badge target-' + escapeHtml(t.tool) + '">' + escapeHtml(t.tool_label) + '</span>' +
            '<span class="task-id">#' + t.id + '</span></div>' +
          '<div class="task-card-prompt">' + escapeHtml(t.prompt) + '</div>' +
          '<div class="task-card-meta">' +
            '<span>耗时 ' + fmtDuration(t.duration_seconds) + '</span>' +
            '<span>创建 ' + fmtTime(t.created_at) + '</span>' +
            (t.working_dir ? '<span>目录 ' + escapeHtml(t.working_dir) + '</span>' : '') +
            (t.model ? '<span>模型 ' + escapeHtml(t.model) + '</span>' : '') +
          '</div>' +
          (reason ? '<div class="task-card-reason">' + escapeHtml(reason) + '</div>' : '') +
        '</div>' +
        '<div class="task-card-actions">' +
          (live ? '<button class="layui-btn layui-btn-sm layui-btn-danger layui-btn-primary" data-act="cancel">取消</button>' : '') +
          '<button class="layui-btn layui-btn-sm" data-act="detail">查看产出</button>' +
          (live ? '' : '<button class="layui-btn layui-btn-sm layui-btn-primary" data-act="del">删除</button>') +
        '</div>' +
      '</div>';
    }).join('');

    box.querySelectorAll('[data-act]').forEach(function (btn) {
      btn.onclick = function () {
        var id = Number(btn.closest('.task-card').getAttribute('data-id'));
        var act = btn.getAttribute('data-act');
        if (act === 'cancel') cancelTask(id);
        else if (act === 'del') deleteTask(id);
        else openDetail(id);
      };
    });
  }

  function cancelTask(id) {
    layer.confirm('确定取消任务 #' + id + '？会终止引擎进程。', { title: '取消任务' }, function (index) {
      layer.close(index);
      apiSend('/' + id + '/cancel', 'POST', {}).then(function (r) {
        layer.msg(r.detail || '已取消', { icon: r.cancelled ? 1 : 0 });
        loadTasks();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
  }

  function deleteTask(id) {
    layer.confirm('删除任务 #' + id + ' 及其产出文件？', { title: '删除任务' }, function (index) {
      layer.close(index);
      apiSend('/' + id, 'DELETE', {}).then(function () { loadTasks(); })
        .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
  }

  /* ---------- 详情 ---------- */
  function openDetail(id) {
    // layer 异步内容不会重新居中，必须固定 area（已知坑 layui-layer-offset-gotcha）
    layer.open({
      type: 1,
      title: '任务 #' + id + ' 产出',
      area: ['760px', '640px'],
      content: '<div class="task-detail" id="task-detail-' + id + '"><div class="empty-tip">加载中…</div></div>',
      success: function () {
        apiGet('/' + id).then(function (t) {
          var box = document.getElementById('task-detail-' + id);
          if (!box) return;   // 弹窗已被用户关掉，晚到的响应直接丢弃
          var steps = (t.precheck && t.precheck.steps) || [];
          box.innerHTML =
            '<div class="task-detail-head">' + statusBadge(t.status) +
              '<span>' + escapeHtml(t.tool_label) + '</span>' +
              '<span>耗时 ' + fmtDuration(t.duration_seconds) + '</span>' +
              (t.exit_code != null ? '<span>退出码 ' + t.exit_code + '</span>' : '') +
              (t.output_path ? '<span class="task-output-path">' + escapeHtml(t.output_path) + '</span>' : '') +
            '</div>' +
            (steps.length ? '<div class="task-precheck">' + steps.map(function (s) {
              return '<span class="task-precheck-step ' + (s.ok ? 'is-ok' : 'is-bad') + '" title="' +
                escapeHtml(s.detail || '') + '">' + escapeHtml(s.label) + ' · ' + s.ms + 'ms</span>';
            }).join('') + '</div>' : '') +
            (t.error_summary ? '<div class="task-error">' + escapeHtml(t.error_summary) + '</div>' : '') +
            '<pre class="task-output">' + (escapeHtml(t.output || '（无产出）') +
              (t.output_truncated ? '\n\n…（产出较长，此处仅显示末尾部分，完整内容见产出文件）' : '')) + '</pre>';
        }).catch(function (e) {
          var box = document.getElementById('task-detail-' + id);
          if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
        });
      }
    });
  }

  /* ---------- 轮询：有活干 2s，全部空闲停表；不在任务页也继续，保证完成提醒可达 ---------- */
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(loadTasks, 2000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function syncPolling() {
    var busy = lastTaskList.some(function (t) {
      return t.status === 'pending' || t.status === 'running';
    });
    if (busy) startPolling(); else stopPolling();
  }

  window.addEventListener('main-tab-changed', function (e) {
    active = e.detail.index === 5;
    if (active && !loaded) { loaded = true; boot(); }
    // 已 boot 过就保持轮询判活：切走 tab 不停表，完成提示才能送达
    if (loaded) syncPolling();
  });
});
