/* 通知提醒 —— 双通道：手机推送（ntfy.sh）+ 桌面横幅（Win32 toast）。
 * 手机通道：任务终态时经 task_notify 监听器推送；桌面通道由 a4agent 常驻进程代发。
 * 本页只管配置与订阅引导。
 */
layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var API = '/api/v1/phone';

  var loaded = false;
  var active = false;

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

  /* ---------- 渲染 ---------- */
  function renderShell() {
    var box = document.getElementById('phone-content');
    if (!box) return;
    box.innerHTML =
      '<div class="phone-chan-bar">' +
        '<span class="phone-chan-title">通知通道</span>' +
        '<span class="phone-chan-item"><i class="phone-chan-dot" id="phone-dot-mobile"></i>' +
          '手机推送 <b id="phone-st-mobile">加载中</b></span>' +
        '<span class="phone-chan-item"><i class="phone-chan-dot" id="phone-dot-desktop"></i>' +
          '桌面横幅 <b id="phone-st-desktop">加载中</b></span>' +
      '</div>' +
      '<div class="phone-sec-tip phone-dep-tip">' +
        '桌面横幅由 a4agent 常驻进程代发（hook 事件经请求文件队列，每 2 秒扫描一次），' +
        '请保持 a4agent 运行；关闭 a4agent 后手机推送仍可用，桌面横幅会静默丢弃。' +
      '</div>' +
      '<div class="phone-layout">' +
      '<div class="phone-col-left">' +
      '<div class="task-form phone-main-card">' +
        '<div class="task-form-head">手机推送' +
          '<span class="task-active-config">经 ntfy 推送到手机 App，扫码订阅后生效</span></div>' +
        '<div class="task-form-row">' +
          '<label>启用通知</label>' +
          '<input type="checkbox" id="phone-enabled">' +
          '<span class="phone-hint">开启后，任务完成/失败/超时等终态实时推送手机</span>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>桌面横幅</label>' +
          '<input type="checkbox" id="phone-desktop">' +
          '<span class="phone-hint">同一事件同时弹出 Windows 系统通知；与手机推送互不影响，可单独关闭</span>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>ntfy 服务器</label>' +
          '<input type="text" id="phone-server" class="layui-input phone-input" placeholder="https://ntfy.sh">' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>话题</label>' +
          '<input type="text" id="phone-topic" class="layui-input phone-input" autocomplete="off">' +
          '<button class="layui-btn layui-btn-sm" id="phone-regen">换一个</button>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>访问令牌</label>' +
          '<input type="password" id="phone-token" class="layui-input phone-input" autocomplete="new-password" ' +
            'placeholder="可选；自建 ntfy 服务开启鉴权时填写">' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>推送时机</label>' +
          '<label class="phone-check"><input type="checkbox" id="phone-ev-success">成功</label>' +
          '<label class="phone-check"><input type="checkbox" id="phone-ev-failed">失败</label>' +
          '<label class="phone-check"><input type="checkbox" id="phone-ev-timeout">超时</label>' +
          '<label class="phone-check"><input type="checkbox" id="phone-ev-cancelled">已取消</label>' +
        '</div>' +
        '<div class="task-form-row phone-actions">' +
          '<button class="layui-btn layui-btn-normal layui-btn-sm" id="phone-save">保存</button>' +
          '<button class="layui-btn layui-btn-sm" id="phone-test">发送测试通知</button>' +
          '<button class="layui-btn layui-btn-sm" id="phone-test-desktop">测试桌面横幅</button>' +
        '</div>' +
      '</div>' +
      '<div class="task-form phone-sub-card">' +
        '<div class="task-form-head">手机订阅' +
          '<span class="task-active-config">手机安装 ntfy App 后扫码或输入话题名订阅</span></div>' +
        '<div class="phone-qr-area">' +
          '<div id="phone-qrcode"></div>' +
          '<div class="phone-qr-steps">' +
            '<p>1. 手机安装 ntfy App（App Store / Google Play / F-Droid）</p>' +
            '<p>2. 用 App 扫左侧二维码订阅，或手动添加订阅输入下方话题名</p>' +
            '<p>3. 在该订阅的设置里开启「即时交付」，否则消息需手动刷新</p>' +
            '<p class="phone-sub-url">订阅地址：<span id="phone-sub-url"></span></p>' +
          '</div>' +
        '</div>' +
        '<div class="phone-sec-tip">话题名即推送凭据：请勿外传；手机丢失时点「换一个」重置，旧话题立即作废。' +
          'ntfy.sh 为国外免费服务，推送不稳时可自建服务器后改「ntfy 服务器」地址。</div>' +
      '</div>' +
      '</div>' +
      '<div class="phone-col-right">' +
      '<div class="task-form phone-hook-card">' +
        '<div class="task-form-head">OpenCode 接入' +
          '<span class="task-active-config">接管正在运行的 OpenCode：任务下发 + 终局提醒</span></div>' +
        '<div class="task-form-row">' +
          '<label>启用接入</label>' +
          '<input type="checkbox" id="oc-enabled">' +
          '<span class="phone-hint">开启后，「任务下发」可选择 OpenCode 作为引擎，并接收其会话终局提醒</span>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>服务地址</label>' +
          '<input type="text" id="oc-base" class="layui-input phone-input" placeholder="http://127.0.0.1:49374">' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>服务密码</label>' +
          '<input type="password" id="oc-password" class="layui-input phone-input" autocomplete="new-password" ' +
            'placeholder="本机模式留空：自动读取 ~/.config/opencode/service.json">' +
        '</div>' +
        '<div class="task-form-row phone-actions">' +
          '<button class="layui-btn layui-btn-normal layui-btn-sm" id="oc-save">保存</button>' +
          '<button class="layui-btn layui-btn-sm" id="oc-test">测试连接</button>' +
          '<span class="phone-chan-item" style="margin-left:8px;">' +
            '<i class="phone-chan-dot" id="oc-dot"></i>状态 <b id="oc-state">未启用</b></span>' +
        '</div>' +
        '<div class="phone-sec-tip">本机装了 OpenCode 即可直接用：a4agent 会自动读取它的服务密码与默认端口。' +
          '密码用 Windows DPAPI 加密存储，界面永不回显；远程服务需填写上面的地址与密码（留空表示沿用本机配置）。</div>' +
      '</div>' +
      '<div class="task-form phone-hook-card">' +
        '<div class="task-form-head">会话交互' +
          '<span class="task-active-config">终端里 AI 的提问 / 权限请求推手机作答（Claude Code 等）</span></div>' +
        '<div class="task-form-row">' +
          '<label>交互模式</label>' +
          '<div class="seg-control" id="phone-hook-mode-seg">' +
            '<button class="seg-btn" data-mode="home">终端优先</button>' +
            '<button class="seg-btn" data-mode="out">外出 · 手机优先</button>' +
          '</div>' +
        '</div>' +
        '<div class="task-form-row">' +
          '<label>作答等待</label>' +
          '<div class="layui-form phone-hook-timeout-wrap" lay-filter="phone-hook-form">' +
            '<select id="phone-hook-timeout" lay-filter="phone-hook-timeout">' +
              '<option value="30">30 秒</option><option value="60">60 秒</option>' +
              '<option value="120">2 分钟</option><option value="300">5 分钟</option>' +
            '</select>' +
          '</div>' +
        '</div>' +
        '<div class="phone-hint phone-hint-block">' +
          '外出· 手机优先模式下，手机超过这个时间没回复，就自动切回终端继续等。' +
          'WorkBuddy 最多只能等 45 秒，选更大的值对它无效。</div>' +
        '<div class="phone-engine-table">' +
          '<div class="phone-engine-thead">' +
            '<span class="phone-engine-col-name">宿主</span>' +
            '<span class="phone-engine-col-cfg">配置文件</span>' +
            '<span class="phone-engine-col-state">状态</span>' +
            '<span class="phone-engine-col-act">操作</span>' +
          '</div>' +
          '<div id="phone-hook-engines"><span class="phone-hint">加载中…</span></div>' +
        '</div>' +
        '<div class="phone-sec-tip">宿主注册只决定 AI 的提问 / 权限请求是否转推手机；' +
          '桌面横幅由左侧「桌面横幅」开关统一控制。DSH 注册会把内置 Cordis 插件挂进 ' +
          '~/.dsh/profiles 各 profile，重启 dsh 后生效。</div>' +
      '</div>' +
      '</div>' +
      '</div>';
    form.render('select', 'phone-hook-form');
  }

  function fillForm(cfg) {
    document.getElementById('phone-enabled').checked = !!cfg.enabled;
    document.getElementById('phone-desktop').checked = cfg.desktop !== false;
    document.getElementById('phone-server').value = cfg.server || '';
    document.getElementById('phone-topic').value = cfg.topic || '';
    document.getElementById('phone-token').value = '';
    document.getElementById('phone-token').placeholder = cfg.token_set ? '已设置；留空保持不变' : '可选；自建 ntfy 服务开启鉴权时填写';
    var ev = cfg.events || {};
    document.getElementById('phone-ev-success').checked = ev.success !== false;
    document.getElementById('phone-ev-failed').checked = ev.failed !== false;
    document.getElementById('phone-ev-timeout').checked = ev.timeout !== false;
    document.getElementById('phone-ev-cancelled').checked = ev.cancelled === true;
    document.getElementById('phone-sub-url').textContent = cfg.subscribe_url || '';
    renderChannelStatus(cfg);
    renderQR(cfg.qr_payload || '');
  }

  /* ---------- 通道状态总览 ---------- */
  function renderChannelStatus(cfg) {
    var mobile = !!cfg.enabled;
    var desktop = cfg.desktop !== false;
    var mSt = document.getElementById('phone-st-mobile');
    var dSt = document.getElementById('phone-st-desktop');
    var mDot = document.getElementById('phone-dot-mobile');
    var dDot = document.getElementById('phone-dot-desktop');
    if (mSt) mSt.textContent = mobile ? (cfg.topic ? '已开启' : '待生成话题') : '已关闭';
    if (dSt) dSt.textContent = desktop ? '已开启' : '已关闭';
    if (mDot) mDot.className = 'phone-chan-dot' + (mobile ? ' is-on' : '');
    if (dDot) dDot.className = 'phone-chan-dot' + (desktop ? ' is-on' : '');
  }

  function renderQR(text) {
    var box = document.getElementById('phone-qrcode');
    if (!box) return;
    box.innerHTML = '';
    if (!text || typeof QRCode === 'undefined') return;
    new QRCode(box, { text: text, width: 148, height: 148, correctLevel: QRCode.CorrectLevel.M });
  }

  function readForm() {
    return {
      enabled: document.getElementById('phone-enabled').checked,
      desktop: document.getElementById('phone-desktop').checked,
      server: document.getElementById('phone-server').value.trim(),
      topic: document.getElementById('phone-topic').value.trim(),
      token: document.getElementById('phone-token').value.trim(),
      events: {
        success: document.getElementById('phone-ev-success').checked,
        failed: document.getElementById('phone-ev-failed').checked,
        timeout: document.getElementById('phone-ev-timeout').checked,
        cancelled: document.getElementById('phone-ev-cancelled').checked
      }
    };
  }

  /* ---------- 会话交互（hook） ---------- */
  var ENGINE_LABELS = { claude: 'Claude Code', codex: 'Codex', zcode: 'ZCode',
    qoder: 'Qoder', workbuddy: 'WorkBuddy', dsh: 'DeepSeek Harness' };

  function loadHookStatus() {
    return apiGet('/hook/status').then(function (s) {
      document.querySelectorAll('#phone-hook-mode-seg .seg-btn').forEach(function (btn) {
        btn.classList.toggle('seg-active', btn.getAttribute('data-mode') === s.mode);
      });
      document.getElementById('phone-hook-timeout').value = String(s.timeout);
      form.render('select', 'phone-hook-form'); // 程序化赋值后同步 layui 标题与选中态
      // hook 状态接口带回桌面开关，以它为准刷新通道总览
      var box = document.getElementById('phone-desktop');
      if (box && typeof s.desktop === 'boolean') box.checked = s.desktop;
      var dSt = document.getElementById('phone-st-desktop');
      if (dSt) dSt.textContent = s.desktop === false ? '已关闭' : '已开启';
      var dDot = document.getElementById('phone-dot-desktop');
      if (dDot) dDot.className = 'phone-chan-dot' + (s.desktop === false ? '' : ' is-on');
      renderEngines(s.engines);
      return s;
    });
  }

  function renderEngines(engines) {
    var box = document.getElementById('phone-hook-engines');
    if (!box) return;
    box.innerHTML = Object.keys(ENGINE_LABELS).map(function (engine) {
      var st = engines[engine] || { registered: false };
      var path = st.path || '';
      // 配置路径可能很长，中间省略并保留首尾便于辨认宿主
      var shown = path.length > 30
        ? path.slice(0, 14) + '…' + path.slice(-12)
        : path;
      return '<div class="phone-engine-tr">' +
        '<span class="phone-engine-col-name">' + ENGINE_LABELS[engine] + '</span>' +
        '<span class="phone-engine-col-cfg" title="' + escapeHtml(path) + '">' +
          escapeHtml(shown || '—') + '</span>' +
        '<span class="phone-engine-col-state">' +
          '<span class="phone-engine-state' + (st.registered ? ' is-on' : '') + '">' +
            (st.registered ? '已注册' : '未注册') + '</span></span>' +
        '<span class="phone-engine-col-act">' +
          '<button class="layui-btn layui-btn-xs" data-engine="' + engine + '" data-action="' +
          (st.registered ? 'unregister' : 'register') + '">' +
          (st.registered ? '移除' : '注册') + '</button></span>' +
      '</div>';
    }).join('');
    box.querySelectorAll('button[data-engine]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var engine = btn.getAttribute('data-engine');
        var action = btn.getAttribute('data-action');
        apiSend('/hook/' + action, 'POST', { engine: engine })
          .then(function (res) { renderEngines(res.engines); })
          .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      });
    });
  }

  function bindHookActions() {
    var seg = document.getElementById('phone-hook-mode-seg');
    if (!seg) return;
    seg.querySelectorAll('.seg-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        apiSend('/hook/config', 'PUT', { mode: btn.getAttribute('data-mode') })
          .then(function (s) {
            seg.querySelectorAll('.seg-btn').forEach(function (b) {
              b.classList.toggle('seg-active', b.getAttribute('data-mode') === s.mode);
            });
            layer.msg(s.mode === 'out' ? '外出模式：提问/权限请求推手机作答' : '终端优先：手机不参与交互',
              { icon: 1 });
          })
          .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      });
    });
    // layui 渲染的 select 不派发原生 change，走 form.on 过滤器事件；
    // bindHookActions 仅在 boot 时调用一次，不会重复注册
    form.on('select(phone-hook-timeout)', function (data) {
      apiSend('/hook/config', 'PUT', { timeout: Number(data.value) })
        .catch(function (err) { layer.msg(err.message, { icon: 2 }); });
    });
  }

  /* ---------- 动作 ---------- */
  function save() {
    return apiSend('/config', 'PUT', readForm()).then(function (cfg) {
      fillForm(cfg);
      return cfg;
    });
  }

  function bindActions() {
    document.getElementById('phone-save').addEventListener('click', function () {
      save().then(function () { layer.msg('已保存', { icon: 1 }); })
        .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });

    document.getElementById('phone-test').addEventListener('click', function () {
      save().then(function () { return apiSend('/test', 'POST'); })
        .then(function (res) {
          if (res.sent) layer.msg('测试通知已发出，请查看手机', { icon: 1 });
          else layer.alert('推送失败：' + (res.detail || '未知原因'), { icon: 2, title: '测试通知' });
        })
        .catch(function (e) { layer.alert(e.message, { icon: 2, title: '测试通知' }); });
    });

    document.getElementById('phone-test-desktop').addEventListener('click', function () {
      save().then(function () { return apiSend('/test/desktop', 'POST'); })
        .then(function (res) {
          if (res.sent) layer.msg('桌面横幅已触发，请看右下角系统通知', { icon: 1 });
          else layer.alert('桌面横幅未发出：' + (res.detail || '未知原因') +
            '（非 Windows 系统、或系统通知被禁用）', { icon: 2, title: '测试桌面横幅' });
        })
        .catch(function (e) { layer.alert(e.message, { icon: 2, title: '测试桌面横幅' }); });
    });

    document.getElementById('phone-regen').addEventListener('click', function () {
      layer.confirm('换一个新话题？当前话题立即作废，手机需要重新扫码订阅。', { title: '重置话题' },
        function (index) {
          layer.close(index);
          apiSend('/topic/regenerate', 'POST').then(function (cfg) {
            fillForm(cfg);
            layer.msg('已生成新话题，请重新扫码订阅', { icon: 1 });
          }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        });
    });
  }

  /* ---------- OpenCode 接入 ---------- */
  var OC_API = '/api/v1/opencode';

  function ocGet(path) {
    return fetch(OC_API + path, { headers: { 'Content-Type': 'application/json' } })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(parseError(j, r.status));
          return j;
        });
      });
  }

  function ocSend(path, method, body) {
    return fetch(OC_API + path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(parseError(j, r.status));
        return j;
      });
    });
  }

  function renderOcStatus(st) {
    var box = document.getElementById('oc-state');
    var dot = document.getElementById('oc-dot');
    if (!box || !dot) return;
    if (!st.enabled) {
      box.textContent = '未启用';
      dot.className = 'phone-chan-dot';
    } else if (st.reachable) {
      box.textContent = '已连接 · v' + (st.version || '?');
      dot.className = 'phone-chan-dot is-on';
    } else {
      box.textContent = st.detail || '未连接';
      dot.className = 'phone-chan-dot is-off';
    }
  }

  function fillOcForm(st) {
    document.getElementById('oc-enabled').checked = !!st.enabled;
    document.getElementById('oc-base').value = st.base_url || '';
    // 密码不回显：留空表示保持原样（后端按此语义处理）
    document.getElementById('oc-password').value = '';
    renderOcStatus(st);
  }

  function readOcForm() {
    return {
      enabled: document.getElementById('oc-enabled').checked,
      base_url: document.getElementById('oc-base').value.trim(),
      password: document.getElementById('oc-password').value
    };
  }

  function ocSave() {
    return ocSend('/config', 'PUT', readOcForm()).then(fillOcForm);
  }

  function bindOcActions() {
    document.getElementById('oc-save').addEventListener('click', function () {
      ocSave().then(function () { layer.msg('已保存', { icon: 1 }); })
        .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
    });
    document.getElementById('oc-test').addEventListener('click', function () {
      ocSave().then(function () { return ocSend('/test', 'POST'); })
        .then(function (st) {
          renderOcStatus(st);
          if (st.reachable) {
            layer.msg('已连接 OpenCode ' + (st.version || '') + '，任务下发可选用它', { icon: 1 });
          } else {
            layer.alert('连接失败：' + (st.detail || '未知原因'), { icon: 2, title: 'OpenCode 连接' });
          }
        })
        .catch(function (e) { layer.alert(e.message, { icon: 2, title: 'OpenCode 连接' }); });
    });
  }

  function boot() {
    renderShell();
    bindActions();
    bindHookActions();
    bindOcActions();
    apiGet('/config').then(fillForm)
      .catch(function (e) {
        var box = document.getElementById('phone-content');
        if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
      });
    loadHookStatus().catch(function () { /* hook 区随主配置一起由空态兜底 */ });
    ocGet('/config').then(fillOcForm)
      .catch(function (e) {
        var box = document.getElementById('oc-state');
        if (box) box.textContent = '读取失败：' + e.message;
      });
  }

  window.addEventListener('main-tab-changed', function (e) {
    active = e.detail.index === 6;
    if (active && !loaded) { loaded = true; boot(); }
  });
});
