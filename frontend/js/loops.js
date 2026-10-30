/* 循环管理 —— 定时/间隔拉起无头 Agent 任务的循环定义、运行历史与日志。
 *
 * 定义与执行分离：本页只管「定义 + 看状态」，到点由后端调度线程投递，
 * 运行记录复用「任务下发」的表与执行器（AgentTask.loop_id），因此超时、
 * 杀进程树、日志落盘、终态通知全部与手动任务一致。
 *
 * 权限口径（2026-10-25 与用户决议）：无头执行只有完全信任一种模式，与
 * 「任务下发」相同；表单里不放权限选择，只做风险明示。
 */
layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var API = '/api/v1/loops';
  var TASK_API = '/api/v1/tasks';

  var loaded = false;
  var active = false;
  var pollTimer = null;
  var cdTimer = null;
  var loops = [];
  var engines = [];
  var activeConfig = null;
  var draft = null;          // 弹层草稿：null = 新建
  var pickedSkills = [];     // 弹层已选技能（SkillRef + _ends/_visible 渲染元数据，提交时剥掉）
  var lastRunning = {};      // loop id -> 是否在进行中，用于「刚结束」提示
  var firstLoad = true;

  // 整词入字典（"周一"…"周日"），不要拆成单字：i18n 抽取器会把数组里的
  // 单字当成独立文案，报一批永远命不中的未译词
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
      return r.json();
    });
  }

  function fmtTime(s) { return s ? String(s).slice(5, 16) : '—'; }

  function fmtDuration(sec) {
    if (sec == null) return '—';
    if (sec < 60) return sec + ' 秒';
    return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒';
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function localDTValue(d) {
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  /* 后端给的是本地时间的 naive 字符串（"2026-10-26 09:00:00"），
     不能直接 new Date(s)——Safari/旧 WebView 会得到 Invalid Date */
  function parseServerTime(s) {
    if (!s) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(s);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }

  function countdownText(target) {
    var diff = Math.floor((target.getTime() - Date.now()) / 1000);
    if (diff <= 0) return '即将执行';
    if (diff < 60) return diff + ' 秒后';
    var mins = Math.ceil(diff / 60);
    if (mins < 60) return mins + ' 分后';
    var hrs = Math.floor(mins / 60);
    var rest = mins % 60;
    return rest ? hrs + ' 小时 ' + rest + ' 分后' : hrs + ' 小时后';
  }

  /* 运行状态徽章：任务链的七种终态 + 调度层两种「没跑」记录 */
  var RUN_STATUS = {
    pending: ['排队中', 'task-status-pending'],
    running: ['执行中', 'task-status-running'],
    success: ['已完成', 'task-status-success'],
    failed: ['失败', 'task-status-failed'],
    timeout: ['超时', 'task-status-timeout'],
    cancelled: ['已取消', 'task-status-cancelled'],
    precheck_failed: ['预检未通过', 'task-status-failed'],
    skipped: ['已跳过', 'task-status-cancelled'],
    missed: ['已错过', 'task-status-cancelled']
  };

  function runBadge(status) {
    var m = RUN_STATUS[status] || [status, 'task-status-pending'];
    return '<span class="task-status ' + m[1] + '">' + escapeHtml(m[0]) + '</span>';
  }

  /* 计划描述由前端按结构化字段拼，不走后端文本：i18n 只认「含中文的独立
     文本节点」，后端一句中文整串在任何模式下的英文界面都是漏译。每段各自
     成节点（星期、顿号、时刻分开），通配规则才能逐段命中。 */
  function listSep() {
    return (window.i18n && window.i18n.lang === 'en') ? ', ' : '、';
  }

  function scheduleLine(l) {
    var cfg = l.schedule_config || {};
    if (l.schedule_type === 'once') return escapeHtml(cfg.run_at) + ' 执行一次';
    if (l.schedule_type === 'interval') {
      var m = Number(cfg.minutes || 0);
      if (m && m % 1440 === 0) return '每 ' + (m / 1440) + ' 天';
      if (m && m % 60 === 0) return '每 ' + (m / 60) + ' 小时';
      return '每 ' + m + ' 分钟';
    }
    if (l.schedule_type === 'daily') return '每天 ' + escapeHtml(cfg.time);
    // 每周：首个星期带「每周」前缀（中英文语序不同，前缀必须跟着首日走），
    // 其余星期独立成节点，各自命中字典。WEEKDAYS 是「周一」这类整词，
    // 这里只取日字符（slice(1)），否则会拼出「每周周一」
    var days = (cfg.weekdays || []).map(Number).filter(function (d) { return d >= 1 && d <= 7; });
    if (!days.length) return '—';
    var head = '<span class="loop-wd">每周' + WEEKDAYS[days[0] - 1].slice(1) + '</span>';
    var rest = days.slice(1).map(function (d) {
      return '<span class="loop-wd">' + WEEKDAYS[d - 1] + '</span>';
    }).join(listSep());
    return head + (rest ? listSep() + rest : '') + ' ' + escapeHtml(cfg.time);
  }

  function triggerBadge(t) {
    return '<span class="loop-run-trigger">' +
      (t === 'manual' ? '手动' : '自动') + '</span>';
  }

  /* ---------- 外壳 ---------- */
  function renderShell() {
    var box = document.getElementById('loops-content');
    if (!box) return;
    box.innerHTML =
      '<div class="loops-header">' +
        '<span class="loops-title">循环任务</span>' +
        '<span class="loops-sched" id="loops-sched">加载中…</span>' +
        '<span class="loops-header-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="loops-refresh">刷新</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" id="loops-add">新建循环任务</button>' +
        '</span>' +
      '</div>' +
      '<div id="loops-list" class="loops-list"><div class="empty-tip">加载中…</div></div>';
    document.getElementById('loops-refresh').onclick = loadLoops;
    document.getElementById('loops-add').onclick = function () { openForm(null); };
  }

  /* ---------- 列表 ---------- */
  function loadLoops() {
    apiGet('').then(function (data) {
      loops = data.loops || [];
      activeConfig = data.active_config || null;
      renderScheduler(data.scheduler || {});
      renderLoops();
      announceFinished();
      syncPolling();
    }).catch(function (e) {
      var box = document.getElementById('loops-list');
      if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function renderScheduler(s) {
    var el = document.getElementById('loops-sched');
    if (!el) return;
    var text = s.running
      ? '调度中 · 每 ' + Math.round(s.scan_interval || 20) + ' 秒扫描'
      : '调度已停止';
    el.textContent = text;
    el.classList.toggle('is-on', !!s.running);
  }

  function renderLoops() {
    var box = document.getElementById('loops-list');
    if (!box) return;
    if (!loops.length) {
      box.innerHTML = '<div class="empty-tip">还没有循环任务。点「新建循环任务」让 Agent ' +
        '按计划自动执行，应用开着才会触发</div>';
      return;
    }
    box.innerHTML = loops.map(function (l) {
      var last = l.last_run;
      var lastText = last
        ? '最近 ' + (RUN_STATUS[last.status] ? RUN_STATUS[last.status][0] : last.status) +
          ' · ' + fmtTime(last.finished_at)
        : '尚未运行';
      var lastBad = last && last.status !== 'success' && last.status !== 'skipped' &&
        last.status !== 'missed';
      var meta = [];
      if (l.workdir) meta.push('目录 ' + l.workdir);
      meta.push('超时 ' + Math.round(l.timeout_seconds / 60) + ' 分钟');
      if (l.catch_up) meta.push('启动时补跑');
      meta.push('<span class="' + (lastBad ? 'loop-last-bad' : '') + '">' + lastText + '</span>');

      var cd = '';
      if (l.status === 'completed') cd = '已完成';
      else if (!l.enabled) cd = '已停用';
      else if (l.next_run_at) cd = countdownText(parseServerTime(l.next_run_at));
      var stateNode = cd ? ' · <span class="loop-state">' + cd + '</span>' : '';
      var cdNode = (!cd && l.next_run_at && l.status !== 'completed')
        ? '（<span class="loop-cd">' + countdownText(parseServerTime(l.next_run_at)) + '</span>）'
        : '';

      return '<div class="loop-card' + (l.status === 'completed' ? ' is-completed' : '') +
        (!l.enabled && l.status !== 'completed' ? ' is-paused' : '') + '" data-id="' + l.id + '">' +
        '<div class="loop-card-main">' +
          '<div class="loop-card-title">' +
            '<span class="target-badge target-' + escapeHtml(l.tool) + '">' +
              escapeHtml(l.tool_label) + '</span>' +
            '<span class="loop-name">' + escapeHtml(l.name) + '</span>' +
            (l.running ? '<span class="task-status task-status-running">执行中</span>' : '') +
            '<span class="task-id">#' + l.id + '</span>' +
          '</div>' +
          '<div class="loop-card-sched">' + scheduleLine(l) +
            (l.next_run_at && l.status !== 'completed'
              ? ' <span class="loop-next">· 下次运行</span> ' + fmtTime(l.next_run_at) + cdNode
              : '') + stateNode +
          '</div>' +
          '<div class="loop-card-task">' + escapeHtml(l.task_content) + '</div>' +
          '<div class="loop-card-meta">' + meta.map(function (m) {
            return '<span>' + m + '</span>';
          }).join('') + '</div>' +
          (lastBad && last.error_summary
            ? '<div class="task-card-reason">' + escapeHtml(last.error_summary) + '</div>' : '') +
        '</div>' +
        '<div class="loop-card-actions">' +
          '<div class="layui-form loop-card-switch" lay-filter="loop-card">' +
            '<input type="checkbox" lay-skin="switch" ' +
            'lay-filter="loop-toggle" data-id="' + l.id + '"' +
            (l.enabled ? ' checked' : '') + '></div>' +
          '<button class="layui-btn layui-btn-sm" data-act="run">立即执行</button>' +
          '<button class="layui-btn layui-btn-sm" data-act="history">运行历史</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" data-act="edit">编辑</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-danger layui-btn-primary" data-act="del">删除</button>' +
        '</div>' +
      '</div>';
    }).join('');
    /* 只渲染本页卡片里的开关：form.render('switch') 这个 layui 版本不认
       （switch 是 checkbox 渲染器内部按 lay-skin 分发的），且不带 filter
       会把全页复选框重绘一遍 */
    form.render('checkbox', 'loop-card');

    box.querySelectorAll('[data-act]').forEach(function (btn) {
      btn.onclick = function () {
        var id = Number(btn.closest('.loop-card').getAttribute('data-id'));
        var act = btn.getAttribute('data-act');
        if (act === 'run') runNow(id);
        else if (act === 'edit') editLoop(id);
        else if (act === 'del') deleteLoop(id);
        else openHistory(id);
      };
    });
  }

  /* 倒计时秒表：只改文本，不重绘列表（1s 一次，重绘会打断用户操作）。
     括号在 span 外，秒表只动 span 里的文字，i18n 通配规则才命得中。 */
  function renderCountdowns() {
    if (!active) return;
    loops.forEach(function (l) {
      var card = document.querySelector('.loop-card[data-id="' + l.id + '"]');
      if (!card) return;
      var span = card.querySelector('.loop-cd');
      if (!span) return;
      var target = parseServerTime(l.next_run_at);
      if (!target) return;
      span.textContent = countdownText(target);
    });
  }

  function announceFinished() {
    var seen = {};
    loops.forEach(function (l) {
      seen[l.id] = l.running;
      var prev = lastRunning[l.id];
      if (!firstLoad && prev === true && l.running === false) {
        var last = l.last_run || {};
        layer.msg('循环 #' + l.id + '（' + l.name + '）：' +
          (RUN_STATUS[last.status] ? RUN_STATUS[last.status][0] : (last.status || '已结束')),
          { icon: last.status === 'success' ? 1 : 2 });
      }
    });
    lastRunning = seen;
    firstLoad = false;
  }

  /* ---------- 卡片动作 ---------- */
  function toggleLoop(id, on) {
    apiSend('/' + id + (on ? '/enable' : '/disable'), 'POST', {}).then(function () {
      layer.msg(on ? '循环 #' + id + ' 已启用' : '循环 #' + id + ' 已停用', { icon: 1 });
      loadLoops();
    }).catch(function (e) {
      layer.msg(e.message, { icon: 2 });
      loadLoops();  // 失败后重拉，把开关恢复成真实状态
    });
  }

  function runNow(id) {
    apiSend('/' + id + '/run', 'POST', {}).then(function (task) {
      layer.msg('循环 #' + id + ' 已立即执行（任务 #' + task.id + '）', { icon: 1 });
      loadLoops();
    }).catch(function (e) {
      // 预检失败与上一轮未结束都是可读原因，弹窗即全部信息
      layer.alert(e.message, { title: '无法立即执行', icon: 2 });
    });
  }

  function deleteLoop(id) {
    var loop = null;
    loops.forEach(function (l) { if (l.id === id) loop = l; });
    layer.confirm('删除循环「' + (loop ? loop.name : '#' + id) + '」及其全部运行记录？', {
      title: '删除循环任务'
    }, function (index) {
      layer.close(index);
      apiSend('/' + id, 'DELETE', {}).then(function (r) {
        layer.msg('已删除，含 ' + (r.runs_deleted || 0) + ' 条运行记录', { icon: 1 });
        loadLoops();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
  }

  function editLoop(id) {
    var loop = null;
    loops.forEach(function (l) { if (l.id === id) loop = l; });
    openForm(loop);
  }

  /* ---------- 新建 / 编辑弹层 ---------- */
  function openForm(loop) {
    draft = loop
      ? {
          id: loop.id, name: loop.name, tool: loop.tool,
          task_content: loop.task_content, workdir: loop.workdir,
          schedule_type: loop.schedule_type,
          schedule_config: loop.schedule_config || {},
          timeout_seconds: loop.timeout_seconds,
          catch_up: loop.catch_up, enabled: loop.enabled,
          skills: Array.isArray(loop.skills) ? loop.skills.slice() : []
        }
      : {
          id: null, name: '', tool: '', task_content: '', workdir: '',
          schedule_type: 'interval', schedule_config: { minutes: 30 },
          timeout_seconds: 1800, catch_up: false, enabled: true,
          skills: []
        };
    // 编辑回填：服务端快照只有契约五字段，先按可兼容处理（无渲染元数据不误标黄）
    pickedSkills = draft.skills.map(function (s) {
      return {
        name: s.name, source_tool: s.source_tool || '',
        source_scope: s.source_scope || '', source_project: s.source_project || '',
        source_path: s.source_path || ''
      };
    });
    if (!draft.tool) {
      for (var i = 0; i < engines.length; i++) {
        if (engines[i].installed) { draft.tool = engines[i].tool; break; }
      }
    }
    layer.open({
      type: 1,
      title: loop ? '编辑循环任务' : '新建循环任务',
      area: ['620px', '680px'],
      content: '<div class="loop-form" id="loop-form"><div class="empty-tip">加载中…</div></div>',
      success: function () {
        // 等引擎探测回来再画表单：否则引擎单选是空的，用户存不下
        loadEngines().then(renderForm).catch(function (e) {
          var box = document.getElementById('loop-form');
          if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
        });
      }
    });
  }

  function renderForm() {
    var box = document.getElementById('loop-form');
    if (!box) return;
    var d = draft;
    var cfg = d.schedule_config || {};
    var engineHtml = engines.map(function (e) {
      var dis = e.installed ? '' : ' disabled';
      var cls = e.installed ? '' : ' is-off';
      var tip = e.installed ? (e.version ? ' v' + e.version : '') : '未安装';
      return '<label class="task-engine' + cls + '">' +
        '<input type="radio" name="loop-tool" value="' + e.tool + '"' +
        (e.tool === d.tool ? ' checked' : '') + dis + '>' +
        '<span>' + escapeHtml(e.label) + '</span>' +
        '<em>' + escapeHtml(tip) + '</em></label>';
    }).join('');

    /* 注意：不要把整个表单包进 layui-form——form.render(null) 会顺带藏掉所有
       原生单选/复选框（layui 对它们也有自己的渲染）。只有 select 需要 layui
       渲染，单独包一层（与「任务下发」的 timeout 选择同一做法）。 */
    box.innerHTML =
      '<div class="loop-form-body">' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">名称</label>' +
          '<div class="layui-input-block">' +
            '<input class="layui-input" id="loop-f-name" value="' + escapeHtml(d.name) +
              '" placeholder="如：每日巡检">' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">引擎</label>' +
          '<div class="layui-input-block"><div class="loop-form-engines">' + engineHtml + '</div></div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">技能</label>' +
          '<div class="layui-input-block loop-form-row">' +
            '<button class="layui-btn layui-btn-sm" id="loop-f-skills">选择技能…</button>' +
            '<div id="loop-skills-chips" class="task-skills-chips"></div>' +
            '<span class="task-skills-hint">' +
              '<span>已选 </span><span id="loop-skills-count">0</span>' +
              '<span> 个 · 任务结束自动移除</span>' +
            '</span>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">任务内容</label>' +
          '<div class="layui-input-block">' +
            '<textarea class="layui-input" id="loop-f-content" rows="4" ' +
              'placeholder="每轮要交给 Agent 的任务，例如：检查昨天的报错日志并汇总到报告">' +
              escapeHtml(d.task_content) + '</textarea>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">工作目录</label>' +
          '<div class="layui-input-block loop-form-row">' +
            '<button class="layui-btn layui-btn-sm" id="loop-f-pick">选择文件夹…</button>' +
            '<span id="loop-f-dir-text" class="task-workdir' + (d.workdir ? '' : ' is-empty') + '">' +
              (d.workdir ? escapeHtml(d.workdir) : '未选择 · 引擎在用户主目录执行') + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">触发计划</label>' +
          '<div class="layui-input-block">' +
            '<div class="layui-form loop-select-wrap" lay-filter="loop-form">' +
              '<select id="loop-f-type" lay-filter="loop-type">' +
                '<option value="interval">按间隔重复</option>' +
                '<option value="daily">每天固定时刻</option>' +
                '<option value="weekly">每周选定几天</option>' +
                '<option value="once">仅一次</option>' +
              '</select>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label"></label>' +
          '<div class="layui-input-block" id="loop-f-sched"></div>' +
        '</div>' +
        '<div class="layui-form-item loop-form-adv">' +
          '<label class="layui-form-label">高级</label>' +
          '<div class="layui-input-block">' +
            '<div class="loop-form-risk">循环任务与「任务下发」相同，以无头模式执行：引擎会自动获得完全权限（跳过交互确认），请只把循环任务交给可信的工作目录。</div>' +
            '<div class="loop-form-row">' +
              '<div class="layui-form loop-select-wrap loop-select-timeout" lay-filter="loop-form">' +
                '<select id="loop-f-timeout">' +
                  '<option value="300">超时 5 分钟</option>' +
                  '<option value="600">超时 10 分钟</option>' +
                  '<option value="1800" selected>超时 30 分钟</option>' +
                  '<option value="3600">超时 60 分钟</option>' +
                  '<option value="7200">超时 2 小时</option>' +
                  '<option value="14400">超时 4 小时</option>' +
                '</select>' +
              '</div>' +
              '<label class="loop-weekday"><input type="checkbox" id="loop-f-catchup"' +
                (d.catch_up ? ' checked' : '') + '>启动时补跑错过的触发</label>' +
              '<label class="loop-weekday"><input type="checkbox" id="loop-f-enabled"' +
                (d.enabled ? ' checked' : '') + '>保存后立即启用</label>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="loop-form-actions">' +
          '<button class="layui-btn layui-btn-primary" id="loop-f-cancel">取消</button>' +
          '<button class="layui-btn layui-btn-normal" id="loop-f-save">保存</button>' +
        '</div>' +
      '</div>';

    document.getElementById('loop-f-pick').onclick = pickWorkdir;
    document.getElementById('loop-f-skills').onclick = openSkillPicker;
    var engineBox = document.querySelector('.loop-form-engines');
    if (engineBox) engineBox.addEventListener('change', function () {
      renderSkillChips();   // 引擎切换保留已选，但不兼容项要重新标黄
    });
    document.getElementById('loop-f-cancel').onclick = function () { layer.closeAll(); };
    document.getElementById('loop-f-save').onclick = submitForm;
    /* 计划类型切换走 layui 的 select 事件：原生 select 被 form.render 藏起来，
       元素级 onchange 在用户点选 layui 下拉项时不一定触发 */

    document.getElementById('loop-f-type').value = d.schedule_type;
    renderScheduleFields();
    document.getElementById('loop-f-timeout').value = String(d.timeout_seconds);
    renderSkillChips();
    form.render(null, 'loop-form');
  }

  /* ---------- 技能选择 ---------- */
  /* 弹层是公共组件（skillpick.js，与「任务下发」共用同一份）；这里只负责
   * 把结果写回 pickedSkills 并重渲 chip。引擎切换不自动删不兼容项。 */
  function selectedTool() {
    var el = document.querySelector('input[name="loop-tool"]:checked');
    return el ? el.value : '';
  }

  function skillCompatible(s, tool) {
    if (!tool) return true;
    // 编辑回填的快照没有 _ends/_visible 渲染元数据，无从判断兼容性，不误标黄
    if (typeof s._ends === 'undefined' && typeof s._visible === 'undefined') return true;
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
      workdir: draft.workdir || '',
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
    var box = document.getElementById('loop-skills-chips');
    var count = document.getElementById('loop-skills-count');
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

  /* 触发计划字段随类型整体重绘：四种计划的输入形态完全不同，混在一个
     表单里要靠显隐切换，反而更容易漏校验 */
  function renderScheduleFields() {
    var box = document.getElementById('loop-f-sched');
    if (!box) return;
    var type = draft.schedule_type;
    var cfg = draft.schedule_config || {};
    var html = '';
    if (type === 'once') {
      var at = cfg.run_at ? String(cfg.run_at).replace(' ', 'T') : localDTValue(new Date(Date.now() + 5 * 60000));
      html = '<div class="loop-form-row">' +
        '<input class="layui-input" id="loop-f-once" type="datetime-local" value="' + at + '" ' +
        'style="max-width:220px">' +
        '<span class="loop-form-risk" style="margin:0">应用关着时错过的触发不会补跑</span></div>';
    } else if (type === 'interval') {
      html = '<div class="loop-form-row">' +
        '<input class="layui-input" id="loop-f-interval" type="number" min="1" max="10080" ' +
        'value="' + (cfg.minutes || 30) + '" style="max-width:120px">' +
        '<span>分钟一轮（上一轮结束或跳过后才计下一轮）</span></div>';
    } else if (type === 'daily') {
      html = '<div class="loop-form-row">' +
        '<input class="layui-input" id="loop-f-time" type="time" value="' + (cfg.time || '09:00') +
        '" style="max-width:120px">' +
        '<span>每天到这个点执行</span></div>';
    } else {
      var days = cfg.weekdays || [1];
      var wd = WEEKDAYS.map(function (name, i) {
        var v = i + 1;
        return '<label class="loop-weekday"><input type="checkbox" name="loop-weekday" value="' + v + '"' +
          (days.indexOf(v) >= 0 ? ' checked' : '') + '>' + name + '</label>';
      }).join('');
      html = '<div class="loop-weekdays">' + wd + '</div>' +
        '<div class="loop-form-row" style="margin-top:8px">' +
        '<input class="layui-input" id="loop-f-time" type="time" value="' + (cfg.time || '09:00') +
        '" style="max-width:120px">' +
        '<span>选中星期到这个点执行</span></div>';
    }
    box.innerHTML = html;
  }

  function pickWorkdir() {
    if (!window.A4DirPicker) {
      layer.msg('目录选择组件未加载，请刷新页面', { icon: 2 });
      return;
    }
    window.A4DirPicker.pick({
      title: '选择工作目录',
      start: draft.workdir || '~',
      tip: '进到目标文件夹后点「选择此文件夹」，该文件夹下的内容就是引擎的工作目录'
    }).then(function (p) {
      if (!p) return;
      draft.workdir = p;
      var el = document.getElementById('loop-f-dir-text');
      if (el) {
        el.className = 'task-workdir';
        el.textContent = p;
      }
    });
  }

  function readScheduleConfig() {
    var type = draft.schedule_type;
    if (type === 'once') {
      var v = document.getElementById('loop-f-once').value;
      if (!v) { layer.msg('请选择执行时间', { icon: 2 }); return null; }
      return { run_at: v.replace('T', ' ') };
    }
    if (type === 'interval') {
      var mins = Number(document.getElementById('loop-f-interval').value);
      if (!mins || mins < 1) { layer.msg('间隔至少 1 分钟', { icon: 2 }); return null; }
      return { minutes: Math.floor(mins) };
    }
    var time = document.getElementById('loop-f-time').value;
    if (!time) { layer.msg('请选择时刻', { icon: 2 }); return null; }
    if (type === 'daily') return { time: time };
    var days = [];
    document.querySelectorAll('input[name="loop-weekday"]:checked').forEach(function (cb) {
      days.push(Number(cb.value));
    });
    if (!days.length) { layer.msg('每周计划至少要选一天', { icon: 2 }); return null; }
    return { weekdays: days.sort(), time: time };
  }

  function submitForm() {
    var name = (document.getElementById('loop-f-name').value || '').trim();
    if (!name) { layer.msg('请填写名称', { icon: 2 }); return; }
    var toolEl = document.querySelector('input[name="loop-tool"]:checked');
    if (!toolEl) { layer.msg('请选择引擎', { icon: 2 }); return; }
    var content = (document.getElementById('loop-f-content').value || '').trim();
    if (!content) { layer.msg('请填写任务内容', { icon: 2 }); return; }
    var config = readScheduleConfig();
    if (!config) return;

    var body = {
      name: name,
      tool: toolEl.value,
      task_content: content,
      workdir: draft.workdir || '',
      schedule_type: draft.schedule_type,
      schedule_config: config,
      timeout_seconds: Number(document.getElementById('loop-f-timeout').value),
      catch_up: document.getElementById('loop-f-catchup').checked,
      enabled: document.getElementById('loop-f-enabled').checked
    };
    // 未选则不传（后端按空处理）；提交只带 SkillRef 契约字段，剥掉 _ends/_visible 渲染元数据
    if (pickedSkills.length) {
      body.skills = pickedSkills.map(function (s) {
        return {
          name: s.name,
          source_tool: s.source_tool || '',
          source_scope: s.source_scope || '',
          source_project: s.source_project || '',
          source_path: s.source_path || ''
        };
      });
    }
    var path = draft.id ? '/' + draft.id : '';
    var method = draft.id ? 'PUT' : 'POST';
    apiSend(path, method, body).then(function () {
      layer.closeAll();
      layer.msg(draft.id ? '已保存' : '循环任务已创建', { icon: 1 });
      loadLoops();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---------- 运行历史 ---------- */
  function openHistory(id) {
    var loop = null;
    loops.forEach(function (l) { if (l.id === id) loop = l; });
    layer.open({
      type: 1,
      title: '运行历史 · ' + (loop ? loop.name : '#' + id),
      area: ['720px', '600px'],
      content: '<div class="loop-runs" id="loop-runs-' + id + '">' +
        '<div class="empty-tip">加载中…</div></div>',
      success: function () { loadRuns(id); }
    });
  }

  function loadRuns(id) {
    apiGet('/' + id + '/runs?limit=50').then(function (data) {
      var box = document.getElementById('loop-runs-' + id);
      if (!box) return;  // 弹窗已被关掉
      var runs = data.runs || [];
      var head = '<div class="loop-runs-head">共 ' + data.total + ' 条' +
        (data.total > runs.length ? '（显示最近 ' + runs.length + ' 条）' : '') + '</div>';
      if (!runs.length) {
        box.innerHTML = head + '<div class="empty-tip">还没有运行记录</div>';
        return;
      }
      box.innerHTML = head + '<div class="loop-runs-list">' + runs.map(function (r) {
        var live = r.status === 'pending' || r.status === 'running';
        return '<div class="loop-run" data-id="' + r.id + '">' +
          '<div class="loop-run-title">' + runBadge(r.status) + triggerBadge(r.trigger_type) +
            '<span class="task-id">#' + r.id + '</span></div>' +
          '<div class="loop-run-meta">' +
            '<span>开始 ' + fmtTime(r.started_at || r.created_at) + '</span>' +
            '<span>结束 ' + fmtTime(r.finished_at) + '</span>' +
            '<span>耗时 ' + fmtDuration(r.duration_seconds) + '</span>' +
          '</div>' +
          (r.error_summary ? '<div class="loop-run-error">' + escapeHtml(r.error_summary) + '</div>' : '') +
          '<div class="loop-run-actions">' +
            (r.output_path ? '<button class="layui-btn layui-btn-xs" data-act="log">查看日志</button>' : '') +
            (r.output_path ? '<button class="layui-btn layui-btn-xs layui-btn-primary" data-act="logfull">完整日志</button>' : '') +
            (live ? '<button class="layui-btn layui-btn-xs layui-btn-danger layui-btn-primary" data-act="cancel">取消</button>' : '') +
          '</div>' +
        '</div>';
      }).join('') + '</div>';

      box.querySelectorAll('[data-act]').forEach(function (btn) {
        btn.onclick = function () {
          var runId = Number(btn.closest('.loop-run').getAttribute('data-id'));
          var act = btn.getAttribute('data-act');
          if (act === 'cancel') cancelRun(id, runId);
          else if (act === 'logfull') openRunLog(runId, true);
          else openRunLog(runId, false);
        };
      });
    }).catch(function (e) {
      var box = document.getElementById('loop-runs-' + id);
      if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function cancelRun(loopId, runId) {
    layer.confirm('确定取消这次运行？会终止引擎进程。', { title: '取消运行' }, function (index) {
      layer.close(index);
      fetch(API + '/runs/' + runId + '/cancel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
      }).then(function (r) { return r.json(); }).then(function (res) {
        layer.msg(res.cancelled ? '已取消' : '这次运行已经结束', { icon: res.cancelled ? 1 : 0 });
        loadRuns(loopId);
        loadLoops();
      }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
  }

  function openRunLog(runId, full) {
    var path = full ? '/runs/' + runId + '/log/file' : '/runs/' + runId + '/log';
    apiGet(path).then(function (data) {
      var text = data.output || '（无产出）';
      var note = data.output_truncated
        ? '\n\n…（产出较长，此处仅显示末尾部分，完整内容见产出文件）'
        : (full && data.truncated ? '\n\n…（日志超过 4MB，仅显示开头部分，完整内容见产出文件）' : '');
      layer.open({
        type: 1,
        title: (full ? '完整日志' : '日志摘要') + ' · 运行 #' + runId,
        area: full ? ['880px', '700px'] : ['760px', '600px'],
        content: '<div class="loop-log-view"><pre>' + escapeHtml(text + note) + '</pre></div>'
      });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---------- 引擎列表（表单里复用手动任务的同一份探测） ----------
     缓存一次请求：表单打开时若引擎还没拉到，单选会是空的；用同一个
     promise 兜住「刚进页签就点新建」的竞态。 */
  var enginesPromise = null;

  function loadEngines() {
    if (!enginesPromise) {
      enginesPromise = fetch(TASK_API + '/engines')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          engines = data.engines || [];
          activeConfig = data.active_config || null;
        })
        .catch(function () { engines = []; });
    }
    return enginesPromise;
  }

  /* ---------- 轮询：页面挂着 15s 一刷；进行中的循环不额外加速，
     状态变化（结束/跳过）靠这个间隔 + 立即执行后的主动刷新覆盖 ---------- */
  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(loadLoops, 15000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function syncPolling() {
    if (active) startPolling(); else stopPolling();
  }

  /* ---------- 开关与计划类型（只注册一次，layui 同 filter 会覆盖式注册） ---------- */
  form.on('switch(loop-toggle)', function (data) {
    var id = Number(data.elem.getAttribute('data-id'));
    toggleLoop(id, data.elem.checked);
  });

  form.on('select(loop-type)', function (data) {
    if (!draft) return;
    draft.schedule_type = data.value;
    renderScheduleFields();
  });

  /* ---------- tab ---------- */
  window.addEventListener('main-tab-changed', function (e) {
    active = e.detail.index === 6;
    if (active && !loaded) {
      loaded = true;
      renderShell();
      loadEngines();
      loadLoops();
    }
    syncPolling();
    if (active && !cdTimer) cdTimer = setInterval(renderCountdowns, 1000);
    if (!active && cdTimer) { clearInterval(cdTimer); cdTimer = null; }
  });

  /* 语言切换后重绘一次：星期顿号/逗号是按当时语言拼的，DOM 应用器不会
     反向翻译这类碎片，重渲最稳妥（倒计时下一秒会自行续上） */
  if (window.i18n && window.i18n.onChange) {
    window.i18n.onChange(function () { if (loaded) renderLoops(); });
  }
});
