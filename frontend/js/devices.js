/* 设备页（CrossDevice 集成，M4）：node 运行时 / 发现池 / 一次确认配对 / 设备操作。
 *
 * 协议口径（CrossDevice 接口契约 V1.5）：发现广播只是"看得见"的线索，
 * 绝不自动信任；首次连接经被控端本地一次确认（确认窗同屏一次性 6 位配对码，
 * 带倒计时，可拒绝），配对成功后免手工连接。被控端确认弹窗走全局轮询
 * （不随 tab 切换停止，与任务页完成提醒同款口径）。
 */
layui.use(['layer'], function () {
  var layer = layui.layer;
  var API = '/api/v1/devices';

  var loaded = false;
  var active = false;
  var refreshTimer = null;       // 页内 2s 轮询（运行时 / 池 / 设备列表）
  var watchTimer = null;         // 全局配对监听轮询（被控端确认数据源）
  var incomingShown = {};        // 已弹过窗的配对会话 session_id -> layer index
  var codeLayerIndex = null;     // 被控端配对码弹窗
  var codeLayerSession = null;
  var countdownTimer = null;
  var execPolling = {};          // 设备名 -> true（exec 轮询中）
  var statusMap = {};            // 设备名 -> 最新状态（GET /status）

  /* ---------- 工具 ---------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function parseError(j, status) {
    var detail = j && j.detail;
    if (detail && typeof detail === 'object') {
      return { message: detail.message || JSON.stringify(detail), code: detail.code || '' };
    }
    return { message: (typeof detail === 'string' ? detail : '') ||
      '请求失败（HTTP ' + status + '）', code: '' };
  }

  function apiGet(path) {
    return fetch(API + path).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw parseError(j, r.status); },
        function () { throw parseError(null, r.status); });
      return r.json();
    });
  }

  function apiSend(path, method, body) {
    return fetch(API + path, {
      method: method,
      headers: body instanceof FormData ? undefined : { 'Content-Type': 'application/json' },
      body: body instanceof FormData ? body : JSON.stringify(body || {})
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw parseError(j, r.status); },
        function () { throw parseError(null, r.status); });
      return r.json();
    });
  }

  function fail(e) {
    var msg = e && e.message ? e.message : String(e);
    layer.msg(msg, { icon: 2 });
    return e;
  }

  /* 设备操作的统一错误处理：TOFU 指纹变更给"重置指纹后重连"入口
     （等价 xd --trust 场景，其余错误只提示） */
  function handleOpError(name, e) {
    var msg = e && e.message ? e.message : String(e);
    if (e && e.code === 'TOFU_MISMATCH') { offerTrustReset(name, msg); return; }
    layer.msg(msg, { icon: 2 });
  }

  function offerTrustReset(name, msg) {
    layer.confirm(msg + '是否重置该设备的指纹并重连？' +
      '（仅当确认是同一台设备重装或更换证书时执行）', { title: '证书指纹变更' },
      function (index) {
        layer.close(index);
        apiSend('/' + encodeURIComponent(name) + '/trust/reset', 'POST')
          .then(function () { layer.msg('指纹已重置，请重试操作', { icon: 1 }); })
          .catch(fail);
      });
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(); } catch (e) { return '—'; }
  }

  /* ---------- 初始化 ---------- */
  function boot() {
    renderShell();
    refreshAll();
    startWatch();
  }

  function renderShell() {
    var box = document.getElementById('devices-content');
    if (!box) return;
    box.innerHTML =
      '<div class="dev-section-head"><span>本机被控端（node）</span>' +
        '<span class="dev-head-actions">' +
          '<button class="layui-btn layui-btn-sm" id="dev-runtime-start">启动</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-runtime-stop">停止</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-instance-add">添加实例</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-runtime-settings">运行时设置</button>' +
        '</span></div>' +
      '<div id="dev-runtime" class="dev-runtime"><div class="empty-tip">加载中…</div></div>' +
      '<div class="dev-section-head"><span>已配对设备</span>' +
        '<span class="dev-head-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-skill-install">安装控制技能</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-refresh">刷新</button>' +
        '</span></div>' +
      '<div id="dev-list" class="dev-cards"><div class="empty-tip">加载中…</div></div>' +
      '<div class="dev-section-head"><span>发现的设备（同一局域网，尚未配对）</span>' +
        '<span class="dev-head-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-manual-add">手动添加 IP</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" id="dev-pool-refresh">刷新</button>' +
        '</span></div>' +
      '<div id="dev-pool"><div class="empty-tip">加载中…</div></div>' +
      '<div class="dev-hint"><span>配对说明：在对方设备的「设备互联」页确认请求后，这里输入它展示的 6 位配对码。</span>' +
      '<span>发现列表只提供连接线索，未经确认不会建立连接。</span></div>';

    document.getElementById('dev-refresh').onclick = refreshAll;
    document.getElementById('dev-pool-refresh').onclick = refreshAll;
    document.getElementById('dev-runtime-start').onclick = function () {
      apiSend('/runtime/start', 'POST').then(function () {
        layer.msg('启动指令已下发', { icon: 1 });
        refreshAll();
      }).catch(fail);
    };
    document.getElementById('dev-runtime-stop').onclick = function () {
      layer.confirm('停止本机被控端？其他设备将无法连接本机。', { title: '停止 node' },
        function (index) {
          layer.close(index);
          apiSend('/runtime/stop', 'POST').then(function () {
            layer.msg('已停止', { icon: 1 });
            refreshAll();
          }).catch(fail);
        });
    };
    document.getElementById('dev-instance-add').onclick = openInstanceAdd;
    document.getElementById('dev-runtime-settings').onclick = openRuntimeSettings;
    document.getElementById('dev-manual-add').onclick = openManualAdd;
    document.getElementById('dev-skill-install').onclick = openInstallSkill;
  }

  function loadStatuses() {
    return apiGet('/status').then(function (data) {
      statusMap = {};
      (data.devices || []).forEach(function (d) {
        statusMap[d.device] = d;
      });
    }).catch(function () {});
  }

  function refreshAll() {
    apiGet('/runtime').then(renderRuntime).catch(function () {});
    apiGet('/pool').then(renderPool).catch(function () {});
    loadStatuses().then(function () {
      return apiGet('');
    }).then(function (data) {
      renderDevices(data.devices || []);
    }).catch(function () {});
  }

  /* ---------- 运行时 ---------- */
  var RUNTIME_STATE = {
    stopped: ['已停止', 'dev-state-off'],
    starting: ['启动中', 'dev-state-warn'],
    running: ['运行中', 'dev-state-on'],
    failed: ['失败', 'dev-state-bad']
  };

  function renderRuntime(st) {
    lastRuntime = st;
    var box = document.getElementById('dev-runtime');
    if (!box) return;
    var m = RUNTIME_STATE[st.state] || [st.state, 'dev-state-off'];
    var lines = (st.log || []).slice(-6).map(function (l) {
      return '<div class="dev-log-line">' + escapeHtml(l) + '</div>';
    }).join('');
    box.innerHTML =
      '<div class="dev-runtime-row">' +
        '<span class="dev-state ' + m[1] + '">' + escapeHtml(m[0]) + '</span>' +
        (st.port ? '<span>端口 </span><span>' + st.port + '</span>' : '<span>端口未就绪</span>') +
        (st.pid ? '<span>PID </span><span>' + st.pid + '</span>' : '') +
        '<span class="dev-flex"></span>' +
        '<span class="dev-muted">' + (st.cert_ready ? '证书就绪' : '证书缺失') + '</span>' +
      '</div>' +
      '<div class="dev-runtime-row dev-muted">' +
        '<span>node.py：</span><span class="dev-path" title="' + escapeHtml(st.node_path) + '">' +
          escapeHtml(st.node_path || '未找到，请在运行时设置中指定') + '</span>' +
      '</div>' +
      '<div class="dev-listen-row"><span>发现广播监听：</span>' +
        '<span>' + (st.listener && st.listener.bound
          ? '已开启（UDP ' + st.listener.port + '）'
          : '未开启' + (st.listener && st.listener.error ? '：' + st.listener.error : '')) +
        '</span></div>' +
      (lines ? '<div class="dev-log">' + lines + '</div>' : '') +
      renderInstances(st.instances || []);
    bindInstanceActions(box);
  }

  function openRuntimeSettings() {
    apiGet('/runtime/config').then(function (cfg) {
      var html =
        '<div class="dev-form-row"><label>node.py 路径</label>' +
          '<input class="layui-input" id="dev-cfg-node" value="' + escapeHtml(cfg.node_path || '') + '" ' +
          'placeholder="留空自动探测（安装目录 node_runtime/node.py）"></div>' +
        '<div class="dev-form-row"><label>Python 解释器</label>' +
          '<input class="layui-input" id="dev-cfg-python" value="' + escapeHtml(cfg.python || '') + '" ' +
          'placeholder="留空自动探测"></div>' +
        '<div class="dev-form-row"><label>gen_cert.ps1 路径</label>' +
          '<input class="layui-input" id="dev-cfg-cert" value="' + escapeHtml(cfg.cert_tool || '') + '" ' +
          'placeholder="留空随 node.py 自动探测"></div>' +
        '<div class="dev-form-row"><label>附加参数</label>' +
          '<input class="layui-input" id="dev-cfg-extra" value="' + escapeHtml((cfg.extra_args || []).join(' ')) + '" ' +
          'placeholder="空格分隔，如：--port 51888"></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<label class="dev-check"><input type="checkbox" id="dev-cfg-auto"' +
            (cfg.auto_start ? ' checked' : '') + '><span>应用启动时自动拉起本机被控端</span></label></div>';
      layer.open({
        type: 1, title: '运行时设置', area: ['560px', '420px'],
        content: '<div class="dev-form">' + html +
          '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-cfg-save">保存</button></div></div>',
        success: function () {
          document.getElementById('dev-cfg-save').onclick = function () {
            var body = {
              node_path: document.getElementById('dev-cfg-node').value.trim(),
              python: document.getElementById('dev-cfg-python').value.trim(),
              cert_tool: document.getElementById('dev-cfg-cert').value.trim(),
              extra_args: document.getElementById('dev-cfg-extra').value.trim().split(/\s+/).filter(Boolean),
              auto_start: document.getElementById('dev-cfg-auto').checked
            };
            apiSend('/runtime/config', 'PUT', body).then(function () {
              layer.msg('已保存', { icon: 1 });
              layer.closeAll();
              refreshAll();
            }).catch(fail);
          };
        }
      });
    }).catch(fail);
  }

  /* ---------- 具名实例（B40 多实例） ---------- */
  var INSTANCE_NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;   // 与 node.py --instance 同规
  var lastRuntime = null;      // 最近一次 /runtime 快照（实例清单维护用）

  function instanceNames() {
    return ((lastRuntime && lastRuntime.instances) || [])
      .map(function (i) { return i.name; });
  }

  function renderInstances(list) {
    if (!list.length) return '';
    var cards = list.map(function (ins) {
      var m = RUNTIME_STATE[ins.state] || [ins.state, 'dev-state-off'];
      return '<div class="dev-card dev-instance-card" data-instance="' +
        escapeHtml(ins.name) + '">' +
        '<div class="dev-card-title">' +
          '<span class="dev-card-name">' + escapeHtml(ins.name) + '</span>' +
          '<span class="dev-state ' + m[1] + '">' + escapeHtml(m[0]) + '</span>' +
        '</div>' +
        '<div class="dev-card-meta">' +
          (ins.port ? '<span>端口 </span><span>' + ins.port + '</span>'
                    : '<span>端口未就绪</span>') +
          (ins.pid ? '<span>PID </span><span>' + ins.pid + '</span>' : '') +
        '</div>' +
        '<div class="dev-card-actions">' +
          '<button class="layui-btn layui-btn-sm" data-iact="start">启动</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" data-iact="stop">停止</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary layui-btn-danger" data-iact="remove">移除</button>' +
        '</div></div>';
    }).join('');
    return '<div class="dev-instance-list">' + cards + '</div>';
  }

  function bindInstanceActions(box) {
    box.querySelectorAll('[data-iact]').forEach(function (btn) {
      btn.onclick = function () {
        var card = btn.closest('.dev-instance-card');
        if (!card) return;
        var name = card.getAttribute('data-instance');
        var act = btn.getAttribute('data-iact');
        if (act === 'start') startInstance(name);
        else if (act === 'stop') stopInstance(name);
        else if (act === 'remove') removeInstance(name);
      };
    });
  }

  function startInstance(name) {
    apiSend('/runtime/instances/' + encodeURIComponent(name) + '/start', 'POST')
      .then(function () { layer.msg('启动指令已下发', { icon: 1 }); refreshAll(); })
      .catch(fail);
  }

  function stopInstance(name) {
    layer.confirm('停止实例「' + name + '」？依赖它的设备将无法连接本机。',
      { title: '停止实例' }, function (index) {
        layer.close(index);
        apiSend('/runtime/instances/' + encodeURIComponent(name) + '/stop', 'POST')
          .then(function () { layer.msg('已停止', { icon: 1 }); refreshAll(); })
          .catch(fail);
      });
  }

  function removeInstance(name) {
    layer.confirm('移除实例「' + name + '」？进程将被停止并从清单移除。',
      { title: '移除实例' }, function (index) {
        layer.close(index);
        saveInstances(instanceNames().filter(function (n) { return n !== name; }));
      });
  }

  function saveInstances(names) {
    apiSend('/runtime/config', 'PUT', { instances: names }).then(function () {
      layer.msg('已保存', { icon: 1 });
      refreshAll();
    }).catch(fail);
  }

  function openInstanceAdd() {
    layer.open({
      type: 1, title: '添加实例', area: ['500px', '260px'],
      content: '<div class="dev-form">' +
        '<div class="dev-form-row"><label>实例名</label>' +
          '<input class="layui-input" id="dev-instance-name" autocomplete="off" ' +
          'placeholder="字母/数字/连字符/下划线，最长 32 位"></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-instance-save">添加</button>' +
          '<span class="dev-muted dev-form-tip">每个实例是独立的被控端设备，端口自动顺延</span></div></div>',
      success: function () {
        var input = document.getElementById('dev-instance-name');
        input.focus();
        document.getElementById('dev-instance-save').onclick = function () {
          var name = (input.value || '').trim();
          if (!name) { layer.msg('请填写实例名', { icon: 2 }); return; }
          if (!INSTANCE_NAME_RE.test(name)) {
            layer.msg('实例名限字母、数字、连字符或下划线（最长 32 位）', { icon: 2 });
            return;
          }
          if (instanceNames().indexOf(name) !== -1) {
            layer.msg('实例已存在', { icon: 2 });
            return;
          }
          saveInstances(instanceNames().concat(name));
          layer.closeAll();
        };
      }
    });
  }

  /* ---------- 发现池 ---------- */
  function renderPool(data) {
    var box = document.getElementById('dev-pool');
    if (!box) return;
    var entries = (data.entries || []).map(function (e) {
      e._manual = false;
      return e;
    });
    var manual = (data.manual || []).map(function (m0) {
      return { _manual: true, ip: m0.ip, port: m0.port, hostname: m0.label || '',
               device_id: '', version: '', online: null, last_seen: m0.added_at };
    });
    var rows = entries.concat(manual);
    if (!rows.length) {
      box.innerHTML = '<div class="empty-tip"><span>暂未发现设备。确认对方已安装并启动，</span>' +
        '<span>或点「手动添加 IP」</span></div>';
      return;
    }
    box.innerHTML = '<table class="dev-table"><thead><tr>' +
      '<th>状态</th><th>主机名</th><th>地址</th><th>版本</th><th>设备 ID</th><th>最近出现</th><th>操作</th>' +
      '</tr></thead><tbody>' + rows.map(function (e) {
        var online = e.online;
        var state = online === null ? '—'
          : (online ? '在线' : '离线');
        var cls = online === true ? 'dev-state-on' : (online === false ? 'dev-state-off' : '');
        var addr = e.ip + ':' + e.port;
        return '<tr>' +
          '<td><span class="dev-state ' + cls + '">' + state + '</span></td>' +
          '<td>' + escapeHtml(e.hostname || '—') + '</td>' +
          '<td><span class="dev-addr">' + escapeHtml(addr) + '</span></td>' +
          '<td>' + escapeHtml(e.version || '—') + '</td>' +
          '<td>' + escapeHtml(e.device_id || '—') + '</td>' +
          '<td>' + fmtTime(e.last_seen) + '</td>' +
          '<td class="dev-actions">' +
            '<button class="layui-btn layui-btn-xs layui-btn-normal" data-act="pair" data-ip="' +
              escapeHtml(e.ip) + '" data-port="' + e.port + '">发起配对</button>' +
            (e._manual ? '<button class="layui-btn layui-btn-xs layui-btn-primary" data-act="rm-manual" data-ip="' +
              escapeHtml(e.ip) + '" data-port="' + e.port + '">移除</button>' : '') +
          '</td></tr>';
      }).join('') + '</tbody></table>';

    box.querySelectorAll('[data-act="pair"]').forEach(function (btn) {
      btn.onclick = function () {
        openPair(btn.getAttribute('data-ip'), Number(btn.getAttribute('data-port')));
      };
    });
    box.querySelectorAll('[data-act="rm-manual"]').forEach(function (btn) {
      btn.onclick = function () {
        apiSend('/pool/manual', 'DELETE', {
          ip: btn.getAttribute('data-ip'), port: Number(btn.getAttribute('data-port'))
        }).then(refreshAll).catch(fail);
      };
    });
  }

  function openManualAdd() {
    layer.open({
      type: 1, title: '手动添加 IP', area: ['460px', '280px'],
      content: '<div class="dev-form">' +
        '<div class="dev-form-row"><label>IP 地址</label>' +
          '<input class="layui-input" id="dev-manual-ip" placeholder="如 192.168.3.20"></div>' +
        '<div class="dev-form-row"><label>端口</label>' +
          '<input class="layui-input" id="dev-manual-port" value="51888"></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-manual-save">添加</button>' +
          '<span class="dev-muted dev-form-tip">广播不可达（跨网段）时手动填地址</span></div></div>',
      success: function () {
        document.getElementById('dev-manual-save').onclick = function () {
          var ip = document.getElementById('dev-manual-ip').value.trim();
          var port = Number(document.getElementById('dev-manual-port').value) || 51888;
          apiSend('/pool/manual', 'POST', { ip: ip, port: port })
            .then(function () { layer.closeAll(); refreshAll(); })
            .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        };
      }
    });
  }

  /* ---------- 安装控制技能（B35/FR-16） ---------- */
  function openInstallSkill() {
    layer.open({
      type: 1, title: '安装控制技能', area: ['600px', '440px'],
      content: '<div class="dev-form dev-skill-form">' +
        '<div class="dev-pair-wait"><span>把 CrossDevice 控制技能安装到 Agent 技能目录，</span>' +
        '<span>工作空间内的 Agent 即可发现并使用已配对设备的能力。</span></div>' +
        '<div class="dev-form-row"><label>安装范围</label>' +
          '<span class="dev-check"><input type="radio" name="dev-skill-scope" value="global" checked>' +
          '<span>全局技能库（九端通用）</span></span></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<span class="dev-check"><input type="radio" name="dev-skill-scope" value="project">' +
          '<span>项目目录</span></span>' +
          '<span class="dev-check"><input type="radio" name="dev-skill-scope" value="custom">' +
          '<span>自定义目录</span></span></div>' +
        '<div class="dev-form-row" id="dev-skill-project-row" style="display:none"><label>选择项目</label>' +
          '<select class="layui-input" id="dev-skill-project" style="flex:1"></select></div>' +
        '<div class="dev-form-row" id="dev-skill-custom-row" style="display:none"><label>目录路径</label>' +
          '<input class="layui-input" id="dev-skill-custom-dir" placeholder="绝对路径，如 D:\\work\\demo" autocomplete="off"></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-skill-go">确认安装</button>' +
          '<span class="dev-muted dev-form-tip" id="dev-skill-msg"></span></div></div>',
      success: function () {
        var radios = document.getElementsByName('dev-skill-scope');
        var projectRow = document.getElementById('dev-skill-project-row');
        var customRow = document.getElementById('dev-skill-custom-row');
        var sel = document.getElementById('dev-skill-project');
        function syncRows() {
          var v = 'global';
          radios.forEach(function (r) { if (r.checked) v = r.value; });
          projectRow.style.display = v === 'project' ? '' : 'none';
          customRow.style.display = v === 'custom' ? '' : 'none';
        }
        radios.forEach(function (r) { r.onchange = syncRows; });
        fetch(API + '/skills/project-roots').then(function (r) { return r.json(); })
          .then(function (j) {
            var list = (j && j.projects) || [];
            sel.innerHTML = list.length
              ? list.map(function (p) {
                  return '<option value="' + escapeHtml(p.path) + '">' + escapeHtml(p.path) + '</option>';
                }).join('')
              : '<option value="">（尚无已注册项目，先在技能管理页注册）</option>';
          }).catch(function () {
            sel.innerHTML = '<option value="">（项目列表加载失败）</option>';
          });
        document.getElementById('dev-skill-go').onclick = function () {
          var scope = 'global';
          radios.forEach(function (r) { if (r.checked) scope = r.value; });
          var body = { scope: scope };
          if (scope === 'project') {
            body.project_path = sel.value;
            if (!body.project_path) {
              layer.msg('尚无已注册项目，请改选全局或自定义目录', { icon: 2 });
              return;
            }
          }
          if (scope === 'custom') {
            body.custom_dir = document.getElementById('dev-skill-custom-dir').value.trim();
            if (!body.custom_dir) {
              layer.msg('请填写目录路径', { icon: 2 });
              return;
            }
          }
          var msgEl = document.getElementById('dev-skill-msg');
          msgEl.textContent = '正在安装…';
          apiSend('/install-skill', 'POST', body).then(function (r) {
            var lines = [];
            if (r.installed.length) {
              lines.push('已安装到 ' + r.installed.length + ' 个技能目录：');
              r.installed.forEach(function (x) {
                lines.push('　[' + x.tool + '] ' + x.path);
              });
            }
            if (r.skipped.length) {
              lines.push('已跳过 ' + r.skipped.length + ' 个（同名已存在，未覆盖）：');
              r.skipped.forEach(function (x) {
                lines.push('　[' + x.tool + '] ' + x.reason);
              });
            }
            if (!r.installed.length && !r.skipped.length) lines.push('没有可安装的目标目录。');
            layer.alert(lines.map(function (t) { return '<span>' + escapeHtml(t) + '</span>'; })
              .join('<br>'), { title: '安装控制技能', icon: r.installed.length ? 1 : 0 });
          }).catch(function (e) {
            msgEl.textContent = '';
            layer.alert(e.message || '安装失败', { title: '安装控制技能', icon: 2 });
          });
        };
      }
    });
  }

  /* ---------- 已配对设备 ---------- */
  function renderDevices(list) {
    var box = document.getElementById('dev-list');
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '<div class="empty-tip"><span>还没有已配对设备。在下方发现列表点「发起配对」</span>' +
        '<span>，对方确认后输入配对码即可建立连接</span></div>';
      return;
    }
    box.innerHTML = list.map(function (d) {
      var st = statusMap[d.name] || {};
      var latest = (st.sessions || [])[0];
      var statusHtml = latest
        ? '<div class="dev-status' + (latest.stale ? ' dev-status-stale' : '') + '">' +
            '<span class="dev-status-label">最新状态：</span>' +
            '<span class="dev-status-event">' + escapeHtml(latest.event_label || latest.event) + '</span>' +
            '<span class="dev-status-state">' + escapeHtml(latest.state_label || latest.state) + '</span>' +
            (latest.agent ? '<span class="dev-status-agent">' + escapeHtml(latest.agent) + '</span>' : '') +
            '<span class="dev-status-time">' + fmtTime(latest.ts) + '</span>' +
            (latest.stale ? '<span class="dev-status-badge">已过时</span>' : '') +
          '</div>'
        : '<div class="dev-status dev-status-empty">' +
            '<span>最新状态：</span><span>暂无状态</span></div>';
      if (st.last_error) {
        statusHtml += '<div class="dev-status dev-status-error">' +
          '<span>拉取失败：</span><span>' + escapeHtml(st.last_error) + '</span></div>';
      }
      return '<div class="dev-card" data-name="' + escapeHtml(d.name) + '">' +
        '<div class="dev-card-title">' +
          '<span class="dev-card-name">' + escapeHtml(d.name) + '</span>' +
          '<span class="dev-badge">' + escapeHtml(d.identity || 'default') + '</span>' +
        '</div>' +
        '<div class="dev-card-meta">' +
          '<span>' + escapeHtml(d.ip) + ':' + d.port + '</span>' +
          (d.hostname ? '<span>' + escapeHtml(d.hostname) + '</span>' : '') +
          (d.device_id ? '<span>ID ' + escapeHtml(d.device_id) + '</span>' : '') +
          '<span>配对于 ' + fmtTime(d.added_at) + '</span>' +
        '</div>' +
        statusHtml +
        '<div class="dev-card-actions">' +
          '<button class="layui-btn layui-btn-sm" data-act="exec">命令</button>' +
          '<button class="layui-btn layui-btn-sm" data-act="screen">截屏</button>' +
          '<button class="layui-btn layui-btn-sm" data-act="put">上传</button>' +
          '<button class="layui-btn layui-btn-sm" data-act="get">下载</button>' +
          '<button class="layui-btn layui-btn-sm" data-act="tasks">任务历史</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary" data-act="ping">探活</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-primary layui-btn-danger" data-act="forget">忘记</button>' +
        '</div></div>';
    }).join('');

    box.querySelectorAll('[data-act]').forEach(function (btn) {
      btn.onclick = function () {
        var name = btn.closest('.dev-card').getAttribute('data-name');
        var act = btn.getAttribute('data-act');
        if (act === 'exec') openExec(name);
        else if (act === 'screen') openScreen(name);
        else if (act === 'put') openPut(name);
        else if (act === 'get') openGet(name);
        else if (act === 'tasks') openTasks(name);
        else if (act === 'forget') forgetDevice(name);
        else if (act === 'ping') pingDevice(name);
      };
    });
  }

  function pingDevice(name) {
    apiSend('/' + encodeURIComponent(name) + '/ping', 'POST').then(function (data) {
      layer.msg('设备在线' + (data && data.node_version ? '（node v' + data.node_version + '）' : ''),
        { icon: 1 });
    }).catch(function (e) {
      handleOpError(name, e);
    });
  }

  function forgetDevice(name) {
    layer.confirm('忘记设备「' + name + '」？将删除本机保存的配对凭据，' +
      '再次连接需重新配对。', { title: '忘记设备' }, function (index) {
      layer.close(index);
      apiSend('/' + encodeURIComponent(name) + '/forget', 'POST')
        .then(function () { layer.msg('已忘记', { icon: 1 }); refreshAll(); })
        .catch(fail);
    });
  }

  /* ---------- 配对：控制端 ---------- */
  function openPair(ip, port) {
    apiSend('/pair/begin', 'POST', { ip: ip, port: port }).catch(function (e) {
      layer.alert(e.message || '发起配对失败', { title: '发起配对', icon: 2 });
      throw e;
    }).then(function (session) {
      if (!session) return;
      var expires = session.expires_at || 0;
      layer.open({
        type: 1, title: '配对 ' + ip + ':' + port, area: ['480px', '340px'],
        content: '<div class="dev-form dev-pair-form">' +
          '<div class="dev-pair-wait"><span>请在对方设备的「设备互联」页确认本次配对，</span>' +
          '<span>然后输入它展示的 6 位配对码。</span></div>' +
          '<div class="dev-pair-wait dev-muted"><span>请求有效期至 </span>' +
            '<span id="dev-pair-expires">' + fmtTime(expires) + '</span></div>' +
          '<div class="dev-form-row"><label>配对码</label>' +
            '<input class="layui-input dev-code-input" id="dev-pair-code" maxlength="6" ' +
            'placeholder="6 位数字" autocomplete="off"></div>' +
          '<div class="dev-form-row"><label></label>' +
            '<button class="layui-btn layui-btn-normal" id="dev-pair-confirm">确认配对</button>' +
            '<span class="dev-muted dev-form-tip" id="dev-pair-msg"></span></div></div>',
        success: function () {
          var input = document.getElementById('dev-pair-code');
          input.focus();
          input.onkeydown = function (ev) {
            if (ev.key === 'Enter') submitPair(ip, port);
          };
          document.getElementById('dev-pair-confirm').onclick = function () {
            submitPair(ip, port);
          };
        }
      });
    });
  }

  function submitPair(ip, port) {
    var code = (document.getElementById('dev-pair-code').value || '').trim();
    var msg = document.getElementById('dev-pair-msg');
    if (!/^\d{6}$/.test(code)) {
      if (msg) msg.textContent = '请输入 6 位数字配对码';
      return;
    }
    apiSend('/pair/confirm', 'POST', { ip: ip, port: port, code: code })
      .then(function (entry) {
        layer.closeAll();
        layer.msg('配对成功：' + (entry && entry.name ? entry.name : ip), { icon: 1 });
        refreshAll();
      })
      .catch(function (e) {
        if (msg) msg.textContent = e.message || '配对失败';
      });
  }

  /* ---------- 配对：被控端（本机确认窗，全局轮询） ---------- */
  function startWatch() {
    if (watchTimer) return;
    watchTimer = setInterval(pollIncoming, 2000);
  }

  function pollIncoming() {
    apiGet('/pair/incoming').then(function (data) {
      if (!data || !data.available) return;
      var sid = data.session_id || '';
      if (data.state === 'pending') {
        if (sid && !incomingShown[sid]) {
          incomingShown[sid] = true;
          showIncoming(sid, data);
        }
      } else if (data.state === 'approved') {
        if (sid && codeLayerSession !== sid) {
          showCodeModal(sid, data);
        }
      } else if (data.state !== 'idle' && codeLayerSession === sid) {
        // 会话终结（consumed / expired / voided）：收掉配对码弹窗
        closeCodeModal();
      }
    }).catch(function () {});
  }

  function showIncoming(sid, data) {
    var confirmed = false;
    layer.open({
      type: 1, title: '配对请求', area: ['440px', '240px'], btn: [],
      content: '<div class="dev-incoming">' +
        '<div class="dev-incoming-line"><span>来自 </span><span class="dev-incoming-ip">' +
          escapeHtml(data.source_ip || '?') + '</span><span> 的设备请求与本机配对。</span></div>' +
        '<div class="dev-incoming-line dev-muted"><span>确认前请核实这是你自己的设备。' +
        '确认后将展示一次性配对码，有效期 5 分钟。</span></div>' +
        '<div class="dev-incoming-btns">' +
          '<button class="layui-btn layui-btn-normal" id="dev-incoming-yes">确认配对</button>' +
          '<button class="layui-btn layui-btn-primary" id="dev-incoming-no">拒绝</button>' +
        '</div></div>',
      success: function (layero) {
        document.getElementById('dev-incoming-yes').onclick = function () {
          if (confirmed) return;
          confirmed = true;
          apiSend('/pair/approve', 'POST').then(function (data2) {
            layer.closeAll();
            showCodeModal(sid, data2);
          }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        };
        document.getElementById('dev-incoming-no').onclick = function () {
          apiSend('/pair/reject', 'POST').then(function () {
            layer.closeAll();
            layer.msg('已拒绝，本次配对未建立', { icon: 1 });
          }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        };
      },
      cancel: function () { delete incomingShown[sid]; }
    });
  }

  function showCodeModal(sid, data) {
    closeCodeModal();
    codeLayerSession = sid;
    var expires = data.code_expires_at || data.expires_at || 0;
    // layer.open 的返回值即弹层索引（closeCodeModal 依赖它收层）
    codeLayerIndex = layer.open({
      type: 1, title: '配对码', area: ['380px', '300px'], btn: [],
      content: '<div class="dev-code-modal">' +
        '<div class="dev-code-value" id="dev-pair-code-value">' + escapeHtml(data.code || '') + '</div>' +
        '<div class="dev-code-hint"><span>请在控制端输入此码。有效期剩余 </span>' +
          '<span id="dev-code-countdown">—</span><span> 秒</span></div>' +
        '<div class="dev-code-hint dev-muted"><span>倒计时结束或对方完成配对后，此码自动失效。</span></div>' +
        '<div class="dev-incoming-btns">' +
          '<button class="layui-btn layui-btn-primary layui-btn-sm" id="dev-code-close">关闭</button>' +
        '</div></div>',
      success: function () {
        document.getElementById('dev-code-close').onclick = function () {
          closeCodeModal();
        };
        startCountdown(expires);
      },
      cancel: function () {
        // 用户点右上角叉：只清计时与会话标记，索引由 closeCodeModal 统一清
        if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
        codeLayerSession = null;
        codeLayerIndex = null;
      }
    });
  }

  function closeCodeModal() {
    if (codeLayerIndex != null) {
      layer.close(codeLayerIndex);
      codeLayerIndex = null;
    }
    codeLayerSession = null;
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  }

  function startCountdown(expires) {
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    var el = document.getElementById('dev-code-countdown');
    if (!el) return;
    function tick() {
      var target = document.getElementById('dev-code-countdown');
      if (!target) { if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; } return; }
      var left = Math.max(0, Math.ceil(expires - Date.now() / 1000));
      target.textContent = String(left);   // 倒计时只改 span 文本
      if (left <= 0) {
        if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
        closeCodeModal();
        layer.msg('配对码已过期，如需配对请重新发起', { icon: 0 });
      }
    }
    tick();
    countdownTimer = setInterval(tick, 1000);
  }

  /* ---------- 操作：exec ---------- */
  function openExec(name) {
    layer.open({
      type: 1, title: '命令 - ' + name, area: ['680px', '520px'],
      content: '<div class="dev-form dev-exec-form">' +
        '<div class="dev-form-row"><label>命令</label>' +
          '<input class="layui-input" id="dev-exec-cmd" placeholder="如：ipconfig /all" ' +
          'autocomplete="off"></div>' +
        '<div class="dev-form-row"><label>Shell</label>' +
          '<div class="layui-form dev-exec-shell" lay-filter="dev-exec-shell-form">' +
            '<select id="dev-exec-shell">' +
              '<option value="cmd" selected>cmd</option>' +
              '<option value="powershell">powershell</option>' +
              '<option value="none">none（直接执行）</option>' +
            '</select></div>' +
          '<span class="dev-muted dev-form-tip">超时默认 60 秒</span></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-exec-run">执行</button>' +
          '<span class="dev-muted dev-form-tip" id="dev-exec-state"></span></div>' +
        '<pre class="dev-exec-output" id="dev-exec-output">（等待执行）</pre></div>',
      success: function () {
        layui.form.render('select', 'dev-exec-shell-form');
        document.getElementById('dev-exec-cmd').focus();
        document.getElementById('dev-exec-cmd').onkeydown = function (ev) {
          if (ev.key === 'Enter') runExec(name);
        };
        document.getElementById('dev-exec-run').onclick = function () { runExec(name); };
      }
    });
  }

  function runExec(name) {
    var cmd = (document.getElementById('dev-exec-cmd').value || '').trim();
    var out = document.getElementById('dev-exec-output');
    var state = document.getElementById('dev-exec-state');
    if (!cmd) { layer.msg('请输入命令', { icon: 2 }); return; }
    var shell = document.getElementById('dev-exec-shell').value;
    var base = '/' + encodeURIComponent(name);
    apiSend(base + '/exec', 'POST', { cmd: cmd, shell: shell, timeout: 60 })
      .then(function (data) {
        if (data && data.detached) {
          if (out) out.textContent = '已分离启动（PID ' + data.pid + '），不回收输出';
          return;
        }
        var jobId = data && data.job_id;
        if (!jobId) { if (out) out.textContent = '未返回任务 ID'; return; }
        if (state) state.textContent = '执行中…';
        pollJob(name, jobId, 0);
      })
      .catch(function (e) {
        if (out) out.textContent = e.message || '执行失败';
        handleOpError(name, e);
      });
  }

  function pollJob(name, jobId, round) {
    var out = document.getElementById('dev-exec-output');
    var state = document.getElementById('dev-exec-state');
    if (!out) return;   // 弹窗已关
    apiGet('/' + encodeURIComponent(name) + '/job/' + jobId + '?wait=3')
      .then(function (job) {
        if (job.status === 'running' && round < 40) { pollJob(name, jobId, round + 1); return; }
        if (state) state.textContent = '';
        var text = '状态：' + job.status +
          (job.exit_code != null ? '    退出码：' + job.exit_code : '') +
          (job.duration != null ? '    耗时：' + Math.round(job.duration) + 's' : '') + '\n';
        if (job.stdout) text += '\n[stdout]\n' + job.stdout;
        if (job.stderr) text += '\n[stderr]\n' + job.stderr;
        out.textContent = text || '（无输出）';
      })
      .catch(function (e) {
        out.textContent = e.message || '查询任务失败';
      });
  }

  /* ---------- 操作：截屏 ---------- */
  function openScreen(name) {
    var base = '/' + encodeURIComponent(name);
    layer.open({
      type: 1, title: '截屏 - ' + name, area: ['820px', '620px'],
      content: '<div class="dev-screen-modal">' +
        '<div class="dev-screen-bar">' +
          '<span>显示器</span>' +
          '<div class="layui-form dev-screen-mon" lay-filter="dev-screen-form">' +
            '<select id="dev-screen-monitor">' +
              '<option value="-1" selected>全部（虚拟桌面）</option>' +
              '<option value="0">显示器 1</option><option value="1">显示器 2</option>' +
            '</select></div>' +
          '<button class="layui-btn layui-btn-sm" id="dev-screen-refresh">刷新</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" id="dev-screen-save">保存到本机</button>' +
          '<span class="dev-muted dev-form-tip" id="dev-screen-state"></span>' +
        '</div>' +
        '<div class="dev-screen-view"><img id="dev-screen-img" alt="截屏预览"></div></div>',
      success: function () {
        layui.form.render('select', 'dev-screen-form');
        loadScreen(name);
        document.getElementById('dev-screen-refresh').onclick = function () { loadScreen(name); };
        document.getElementById('dev-screen-save').onclick = function () {
          if (!window.A4DirPicker) { layer.msg('目录选择组件未加载', { icon: 2 }); return; }
          window.A4DirPicker.pick({
            title: '选择截屏保存目录',
            start: '~',
            tip: '截屏将保存为 PNG 文件到所选文件夹'
          }).then(function (dir) {
            if (!dir) return;
            var stateEl = document.getElementById('dev-screen-state');
            if (stateEl) stateEl.textContent = '正在保存…';
            apiSend(base + '/screen/save', 'POST', {
              monitor: Number(document.getElementById('dev-screen-monitor').value),
              save_dir: dir
            }).then(function (r) {
              if (stateEl) stateEl.textContent = '';
              layer.msg('已保存：' + r.saved_path, { icon: 1 });
            }).catch(function (e) {
              if (stateEl) stateEl.textContent = '';
              handleOpError(name, e);
            });
          });
        };
      }
    });
  }

  function loadScreen(name) {
    var img = document.getElementById('dev-screen-img');
    var stateEl = document.getElementById('dev-screen-state');
    if (!img) return;
    var monitor = document.getElementById('dev-screen-monitor').value;
    img.src = API + '/' + encodeURIComponent(name) + '/screen?monitor=' + monitor +
      '&max_width=1600&ts=' + Date.now();
    if (stateEl) stateEl.textContent = '正在截取…';
    img.onload = function () { if (stateEl) stateEl.textContent = ''; };
    img.onerror = function () {
      if (stateEl) stateEl.textContent = '';
      layer.msg('截屏失败：设备不可达或没有桌面会话', { icon: 2 });
    };
  }

  /* ---------- 操作：put / get ---------- */
  function openPut(name) {
    layer.open({
      type: 1, title: '上传文件 - ' + name, area: ['560px', '320px'],
      content: '<div class="dev-form">' +
        '<div class="dev-form-row"><label>本地文件</label>' +
          '<input type="file" id="dev-put-file" class="dev-file-input"></div>' +
        '<div class="dev-form-row"><label>远端路径</label>' +
          '<input class="layui-input" id="dev-put-remote" ' +
          'placeholder="目标完整路径，如 C:\\Users\\pub\\desk\\log.txt"></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-put-run">上传</button>' +
          '<span class="dev-muted dev-form-tip" id="dev-put-state"></span></div></div>',
      success: function () {
        document.getElementById('dev-put-run').onclick = function () {
          var input = document.getElementById('dev-put-file');
          var remote = document.getElementById('dev-put-remote').value.trim();
          var stateEl = document.getElementById('dev-put-state');
          if (!input.files || !input.files.length) {
            layer.msg('请先选择文件', { icon: 2 });
            return;
          }
          if (!remote) { layer.msg('请填写远端目标路径', { icon: 2 }); return; }
          var fd = new FormData();
          fd.append('file', input.files[0]);
          fd.append('remote_path', remote);
          if (stateEl) stateEl.textContent = '上传中…';
          apiSend('/' + encodeURIComponent(name) + '/put', 'POST', fd)
            .then(function (r) {
              if (stateEl) stateEl.textContent = '';
              layer.msg('已上传：' + (r.path || remote) + '（' + (r.size || 0) + ' 字节）', { icon: 1 });
            })
            .catch(function (e) {
              if (stateEl) stateEl.textContent = '';
              handleOpError(name, e);
            });
        };
      }
    });
  }

  function openGet(name) {
    layer.open({
      type: 1, title: '下载文件 - ' + name, area: ['560px', '340px'],
      content: '<div class="dev-form">' +
        '<div class="dev-form-row"><label>远端路径</label>' +
          '<input class="layui-input" id="dev-get-remote" ' +
          'placeholder="远端文件完整路径，如 C:\\Users\\pub\\desk\\log.txt"></div>' +
        '<div class="dev-form-row"><label>保存目录</label>' +
          '<button class="layui-btn layui-btn-sm" id="dev-get-dir">选择文件夹…</button>' +
          '<span class="dev-muted" id="dev-get-dir-text">未选择</span></div>' +
        '<div class="dev-form-row"><label></label>' +
          '<button class="layui-btn layui-btn-normal" id="dev-get-run">下载</button>' +
          '<span class="dev-muted dev-form-tip" id="dev-get-state"></span></div></div>',
      success: function () {
        var dir = '';
        document.getElementById('dev-get-dir').onclick = function () {
          if (!window.A4DirPicker) { layer.msg('目录选择组件未加载', { icon: 2 }); return; }
          window.A4DirPicker.pick({
            title: '选择下载保存目录', start: '~',
            tip: '文件将保存到所选文件夹（同名文件覆盖）'
          }).then(function (p) {
            if (!p) return;
            dir = p;
            document.getElementById('dev-get-dir-text').textContent = p;
          });
        };
        document.getElementById('dev-get-run').onclick = function () {
          var remote = document.getElementById('dev-get-remote').value.trim();
          var stateEl = document.getElementById('dev-get-state');
          if (!remote) { layer.msg('请填写远端文件路径', { icon: 2 }); return; }
          if (!dir) { layer.msg('请先选择保存目录', { icon: 2 }); return; }
          if (stateEl) stateEl.textContent = '下载中…';
          apiGet('/' + encodeURIComponent(name) + '/get?path=' +
              encodeURIComponent(remote) + '&save_dir=' + encodeURIComponent(dir))
            .then(function (r) {
              if (stateEl) stateEl.textContent = '';
              layer.msg('已下载：' + r.saved_path + '（' + (r.size || 0) + ' 字节）', { icon: 1 });
            })
            .catch(function (e) {
              if (stateEl) stateEl.textContent = '';
              handleOpError(name, e);
            });
        };
      }
    });
  }

  /* ---------- 操作：任务历史 ---------- */
  var TASK_STATE = {
    running: '运行中', done: '已完成', timeout: '超时', failed: '失败'
  };

  function openTasks(name) {
    layer.open({
      type: 1, title: '任务历史 - ' + name, area: ['860px', '620px'],
      content: '<div class="dev-tasks-modal"><div id="dev-tasks-box">' +
        '<div class="empty-tip">加载中…</div></div></div>',
      success: function () { loadTasks(name); }
    });
  }

  function loadTasks(name) {
    var box = document.getElementById('dev-tasks-box');
    if (!box) return;
    apiGet('/' + encodeURIComponent(name) + '/tasks?limit=50').then(function (data) {
      var entries = data.entries || [];
      if (!entries.length) {
        box.innerHTML = '<div class="empty-tip">该设备还没有任务记录</div>';
        return;
      }
      box.innerHTML = '<table class="dev-table"><thead><tr>' +
        '<th>开始时间</th><th>状态</th><th>退出码</th><th>命令</th>' +
        '</tr></thead><tbody>' + entries.map(function (t) {
          return '<tr class="dev-task-row" data-id="' + escapeHtml(t.id) + '">' +
            '<td>' + fmtTime(t.started_at) + '</td>' +
            '<td>' + escapeHtml(TASK_STATE[t.status] || t.status) + '</td>' +
            '<td>' + (t.exit_code == null ? '—' : t.exit_code) + '</td>' +
            '<td class="dev-task-cmd" title="' + escapeHtml(t.cmd) + '">' +
              escapeHtml(t.cmd) + '</td></tr>';
        }).join('') + '</tbody></table>' +
        '<div class="dev-muted dev-tasks-total">显示最近 ' + entries.length +
        ' 条（共 ' + (data.total || entries.length) + ' 条）</div>';
      box.querySelectorAll('.dev-task-row').forEach(function (row) {
        row.onclick = function () { openTaskDetail(name, row.getAttribute('data-id')); };
      });
    }).catch(function (e) {
      box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message || '') + '</div>';
    });
  }

  function openTaskDetail(name, taskId) {
    apiGet('/' + encodeURIComponent(name) + '/tasks/' + encodeURIComponent(taskId))
      .then(function (t) {
        var text = '状态：' + (TASK_STATE[t.status] || t.status) +
          (t.exit_code != null ? '    退出码：' + t.exit_code : '') +
          (t.duration != null ? '    耗时：' + Math.round(t.duration) + 's' : '') +
          '\n\n[cmd]\n' + (t.cmd || '') +
          (t.stdout ? '\n\n[stdout]\n' + t.stdout : '') +
          (t.stderr ? '\n\n[stderr]\n' + t.stderr : '');
        layer.open({
          type: 1, title: '任务 ' + taskId, area: ['760px', '560px'],
          content: '<pre class="dev-exec-output dev-task-detail">' +
            escapeHtml(text || '（无内容）') + '</pre>'
        });
      })
      .catch(function (e) { layer.msg(e.message || '加载失败', { icon: 2 }); });
  }

  /* ---------- 轮询 ---------- */
  function startPolling() {
    if (refreshTimer) return;
    refreshTimer = setInterval(refreshAll, 2000);
  }

  function stopPolling() {
    if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  }

  window.addEventListener('main-tab-changed', function (e) {
    active = e.detail.index === 7;
    if (active && !loaded) { loaded = true; boot(); }
    if (loaded) { if (active) startPolling(); else stopPolling(); }
  });
});
