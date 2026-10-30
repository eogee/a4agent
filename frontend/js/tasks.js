/* 任务下发（无头 Agent 任务）—— 引擎探测 / 下发 / 队列轮询 / 产出查看。
 * 下发与执行分离：POST 立即返回 task_id，本页只轮询状态与产出。
 * 执行计划（2026-10-30）：同一个表单，执行计划切到「按计划循环」即创建
 * 循环任务（POST/PUT /loops），不填就立即执行（POST /tasks）——循环只是
 * 给任务加一个计划，不是另一类任务。
 */
layui.use(['layer', 'form', 'element', 'laydate'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var laydate = layui.laydate;
  var API = '/api/v1/tasks';
  var LOOP_API = '/api/v1/loops';

  var loaded = false;
  var active = false;
  var pollTimer = null;
  var engines = [];
  var activeConfig = null;
  var workdir = '';         // 已选工作目录，空 = 交给后端回落到用户主目录
  var pickedSkills = [];    // 已选技能（SkillRef + _ends/_visible 渲染元数据，提交时剥掉元数据）
  var lastTasks = {};      // id -> status，用于识别「刚结束」的任务并提示
  var firstLoad = true;

  /* ---------- 执行计划 ---------- */
  var planMode = 'now';       // 'now' 立即执行 | 'scheduled' 按计划循环
  var editingLoop = null;     // 编辑中的循环对象（null = 非编辑）
  var scheduleType = 'daily'; // 计划类型：once | interval | daily | weekly
  // 整词入字典（「周一」…「周日」）：i18n 抽取器会把数组里的单字当成独立文案
  var WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

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
          '<label>技能</label>' +
          '<button class="layui-btn layui-btn-sm" id="task-pick-skills">选择技能…</button>' +
          '<div id="task-skills-chips" class="task-skills-chips"></div>' +
          '<span class="task-skills-hint" id="task-skills-hint">' +
            '<span>已选 </span><span id="task-skills-count">0</span>' +
            '<span> 个 · 任务结束自动移除</span>' +
          '</span>' +
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
          '<label>超时时间</label>' +
          '<div class="layui-form task-timeout-wrap" lay-filter="task-timeout-form">' +
            '<select id="task-timeout">' +
              '<option value="300">5 分钟</option>' +
              '<option value="600" selected>10 分钟</option>' +
              '<option value="1800">30 分钟</option>' +
              '<option value="3600">60 分钟</option>' +
            '</select>' +
          '</div>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>执行计划</label>' +
          '<div class="layui-form task-timeout-wrap" lay-filter="task-plan-form">' +
            '<select id="task-plan" lay-filter="task-plan">' +
              '<option value="now" selected>立即执行</option>' +
              '<option value="scheduled">按计划循环</option>' +
            '</select>' +
          '</div>' +
          '<span class="task-hint" id="task-plan-hint">下发后立即进队列，运行记录出现在下方任务列表</span>' +
        '</div>' +
        '<div class="task-form-row" id="task-plan-name-row" style="display:none">' +
          '<label>计划名称</label>' +
          '<input class="layui-input" id="task-plan-name" style="max-width:320px" ' +
            'placeholder="默认取任务描述开头">' +
          '<span class="task-hint" id="task-edit-hint" style="display:none"></span>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="task-edit-cancel" ' +
            'style="display:none">取消编辑</button>' +
        '</div>' +
        '<div class="task-form-row" id="task-sched-row" style="display:none">' +
          '<label>周期</label>' +
          '<div class="layui-form task-timeout-wrap" lay-filter="task-sched-form">' +
            '<select id="task-sched-type" lay-filter="task-sched-type">' +
              '<option value="interval" selected>按间隔重复</option>' +
              '<option value="daily">每天固定时刻</option>' +
              '<option value="weekly">每周选定几天</option>' +
              '<option value="once">仅一次</option>' +
            '</select>' +
          '</div>' +
          '<div id="task-sched" class="task-sched-fields"></div>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label></label>' +
          '<button class="layui-btn layui-btn-normal" id="task-submit">下发任务</button>' +
          '<span class="task-hint" id="task-submit-hint">下发前自动检测引擎、服务商连通与端配置一致性</span>' +
        '</div>' +
      '</div>' +
      '<div class="task-queue-head">任务队列' +
        '<button class="layui-btn layui-btn-primary layui-btn-xs" id="task-refresh">刷新</button></div>' +
      '<div id="task-list" class="task-list"><div class="empty-tip">加载中…</div></div>';

    document.getElementById('task-submit').onclick = submitTask;
    document.getElementById('task-refresh').onclick = loadTasks;
    document.getElementById('task-pick-dir').onclick = openDirPicker;
    document.getElementById('task-pick-skills').onclick = openSkillPicker;
    document.getElementById('task-edit-cancel').onclick = cancelLoopEdit;
    var engineBox = document.getElementById('task-engines');
    if (engineBox) engineBox.addEventListener('change', function () {
      updateCurrentModel();
      renderSkillChips();   // 引擎切换保留已选，但不兼容项要重新标黄
    });
    /* 探测结果在后端缓存 5 分钟（重装/启动 OpenCode 后不会立刻自愈），
       按钮走 refresh=true 直通重测；内容每次重渲，所以用委托绑定 */
    if (engineBox) engineBox.addEventListener('click', function (ev) {
      if (ev.target && ev.target.id === 'task-redetect') {
        ev.target.disabled = true;
        loadEngines(true);
      }
    });
    renderWorkdir();
    renderSkillChips();
    updatePlanUI();
    form.render('select', 'task-timeout-form');
    form.render('select', 'task-plan-form');
    form.render('select', 'task-sched-form');
  }

  /* select 被 layui 藏起来后元素级 onchange 不可靠，计划/周期切换走
     layui 的 select 事件（同循环表单的既有坑位） */
  form.on('select(task-plan)', function (data) {
    planMode = data.value === 'scheduled' ? 'scheduled' : 'now';
    updatePlanUI();
    if (planMode === 'scheduled') renderSchedFields();
  });

  form.on('select(task-sched-type)', function (data) {
    scheduleType = data.value;
    renderSchedFields();
  });

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
  /* 统一交给公共目录选择组件（dirpicker.js）：桌面壳走系统原生对话框，浏览器
   * 走内置逐级浏览。这里只负责把选中的目录写回 workdir。 */
  function openDirPicker() {
    if (!window.A4DirPicker) {
      layer.msg('目录选择组件未加载，请刷新页面', { icon: 2 });
      return;
    }
    window.A4DirPicker.pick({
      title: '选择工作目录',
      start: workdir || '~',
      tip: '进到目标文件夹后点「选择此文件夹」，该文件夹下的内容就是引擎的工作目录'
    }).then(function (p) {
      if (!p) return;
      workdir = p;
      renderWorkdir();
    });
  }

  /* ---------- 技能选择 ---------- */
  /* 弹层是公共组件（skillpick.js，循环任务页也用同一份）；这里只负责
   * 把结果写回 pickedSkills 并重渲 chip。引擎切换不自动删不兼容项。 */
  function skillCompatible(s, tool) {
    if (!tool) return true;
    if ((s._ends || []).indexOf(tool) >= 0) return true;
    return tool === 'opencode' && !!s._visible;
  }

  function openSkillPicker() {
    if (!window.A4SkillPicker) {
      layer.msg('技能选择组件未加载，请刷新页面', { icon: 2 });
      return;
    }
    window.A4SkillPicker.pick({
      tool: selectedTool(),
      workdir: workdir,
      selected: pickedSkills
    }).then(function (refs) {
      if (refs === null) return;   // 用户取消，保持原状
      pickedSkills = refs;
      renderSkillChips();
    }).catch(function (e) {
      layer.msg('技能列表加载失败：' + e.message, { icon: 2 });
    });
  }

  function renderSkillChips() {
    var box = document.getElementById('task-skills-chips');
    var count = document.getElementById('task-skills-count');
    if (!box) return;
    var tool = selectedTool();
    var warnTip = '该端不会加载此技能';
    box.innerHTML = pickedSkills.map(function (s, i) {
      var ok = skillCompatible(s, tool);
      return '<span class="task-skill-chip' + (ok ? '' : ' is-warn') + '"' +
        (ok ? '' : ' title="' + warnTip + '"') + '>' +
        '<span class="task-skill-chip-name">' + escapeHtml(s.name) + '</span>' +
        '<button type="button" class="task-skill-chip-x" data-x="' + i + '">×</button>' +
      '</span>';
    }).join('');
    box.querySelectorAll('[data-x]').forEach(function (btn) {
      btn.onclick = function () {
        pickedSkills.splice(Number(btn.getAttribute('data-x')), 1);
        renderSkillChips();
      };
    });
    if (count) count.textContent = String(pickedSkills.length);
  }

  /* ---------- 引擎与方案 ---------- */
  function loadEngines(refresh) {
    apiGet('/engines' + (refresh ? '?refresh=true' : '')).then(function (data) {
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
        /* OpenCode 这类走 HTTP 会话的端没有可探测的命令，「不可用」的含义是
           后台服务没在跑，报「未安装」会让明明装了桌面端的用户以为装错了。 */
        var off = e.http ? '服务未运行' : '未安装';
        var tip = e.installed ? (e.version ? ' v' + e.version : '') : off;
        return '<label class="task-engine' + cls + '"' +
          (e.error ? ' title="' + escapeHtml(e.error) + '"' : '') + '>' +
          '<input type="radio" name="task-tool" value="' + e.tool + '" ' +
          (i === firstInstalled ? 'checked' : '') + dis + '>' +
          '<span>' + escapeHtml(e.label) + '</span>' +
          '<em>' + escapeHtml(tip) + '</em></label>';
      }).join('') +
        '<button type="button" class="layui-btn layui-btn-primary layui-btn-xs task-engine-retest" ' +
          'id="task-redetect">重新检测</button>';
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

  /* ---------- 下发 ----------
   * 一张表单两种提交：执行计划 = 立即执行 → POST /tasks（含预检）；
   * 按计划循环（或正在编辑某个计划）→ POST/PUT /loops。
   * 循环只是给任务加一个执行计划，不是另一类任务。 */
  function submitTask() {
    var prompt = (document.getElementById('task-prompt').value || '').trim();
    if (!prompt) { layer.msg('请先填写任务描述', { icon: 2 }); return; }
    var tool = selectedTool();
    if (!tool) { layer.msg('请选择引擎', { icon: 2 }); return; }
    if (planMode === 'scheduled' || editingLoop) submitLoopPlan(prompt, tool);
    else submitNow(prompt, tool);
  }

  function submitNow(prompt, tool) {
    var body = {
      prompt: prompt,
      tool: tool,
      working_dir: workdir || null,
      timeout_seconds: Number(document.getElementById('task-timeout').value)
    };
    // 未选则不传（后端按空处理）；提交只带 SkillRef 契约字段，剥掉 _ends/_visible 渲染元数据
    if (pickedSkills.length) {
      body.skills = pickedSkills.map(skillRefOf);
    }
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

  function skillRefOf(s) {
    return {
      name: s.name,
      source_tool: s.source_tool || '',
      source_scope: s.source_scope || '',
      source_project: s.source_project || '',
      source_path: s.source_path || ''
    };
  }

  function submitLoopPlan(prompt, tool) {
    var nameEl = document.getElementById('task-plan-name');
    var name = ((nameEl && nameEl.value) || '').trim();
    if (!name) name = prompt.slice(0, 20);   // 默认取任务描述开头
    var config = readSchedConfig();
    if (!config) return;
    var body = {
      name: name,
      tool: tool,
      task_content: prompt,
      workdir: workdir,
      schedule_type: scheduleType,
      schedule_config: config,
      timeout_seconds: Number(document.getElementById('task-timeout').value),
      // 合并表单不再暴露这两个开关：新建走默认（不补跑、保存即启用），
      // 编辑保留原值——它们是低频选项，不值得常驻表单
      catch_up: editingLoop ? !!editingLoop.catch_up : false,
      enabled: editingLoop ? !!editingLoop.enabled : true
    };
    if (pickedSkills.length) {
      body.skills = pickedSkills.map(skillRefOf);
    }
    var btn = document.getElementById('task-submit');
    btn.disabled = true;
    loopSend(editingLoop ? '/' + editingLoop.id : '', editingLoop ? 'PUT' : 'POST', body)
      .then(function () {
        btn.disabled = false;
        document.getElementById('task-prompt').value = '';
        if (nameEl) nameEl.value = '';
        var wasEditing = !!editingLoop;
        exitLoopEdit(false);
        layer.msg(wasEditing ? '计划已保存' : '循环任务已创建，到点自动执行', { icon: 1 });
        if (window.A4Loops && window.A4Loops.reload) window.A4Loops.reload();
      })
      .catch(function (e) {
        btn.disabled = false;
        layer.alert(e.message, { title: '保存失败', icon: 2 });
      });
  }

  /* ---------- 执行计划：显隐 / 周期字段 / 编辑模式 ---------- */
  function loopSend(path, method, body) {
    return fetch(LOOP_API + path, {
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

  /* 周期字段随类型整体重绘：四种计划的输入形态完全不同，混在一个
     表单里要靠显隐切换，反而更容易漏校验（同循环表单的既有决策）。
     时刻/日期时间一律用 laydate 而不是原生 <input type="time|datetime-local">：
     原生控件的面板由浏览器渲染（Chrome 蓝色高亮、图标贴右缘、直角），
     无法与本站主题统一；laydate 面板走 layui 主题（圆角样式见 style.css）。 */
  function renderSchedFields() {
    var box = document.getElementById('task-sched');
    if (!box) return;
    var cfg = editingLoop && editingLoop.schedule_config ? editingLoop.schedule_config : {};
    var html = '';
    if (scheduleType === 'once') {
      html = '<div class="loop-form-row">' +
        '<input class="layui-input task-sched-time" id="task-sched-once" readonly ' +
        'placeholder="请选择执行时间" value="' + (cfg.run_at || '') + '" style="max-width:220px">' +
        '<span class="task-hint">应用关着时错过的触发不会补跑</span></div>';
    } else if (scheduleType === 'interval') {
      html = '<div class="loop-form-row">' +
        '<input class="layui-input" id="task-sched-interval" type="number" min="1" max="10080" ' +
        'value="' + (cfg.minutes || 30) + '" style="max-width:120px">' +
        '<span class="task-hint">分钟一轮（上一轮结束或跳过后才计下一轮）</span></div>';
    } else {
      var timeInput = '<input class="layui-input task-sched-time" id="task-sched-time" readonly ' +
        'placeholder="请选择时刻" value="' + (cfg.time || '') + '" style="max-width:120px">';
      if (scheduleType === 'daily') {
        html = '<div class="loop-form-row">' + timeInput +
          '<span class="task-hint">每天到这个点执行</span></div>';
      } else {
        var days = cfg.weekdays || [1];
        var wd = WEEKDAYS.map(function (name, i) {
          var v = i + 1;
          return '<label class="loop-weekday"><input type="checkbox" name="task-sched-weekday" value="' + v + '"' +
            (days.indexOf(v) >= 0 ? ' checked' : '') + '>' + name + '</label>';
        }).join('');
        html = '<div class="loop-weekdays">' + wd + '</div>' +
          '<div class="loop-form-row" style="margin-top:8px">' + timeInput +
          '<span class="task-hint">选中星期到这个点执行</span></div>';
      }
    }
    box.innerHTML = html;
    if (scheduleType === 'once') {
      laydate.render({
        elem: '#task-sched-once',
        type: 'datetime',
        format: 'yyyy-MM-dd HH:mm',   // 后端 schedule_config.run_at 的口径（无秒）
        theme: '#16baaa'
      });
    } else if (scheduleType === 'daily' || scheduleType === 'weekly') {
      laydate.render({
        elem: '#task-sched-time',
        type: 'time',
        format: 'HH:mm',       // 后端 schedule_config.time 的口径
        theme: '#16baaa'       // 与全站主题色一致
      });
    }
  }

  function readSchedConfig() {
    if (scheduleType === 'once') {
      var v = document.getElementById('task-sched-once').value;
      if (!v) { layer.msg('请选择执行时间', { icon: 2 }); return null; }
      return { run_at: v };   // laydate 已按 yyyy-MM-dd HH:mm 写回，无 T 可替
    }
    if (scheduleType === 'interval') {
      var mins = Number(document.getElementById('task-sched-interval').value);
      if (!mins || mins < 1) { layer.msg('间隔至少 1 分钟', { icon: 2 }); return null; }
      return { minutes: Math.floor(mins) };
    }
    var time = document.getElementById('task-sched-time').value;
    if (!time) { layer.msg('请选择时刻', { icon: 2 }); return null; }
    if (scheduleType === 'daily') return { time: time };
    var days = [];
    document.querySelectorAll('input[name="task-sched-weekday"]:checked').forEach(function (cb) {
      days.push(Number(cb.value));
    });
    if (!days.length) { layer.msg('每周计划至少要选一天', { icon: 2 }); return null; }
    return { weekdays: days.sort(), time: time };
  }

  /* 超时上限两种模式不同（后端 schema：手动 le=3600，循环 le=14400），
     按模式重建 options；原值不在新集合里就回落 10 分钟 */
  function renderTimeoutOptions() {
    var sel = document.getElementById('task-timeout');
    if (!sel) return;
    var cur = sel.value;
    var opts = planMode === 'scheduled'
      ? [['300', '5 分钟'], ['600', '10 分钟'], ['1800', '30 分钟'], ['3600', '60 分钟'],
         ['7200', '2 小时'], ['14400', '4 小时']]
      : [['300', '5 分钟'], ['600', '10 分钟'], ['1800', '30 分钟'], ['3600', '60 分钟']];
    var keep = opts.some(function (o) { return o[0] === cur; });
    sel.innerHTML = opts.map(function (o) {
      return '<option value="' + o[0] + '"' + (!keep && o[0] === '600' ? ' selected' :
        (keep && o[0] === cur ? ' selected' : '')) + '>' + o[1] + '</option>';
    }).join('');
    form.render('select', 'task-timeout-form');
  }

  function updatePlanUI() {
    var planSel = document.getElementById('task-plan');
    var nameRow = document.getElementById('task-plan-name-row');
    var schedRow = document.getElementById('task-sched-row');
    var hint = document.getElementById('task-plan-hint');
    var submitHint = document.getElementById('task-submit-hint');
    var submitBtn = document.getElementById('task-submit');
    var editHint = document.getElementById('task-edit-hint');
    var editCancel = document.getElementById('task-edit-cancel');
    if (!planSel) return;
    if (editingLoop) planSel.value = 'scheduled';
    var scheduled = planSel.value === 'scheduled';
    planMode = scheduled ? 'scheduled' : 'now';
    if (nameRow) nameRow.style.display = scheduled ? '' : 'none';
    if (schedRow) schedRow.style.display = scheduled ? '' : 'none';
    if (hint) hint.textContent = scheduled
      ? '保存为计划，到点自动执行；应用开着才会触发'
      : '下发后立即进队列，运行记录出现在下方任务列表';
    if (submitHint) submitHint.textContent = scheduled
      ? '保存后到点自动执行；引擎以无头模式获得完全权限，请只把计划交给可信的工作目录'
      : '下发前自动检测引擎、服务商连通与端配置一致性';
    if (submitBtn) submitBtn.textContent = editingLoop ? '保存修改' : (scheduled ? '保存计划' : '下发任务');
    if (editHint) {
      editHint.style.display = editingLoop ? '' : 'none';
      if (editingLoop) editHint.textContent = '正在编辑计划 #' + editingLoop.id + ' · ' + editingLoop.name;
    }
    if (editCancel) editCancel.style.display = editingLoop ? '' : 'none';
    if (planSel.disabled !== !!editingLoop) {
      planSel.disabled = !!editingLoop;   // 编辑中的计划不能就地改成「立即执行」
    }
    /* 代码改过原生 value/disabled 后必须重渲染：layui 下拉头显示的是它
       自己缓存的文本，不同步就会出现「显示立即执行、实际保存计划」 */
    form.render('select', 'task-plan-form');
    renderTimeoutOptions();
  }

  /* loops.js 的「编辑」按钮走这里：把循环灌回主表单（进编辑模式） */
  function startLoopEdit(loop) {
    if (!loop) return;
    editingLoop = loop;
    scheduleType = loop.schedule_type || 'interval';
    workdir = loop.workdir || '';
    pickedSkills = (loop.skills || []).map(function (s) {
      return {
        name: s.name, source_tool: s.source_tool || '',
        source_scope: s.source_scope || '', source_project: s.source_project || '',
        source_path: s.source_path || ''
      };
    });
    document.getElementById('task-prompt').value = loop.task_content || '';
    document.getElementById('task-plan-name').value = loop.name || '';
    var toolRadio = document.querySelector('input[name="task-tool"][value="' + loop.tool + '"]');
    if (toolRadio && !toolRadio.disabled) toolRadio.checked = true;
    updateCurrentModel();
    renderWorkdir();
    renderSkillChips();
    updatePlanUI();
    renderSchedFields();
    document.getElementById('task-timeout').value = String(loop.timeout_seconds || 1800);
    form.render('select', 'task-timeout-form');
    var typeSel = document.getElementById('task-sched-type');
    if (typeSel) { typeSel.value = scheduleType; form.render('select', 'task-sched-form'); }
    var promptEl = document.getElementById('task-prompt');
    if (promptEl) promptEl.focus();
  }

  /* 保存计划成功后退出编辑态：保持计划态方便连建（取消编辑走 cancelLoopEdit） */
  function exitLoopEdit() {
    editingLoop = null;
    var planSel = document.getElementById('task-plan');
    if (planSel) planSel.value = 'scheduled';
    syncSchedType();
    updatePlanUI();
    renderSchedFields();
  }

  function cancelLoopEdit() {
    editingLoop = null;
    scheduleType = 'interval';
    var planSel = document.getElementById('task-plan');
    if (planSel) planSel.value = 'now';   // 取消 = 放弃这次编辑，回到立即执行
    document.getElementById('task-prompt').value = '';
    var nameEl = document.getElementById('task-plan-name');
    if (nameEl) nameEl.value = '';
    workdir = '';
    pickedSkills = [];
    syncSchedType();
    updatePlanUI();
    renderSchedFields();
    renderWorkdir();
    renderSkillChips();
  }

  /* loops.js 的「新建循环任务」按钮走这里：切到计划态清空表单 */
  function startNewLoop() {
    editingLoop = null;
    scheduleType = 'interval';
    workdir = '';
    pickedSkills = [];
    document.getElementById('task-prompt').value = '';
    var planSel = document.getElementById('task-plan');
    if (planSel) planSel.value = 'scheduled';
    syncSchedType();
    renderWorkdir();
    renderSkillChips();
    updatePlanUI();
    renderSchedFields();
    document.getElementById('task-prompt').focus();
  }

  /* 类型下拉的显示值与 scheduleType 同步（layui select 改 value 后要重渲染） */
  function syncSchedType() {
    var typeSel = document.getElementById('task-sched-type');
    if (!typeSel) return;
    typeSel.value = scheduleType;
    form.render('select', 'task-sched-form');
  }

  window.A4TaskForm = {
    editLoop: startLoopEdit,
    newLoop: startNewLoop
  };

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
          var names = (t.skills || []).map(function (s) { return s.name; }).join('、');
          var links = t.skill_links || {};
          // 非阻断提示（运行期存在，任务结束随 skill_links 清空）：静态标签各自成
          // 独立节点进字典，后端给的名单/原因属数据，不参与翻译
          var skillNotes = '';
          if (links.skipped_existing && links.skipped_existing.length) {
            skillNotes += '<div class="task-skill-note"><span>同名技能已存在，使用已有版本：</span><span>' +
              escapeHtml(links.skipped_existing.join('、')) + '</span></div>';
          }
          if (links.skipped_invalid && links.skipped_invalid.length) {
            skillNotes += '<div class="task-skill-note"><span>未能注入：</span><span>' +
              escapeHtml(links.skipped_invalid.map(function (x) {
                return x.name + (x.reason ? '（' + x.reason + '）' : '');
              }).join('；')) + '</span></div>';
          }
          if (links.notices && links.notices.length) {
            skillNotes += '<div class="task-skill-note"><span>提示：</span><span>' +
              escapeHtml(links.notices.map(function (x) {
                return x.name + (x.message ? '：' + x.message : '');
              }).join('；')) + '</span></div>';
          }
          box.innerHTML =
            '<div class="task-detail-head">' + statusBadge(t.status) +
              '<span>' + escapeHtml(t.tool_label) + '</span>' +
              '<span>耗时 ' + fmtDuration(t.duration_seconds) + '</span>' +
              (t.exit_code != null ? '<span>退出码 ' + t.exit_code + '</span>' : '') +
              (t.output_path ? '<span class="task-output-path">' + escapeHtml(t.output_path) + '</span>' : '') +
            '</div>' +
            ((t.skills && t.skills.length) ?
              '<div class="task-detail-skills">' + '技能 ' + t.skills.length + ' 个：' + escapeHtml(names) + '</div>' : '') +
            skillNotes +
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
