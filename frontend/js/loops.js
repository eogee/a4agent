/* 循环任务（执行计划）—— 2026-10-30 起并入「任务下发」页，本模块只渲染
 * 表单上方的「计划中的任务」横条：计划列表、启停、立即执行、运行历史与日志。
 * 新建/编辑走主表单（tasks.js 的 A4TaskForm），到点由后端调度线程投递，
 * 运行记录复用任务表（AgentTask.loop_id），因此超时、杀进程树、日志落盘、
 * 终态通知全部与手动任务一致。
 */
layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var API = '/api/v1/loops';

  var loaded = false;
  var active = false;
  var pollTimer = null;
  var cdTimer = null;
  var loops = [];
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
        '<span class="loops-title">计划中的任务</span>' +
        '<span class="loops-sched" id="loops-sched">加载中…</span>' +
        '<span class="loops-header-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="loops-refresh">刷新</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" id="loops-add">新建循环任务</button>' +
        '</span>' +
      '</div>' +
      '<div id="loops-list" class="loops-list"><div class="empty-tip">加载中…</div></div>';
    document.getElementById('loops-refresh').onclick = loadLoops;
    document.getElementById('loops-add').onclick = function () {
      if (window.A4TaskForm) window.A4TaskForm.newLoop();
      else layer.msg('表单组件未加载，请刷新页面', { icon: 2 });
    };
  }

  /* ---------- 列表 ---------- */
  function loadLoops() {
    apiGet('').then(function (data) {
      loops = data.loops || [];
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
      box.innerHTML = '<div class="empty-tip">还没有计划。在上方表单把执行计划切到「按计划循环」' +
        '并保存，Agent 就会到点自动执行（应用开着才会触发）</div>';
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

  /* 表单已并入「任务下发」主表单（tasks.js）：编辑/新建都走 A4TaskForm，
     本模块只保留计划列表、启停、立即执行、运行历史与日志。 */
  function editLoop(id) {
    var loop = null;
    loops.forEach(function (l) { if (l.id === id) loop = l; });
    if (window.A4TaskForm) window.A4TaskForm.editLoop(loop);
    else layer.msg('表单组件未加载，请刷新页面', { icon: 2 });
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

  /* ---------- 开关（只注册一次，layui 同 filter 会覆盖式注册） ---------- */
  form.on('switch(loop-toggle)', function (data) {
    var id = Number(data.elem.getAttribute('data-id'));
    toggleLoop(id, data.elem.checked);
  });

  /* ---------- tab 激活 ----------
   * 2026-10-30 二次改造：表单并入「任务下发」主表单后，计划横条与任务列表
   * 同屏常驻（不再是页内 seg 切换），激活 = 任务下发 Tab（index 5）。
   * 切走即停轮询与倒计时，与独立 Tab 时代的语义一致。 */
  function setLoopsView(on) {
    active = on;
    if (active && !loaded) {
      loaded = true;
      renderShell();
      loadLoops();
    }
    syncPolling();
    if (active && !cdTimer) cdTimer = setInterval(renderCountdowns, 1000);
    if (!active && cdTimer) { clearInterval(cdTimer); cdTimer = null; }
  }

  window.addEventListener('main-tab-changed', function (e) {
    setLoopsView(e.detail.index === 5);
  });

  /* 供 tasks.js 在计划保存/创建后刷新列表 */
  window.A4Loops = { reload: loadLoops };

  /* 语言切换后重绘一次：星期顿号/逗号是按当时语言拼的，DOM 应用器不会
     反向翻译这类碎片，重渲最稳妥（倒计时下一秒会自行续上） */
  if (window.i18n && window.i18n.onChange) {
    window.i18n.onChange(function () { if (loaded) renderLoops(); });
  }
});
