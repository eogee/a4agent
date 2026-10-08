layui.use(['layer', 'form', 'element'], function () {
  var layer = layui.layer;
  var form = layui.form;
  var element = layui.element;
  var API = '/api/v1';

  var providerReturnToConfig = false;

  /* ---------- 工具 ---------- */
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ---------- 列表搜索 ---------- */
  /* 关键词按空格切分，全部命中才算匹配；空关键词返回 null 表示不过滤。 */
  function makeMatcher(query) {
    var tokens = String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!tokens.length) return null;
    return function (g) {
      var text = (String(g.name || '') + ' ' + String(g.description || '')).toLowerCase();
      return tokens.every(function (t) { return text.indexOf(t) !== -1; });
    };
  }

  function noMatchTip(query, label) {
    return '<div class="empty-tip">没有匹配「' + escapeHtml(String(query).trim()) + '」的' + label + '<br>' +
      '<span style="font-size:12px;">按名称与功能描述搜索，空格分隔表示同时满足</span></div>';
  }

  /* 输入防抖 120ms 后回调；清空按钮与 Esc 立即生效 */
  function bindListSearch(inputId, clearId, onChange) {
    var input = document.getElementById(inputId);
    var clear = document.getElementById(clearId);
    var timer = null;

    function syncClearButton() {
      input.parentNode.classList.toggle('has-value', !!input.value);
    }

    function reset() {
      if (timer) { clearTimeout(timer); timer = null; }
      input.value = '';
      syncClearButton();
      onChange('');
    }

    input.addEventListener('input', function () {
      syncClearButton();
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        onChange(input.value);
      }, 120);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') reset();
    });
    clear.addEventListener('click', function () {
      reset();
      input.focus();
    });
  }

  /* 统一解析响应文本：后端异常时可能返回纯文本（如 500 的 Internal Server
   * Error），直接 r.json() 会把 JSON 解析错误当报错弹出，掩盖真实原因。 */
  function parseResponse(r) {
    return r.text().then(function (t) {
      var data;
      try {
        data = t ? JSON.parse(t) : null;
      } catch (e) {
        data = undefined;
      }
      if (!r.ok) {
        var msg = (data && data.detail) || (t ? t.slice(0, 120) : '');
        throw new Error(msg || ('请求失败（HTTP ' + r.status + '）'));
      }
      if (data === undefined) {
        throw new Error('响应不是有效 JSON（HTTP ' + r.status + '）');
      }
      return data;
    });
  }

  function apiGet(path) {
    return fetch(API + path).then(parseResponse);
  }

  function apiSend(path, method, body) {
    return fetch(API + path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(parseResponse);
  }

  /* 从当前打开的配置表单读取值（不依赖 form.getData，兼容性更好） */
  function readFormValues() {
    function val(name) {
      var el = document.querySelector('form[lay-filter="config-form"] [name="' + name + '"]');
      return el ? el.value : '';
    }
    var targets = [];
    if (document.querySelector('form[lay-filter="config-form"] [name="target_claude"]').checked) {
      targets.push('claude');
    }
    if (document.querySelector('form[lay-filter="config-form"] [name="target_codex"]').checked) {
      targets.push('codex');
    }
    return {
      name: val('name'),
      provider_id: val('provider_id'),
      api_key: val('api_key'),
      model: val('model'),
      targets: targets.join(',')
    };
  }

  /* ---------- 状态栏 ---------- */
  function loadStatus() {
    apiGet('/status').then(function (s) {
      var el = document.getElementById('status-text');
      if (s.version) {
        document.getElementById('footer-version').textContent = 'v' + s.version;
      }
      if (s.active_config) {
        var c = s.active_config;
        var pname = c.provider ? c.provider.name : ('#' + c.provider_id);
        var text = c.name + '（' + pname + ' · ' + c.model + '）';
        if (s.current_model && s.current_model !== c.model) {
          text += '  ⚠ 配置文件内为 ' + s.current_model;
        }
        if ((c.targets || '').indexOf('codex') !== -1) {
          text += s.current_codex_model
            ? ' · Codex: ' + s.current_codex_model
            : ' · Codex 待配置';
        }
        el.textContent = text;
        el.classList.add('status-active');
      } else {
        el.textContent = '未设置（使用默认配置）';
        el.classList.remove('status-active');
      }
    }).catch(function (e) {
      document.getElementById('status-text').textContent = '状态获取失败';
    });
  }

  /* ---------- 代理服务开关 ---------- */
  function loadProxyStatus() {
    apiGet('/proxy/status').then(function (s) {
      var sw = document.getElementById('proxy-switch');
      sw.checked = !!s.running;
      sw.disabled = !s.running;
      document.getElementById('proxy-label').textContent =
        s.running ? '代理服务（运行中）' : '代理服务';
      form.render('switch');
    }).catch(function () {});
  }

  form.on('switch(proxy-switch)', function (data) {
    if (data.elem.checked) return; // 只允许关闭，不允许手动开启
    layer.confirm(
      '关闭后当前 OpenAI 服务商将无法使用，需要重新切换配置才能恢复。确定关闭代理服务吗？',
      { title: '关闭代理服务' },
      function (index) {
        apiSend('/proxy/stop', 'POST', {}).then(function () {
          layer.close(index);
          layer.msg('代理服务已关闭', { icon: 1 });
          loadProxyStatus();
        }).catch(function (e) {
          layer.close(index);
          layer.msg(e.message, { icon: 2 });
          loadProxyStatus();
        });
      },
      function () {
        // 取消：恢复为开启状态
        document.getElementById('proxy-switch').checked = true;
        form.render('switch');
      }
    );
  });

  /* ---------- 供应商管理 ---------- */
  function protocolLabel(p) {
    if (p.api_type !== 'openai') return 'Anthropic';
    return p.native_responses ? 'OpenAI 兼容（原生 Responses）' : 'OpenAI 兼容（本地代理）';
  }

  function loadProviders() {
    apiGet('/providers').then(function (list) {
      var box = document.getElementById('provider-list');
      if (!list.length) {
        box.innerHTML = '<div class="empty-tip">还没有供应商，点击右上角「新增供应商」创建</div>';
        return;
      }
      var rows = list.map(function (p) {
        return '<tr>' +
          '<td>' + escapeHtml(p.name) + '</td>' +
          '<td class="provider-base">' + escapeHtml(p.api_base) + '</td>' +
          '<td>' + escapeHtml(protocolLabel(p)) + '</td>' +
          '<td>' + (p.is_custom ? '自定义' : '预置') + '</td>' +
          '<td class="provider-actions">' +
            '<button class="layui-btn layui-btn-xs" data-provider-action="edit" data-id="' + p.id + '">编辑</button>' +
            '<button class="layui-btn layui-btn-xs layui-btn-danger layui-btn-primary" data-provider-action="del" data-id="' + p.id + '" data-name="' + escapeHtml(p.name) + '">删除</button>' +
          '</td>' +
        '</tr>';
      }).join('');
      box.innerHTML =
        '<table class="layui-table"><thead><tr>' +
          '<th>名称</th><th>Base URL</th><th>协议</th><th>来源</th><th style="width:150px">操作</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>';
    }).catch(function (e) {
      document.getElementById('provider-list').innerHTML =
        '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function openProviderForm(editId) {
    var isEdit = editId != null;
    var html = '' +
      '<form class="layui-form provider-form" lay-filter="provider-form">' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">名称</label>' +
          '<div class="layui-input-block"><input type="text" name="name" class="layui-input" placeholder="如：硅基流动"></div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">Base URL</label>' +
          '<div class="layui-input-block"><input type="text" name="api_base" class="layui-input" placeholder="如：https://api.siliconflow.cn/v1"></div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">协议类型</label>' +
          '<div class="layui-input-block"><select name="api_type" lay-filter="provider-form">' +
            '<option value="anthropic">Anthropic（Claude Code 原生）</option>' +
            '<option value="openai">OpenAI 兼容（走本地翻译代理）</option>' +
          '</select>' +
          '<div class="provider-hint" style="color:#8a8e94;">Codex 需使用 OpenAI 兼容（Responses）接口；上游原生支持 Responses 时可勾选下方开关直连，否则经本地代理。</div>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item native-responses-item" style="display:none;">' +
          '<label class="layui-form-label">原生 Responses</label>' +
          '<div class="layui-input-block">' +
            '<input type="checkbox" name="native_responses" lay-skin="switch" lay-text="直连|代理">' +
            '<div class="provider-hint" style="color:#8a8e94;">上游原生支持 OpenAI Responses（如 DeepSeek）时开启：Codex 将直连上游，无需本地代理。</div>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">自定义</label>' +
          '<div class="layui-input-block"><input type="checkbox" name="is_custom" lay-skin="switch" checked></div>' +
        '</div>' +
      '</form>';

    layer.open({
      type: 1,
      title: isEdit ? '编辑供应商' : '新增供应商',
      area: ['500px', 'auto'],
      content: html,
      success: function () {
        if (isEdit) {
          apiGet('/providers/' + editId).then(function (p) {
            document.querySelector('input[name="name"]').value = p.name;
            document.querySelector('input[name="api_base"]').value = p.api_base;
            document.querySelector('select[name="api_type"]').value = p.api_type;
            document.querySelector('input[name="is_custom"]').checked = !!p.is_custom;
            document.querySelector('input[name="native_responses"]').checked = !!p.native_responses;
            toggleNativeResponses();
            form.render(null, 'provider-form');
          });
        } else {
          toggleNativeResponses();
          form.render(null, 'provider-form');
        }
      },
      btn: ['保存', '取消'],
      yes: function (index) {
        var name = document.querySelector('input[name="name"]').value.trim();
        var apiBase = document.querySelector('input[name="api_base"]').value.trim();
        var apiType = document.querySelector('select[name="api_type"]').value;
        var isCustom = document.querySelector('input[name="is_custom"]').checked;
        var nativeResponses = !!document.querySelector('input[name="native_responses"]').checked;
        if (!name || !apiBase) {
          layer.msg('请填写名称和 Base URL', { icon: 2 });
          return;
        }
        var body = { name: name, api_base: apiBase, api_type: apiType, native_responses: nativeResponses, is_custom: isCustom };
        var req = isEdit
          ? apiSend('/providers/' + editId, 'PUT', body)
          : apiSend('/providers', 'POST', body);
        req.then(function () {
          layer.close(index);
          layer.msg('保存成功', { icon: 1 });
          loadProviders();
          if (!isEdit && providerReturnToConfig) {
            providerReturnToConfig = false;
            element.tabChange('main-tab', 'configs');
            openForm(null);
          }
        }).catch(function (e) {
          layer.msg(e.message, { icon: 2 });
        });
      }
    });
  }

  function toggleNativeResponses() {
    var sel = document.querySelector('select[name="api_type"]');
    var item = document.querySelector('.native-responses-item');
    if (sel && item) {
      item.style.display = sel.value === 'openai' ? '' : 'none';
    }
  }

  form.on('select(provider-form)', function (data) {
    if (data.elem.name === 'api_type') {
      toggleNativeResponses();
    }
  });

  function confirmDeleteProvider(id, name) {
    layer.confirm('确定删除供应商「' + escapeHtml(name) + '」？' +
      '<br><span style="font-size:12px;color:#8a8e94;">该供应商下有配置方案时无法删除</span>',
      { title: '删除确认' }, function (index) {
        apiSend('/providers/' + id, 'DELETE', {}).then(function () {
          layer.close(index);
          layer.msg('已删除', { icon: 1 });
          loadProviders();
        }).catch(function (e) {
          layer.msg(e.message, { icon: 2 });
        });
      });
  }

  // layui 对同一 filter 的 element.on 是覆盖式注册（后注册者替换先注册者），
  // 因此本文件是唯一注册 tab(main-tab) 的地方；其他模块（llama.js 等）
  // 通过下面的 DOM 自定义事件感知 tab 切换。
  element.on('tab(main-tab)', function (data) {
    window.dispatchEvent(new CustomEvent('main-tab-changed', { detail: { index: data.index } }));
    if (data.index === 1) loadProviders();
    else if (data.index === 2 && !skillData) loadSkills();
    else if (data.index === 3 && !mcpData) loadMcps();
  });

  /* ---------- 卡片 ---------- */
  // v0.5.1 起只代管 Claude Code 与 Codex。三端（dsh/ZCode/pi）自 v0.5.1
  // 起改由用户在应用内自配，旧数据里残留的值以灰色「已移交」样式呈现，
  // 告诉用户曾经配过、现在归谁管——而不是静默消失让人以为没配过。
  var TARGET_HANDED_OVER = { dsh: 'dsh', zcode: 'ZCode', pi: 'pi' };

  function targetBadges(targets) {
    var list = (targets || 'claude').split(',');
    var html = '';
    list.forEach(function (t) {
      if (t === 'codex') html += '<span class="target-badge target-codex">Codex</span>';
      else if (t === 'claude') html += '<span class="target-badge target-claude">Claude</span>';
      else if (TARGET_HANDED_OVER[t]) {
        html += '<span class="target-badge target-handed" title="该端已改为在应用内自配，本工具不再代管">'
          + TARGET_HANDED_OVER[t] + ' · 已移交</span>';
      }
    });
    return html;
  }

  function buildCard(c) {
    var activeCls = c.is_active ? ' card-active' : '';
    var badge = c.is_active ? '<div class="active-badge">使用中</div>' : '';
    var pname = c.provider ? c.provider.name : ('#' + c.provider_id);
    return '' +
      '<div class="config-card' + activeCls + '" data-id="' + c.id + '" data-targets="' + escapeHtml(c.targets) + '">' +
        badge +
        '<div class="card-name">' + escapeHtml(c.name) + '</div>' +
        '<div class="card-meta">' + escapeHtml(pname) + ' · ' + escapeHtml(c.model) + '</div>' +
        '<div class="card-targets">' + targetBadges(c.targets) + '</div>' +
        '<div class="card-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" data-action="switch" data-name="' + escapeHtml(c.name) + '">切换</button>' +
          '<button class="layui-btn layui-btn-sm" data-action="edit">编辑</button>' +
          '<button class="layui-btn layui-btn-sm layui-btn-danger layui-btn-primary" data-action="del" data-name="' + escapeHtml(c.name) + '">删除</button>' +
        '</div>' +
      '</div>';
  }

  function loadConfigs() {
    apiGet('/configs').then(function (list) {
      var grid = document.getElementById('card-grid');
      if (!list.length) {
        grid.innerHTML = '<div class="empty-tip">还没有配置方案，点击右上角「新增配置」创建</div>';
        return;
      }
      grid.innerHTML = list.map(buildCard).join('');
    }).catch(function (e) {
      document.getElementById('card-grid').innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  /* ---------- 切换 ---------- */
  function confirmSwitch(id, name, targets) {
    var hasClaude = (targets || '').indexOf('claude') !== -1;
    var hasCodex = (targets || '').indexOf('codex') !== -1;
    var restartHtml = hasClaude
      ? '<div class="layui-form" style="margin-top:16px;">' +
          '<input type="checkbox" id="chk-restart" lay-skin="primary" title="切换后重启 Claude Code（若正在运行）">' +
        '</div>'
      : '';
    var codexNote = hasCodex
      ? '<p style="font-size:12px;color:#8a8e94;margin-top:10px;">Codex 配置写入后需重启 Codex 才生效</p>'
      : '';
    // 旧数据里可能还带着已移交的三端，在这里如实告知它们不再被写入
    var handedOver = ['dsh', 'zcode', 'pi'].filter(function (t) {
      return (targets || '').indexOf(t) !== -1;
    });
    var handedNote = handedOver.length
      ? '<p style="font-size:12px;color:#8a8e94;margin-top:10px;">'
        + handedOver.map(function (t) {
            return ({ dsh: 'dsh', zcode: 'ZCode', pi: 'pi' })[t];
          }).join(' / ')
        + ' 的配置不再由本工具写入，请在该应用内自行配置</p>'
      : '';
    layer.open({
      type: 1,
      title: '确认切换',
      area: ['420px', 'auto'],
      content: '<div style="padding:20px 24px;">' +
        '<p style="font-size:15px;">确定切换到「' + escapeHtml(name) + '」？</p>' +
        restartHtml + codexNote + handedNote + '</div>',
      btn: ['确认切换', '取消'],
      success: function () {
        if (hasClaude) form.render('checkbox');
      },
      yes: function (index) {
        var chk = document.getElementById('chk-restart');
        var restart = !!(chk && chk.checked);
        doSwitch(id, restart, index);
      }
    });
  }

  function doSwitch(id, restart, layerIndex) {
    apiSend('/switch/' + id, 'POST', { restart: restart }).then(function (res) {
      layer.close(layerIndex);
      var info = res.process_info ? (res.process_info.detail || '') : '';
      layer.msg((res.message || '切换成功') + (info ? '，' + info : ''), { icon: 1, time: 3200 });
      loadConfigs();
      loadStatus();
      loadProxyStatus();
    }).catch(function (e) {
      layer.close(layerIndex);
      layer.msg(e.message, { icon: 2, time: 3000 });
    });
  }

  /* ---------- 新增 / 编辑 ---------- */
  function providerOptions(providers, selected) {
    return providers.map(function (p) {
      var sel = (selected && p.id === selected) ? ' selected' : '';
      var tag = p.api_type === 'openai' ? (p.native_responses ? '（原生Responses）' : '（OpenAI）') : '';
      return '<option value="' + p.id + '"' + sel + '>' + escapeHtml(p.name) + tag + '</option>';
    }).join('');
  }

  function openForm(editId) {
    apiGet('/providers').then(function (providers) {
      var isEdit = editId != null;
      var html = '' +
        '<form class="layui-form config-form" lay-filter="config-form">' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">方案名称</label>' +
            '<div class="layui-input-block"><input type="text" name="name" class="layui-input" placeholder="如：日常开发 / 写作" value=""></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">服务商</label>' +
            '<div class="layui-input-block">' +
              '<select name="provider_id" lay-search>' +
                '<option value="">选择或搜索服务商</option>' + providerOptions(providers) +
              '</select>' +
              '<div class="provider-hint"><a href="javascript:;" id="link-add-provider">没有你的供应商？点此添加</a></div>' +
            '</div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">API Key</label>' +
            '<div class="layui-input-block"><input type="password" name="api_key" class="layui-input" placeholder="' + (isEdit ? '留空则不修改' : '粘贴 API Key') + '"></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">模型</label>' +
            '<div class="layui-input-block"><input type="text" name="model" class="layui-input" placeholder="如：glm-4.7-flash"></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">应用目标</label>' +
            '<div class="layui-input-block target-checkboxes">' +
              '<input type="checkbox" name="target_claude" title="Claude Code" lay-skin="primary" checked>' +
              '<input type="checkbox" name="target_codex" title="Codex" lay-skin="primary">' +
            '</div>' +
            '<div class="layui-form-mid layui-word-aux" style="margin-left:110px;">dsh / ZCode / pi 已在各自应用内支持自定义供应商，请直接在那里配置</div>' +
          '</div>' +
        '</form>';

      if (isEdit) {
        apiGet('/configs/' + editId).then(function (c) {
          openFormLayer(isEdit, html, c, providers);
        });
      } else {
        openFormLayer(isEdit, html, null, providers);
      }
    });
  }

  function openFormLayer(isEdit, html, c, providers) {
    layer.open({
      type: 1,
      title: isEdit ? '编辑配置方案' : '新增配置方案',
      area: ['500px', 'auto'],
      content: html,
      success: function () {
        if (c) {
          document.querySelector('input[name="name"]').value = c.name;
          document.querySelector('select[name="provider_id"]').value = String(c.provider_id);
          document.querySelector('input[name="model"]').value = c.model;
          var targets = (c.targets || 'claude').split(',');
          document.querySelector('input[name="target_claude"]').checked = targets.indexOf('claude') !== -1;
          document.querySelector('input[name="target_codex"]').checked = targets.indexOf('codex') !== -1;
        }
        form.render(null, 'config-form');
        var link = document.getElementById('link-add-provider');
        if (link) {
          link.addEventListener('click', function () {
            providerReturnToConfig = true;
            layer.closeAll();
            element.tabChange('main-tab', 'providers');
            openProviderForm(null);
          });
        }
      },
      btn: ['保存', '取消'],
      yes: function (index) {
        var data = readFormValues();
        if (!data.name || !data.provider_id || !data.model) {
          layer.msg('请填写方案名称、服务商和模型', { icon: 2 });
          return;
        }
        if (!isEdit && !data.api_key) {
          layer.msg('请填写 API Key', { icon: 2 });
          return;
        }
        if (!data.targets) {
          layer.msg('请至少选择一个应用目标（Claude Code / Codex）', { icon: 2 });
          return;
        }
        // Codex 用 OpenAI 兼容协议：保存前拦截 Anthropic 服务商 + 勾选 Codex
        var selProvider = providers.find(function (p) { return p.id === Number(data.provider_id); });
        var needOpenai = data.targets.indexOf('codex') !== -1;
        if (needOpenai && selProvider && selProvider.api_type !== 'openai') {
          layer.msg('Codex 需使用 OpenAI 兼容接口，请更换服务商或去掉 Codex 目标', { icon: 2 });
          return;
        }
        var body = {
          name: data.name,
          provider_id: Number(data.provider_id),
          model: data.model,
          targets: data.targets
        };
        if (data.api_key) body.api_key = data.api_key;

        var req = isEdit
          ? apiSend('/configs/' + c.id, 'PUT', body)
          : apiSend('/configs', 'POST', body);

        req.then(function () {
          layer.close(index);
          layer.msg('保存成功', { icon: 1 });
          loadConfigs();
        }).catch(function (e) {
          layer.msg(e.message, { icon: 2 });
        });
      }
    });
  }

  /* ---------- 删除 ---------- */
  function confirmDelete(id, name) {
    layer.confirm('确定删除「' + escapeHtml(name) + '」？', { title: '删除确认' }, function (index) {
      apiSend('/configs/' + id, 'DELETE', {}).then(function () {
        layer.close(index);
        layer.msg('已删除', { icon: 1 });
        loadConfigs();
        loadStatus();
      }).catch(function (e) {
        layer.msg(e.message, { icon: 2 });
      });
    });
  }

  /* ---------- 自动更新 ---------- */
  var updateDlTimer = null;

  function fmtBytes(n) {
    if (!n) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (n >= 10 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function checkUpdate(silent) {
    apiGet('/update/check').then(function (r) {
      if (r.status === 'update_available') {
        // 之前已下载校验通过 → 直接提示应用，避免重复下载
        if (r.downloaded) {
          showApplyConfirm(r.downloaded_version || r.latest_version);
        } else {
          showUpdatePrompt(r);
        }
      } else if (!silent) {
        if (r.status === 'up_to_date') {
          layer.msg('已是最新版本 v' + escapeHtml(r.current_version), { icon: 1 });
        } else if (r.status === 'ignored') {
          layer.msg('已忽略该版本（v' + escapeHtml(r.latest_version) + '）', { icon: 0 });
        } else if (r.status === 'too_old') {
          layer.msg(r.error || '当前版本过旧，请下载完整安装包更新', { icon: 2 });
        } else {
          layer.msg('检查更新失败：' + (r.error || '网络或服务器不可达'), { icon: 2 });
        }
      }
    }).catch(function (e) {
      if (!silent) layer.msg('检查更新失败：' + e.message, { icon: 2 });
    });
  }

  // v0.5.1 起 API 切换不再代管 dsh/ZCode/pi——这是破坏性变更，必须在更新弹窗里
  // 明确告知，不能埋在更新说明的正文里（用户多半不会点开「查看完整发布说明」）。
  // 这里按「受影响版本区间」判断是否命中，命中就把横幅提到最前面。
  //   affectedBelow：变更影响的最高版本（含）。用 <= 而非 <——紧邻上一版的用户
  //     正是最需要看到这条提示的人，不能因版本号恰好相等而漏掉
  //   landedIn：变更在哪个版本引入（目标版本达到它才需要提示）
  var BREAKING_CHANGES = [{
    affectedBelow: '0.5.0',
    landedIn: '0.5.1',
    title: 'dsh / ZCode / pi 不再由 a4agent 代管 API 配置',
    detail: '这三端应用自带完整的供应商配置界面，由你在应用内自行配置更可靠。' +
      '你原先用 a4agent 配的条目已自动清理（清理前已做永久快照），你自己配的 provider 与 Key 一概未动。' +
      '如需继续使用，请到各应用设置里重新配置一次。'
  }];

  // 版本号按三元组数组比较。注意不能直接 parseFloat('0.5.0')——三段式版本号
  // 不是合法数字会得到 NaN，比较恒为 false，横幅就永远不会显示；
  // 也不能只取前两段（'0.5.0' 与 '0.5.1' 都会被压成 0.5 而丢掉修订号）。
  function parseVersion(v) {
    var m = String(v || '').match(/(\d+)\.(\d+)(?:\.(\d+))?/);
    return m ? [+m[1], +m[2], +(m[3] || 0)] : [0, 0, 0];
  }

  function compareVersion(a, b) {
    for (var i = 0; i < 3; i++) {
      if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
    }
    return 0;
  }

  function breakingChangesFor(currentVersion, latestVersion) {
    var cur = parseVersion(currentVersion);
    var latest = parseVersion(latestVersion);
    return BREAKING_CHANGES.filter(function (c) {
      return compareVersion(cur, parseVersion(c.affectedBelow)) <= 0
        && compareVersion(latest, parseVersion(c.landedIn)) >= 0;
    });
  }

  function breakingBanner(currentVersion, latestVersion) {
    var items = breakingChangesFor(currentVersion, latestVersion);
    if (!items.length) return '';
    return items.map(function (c) {
      return '<div class="update-breaking">' +
        '<div class="update-breaking-title">破坏性变更：' + escapeHtml(c.title) + '</div>' +
        '<div class="update-breaking-detail">' + escapeHtml(c.detail) + '</div>' +
      '</div>';
    }).join('');
  }

  function showUpdatePrompt(r) {
    var notes = (r.notes || '').trim();
    var notesHtml = '<div class="update-notes">' + (notes ? renderMarkdown(notes) : '暂无更新说明') + '</div>';
    if (r.notes_url && /^https:\/\//.test(r.notes_url)) {
      notesHtml += '<div class="update-notes-url"><a href="' + escapeHtml(r.notes_url) + '" target="_blank" rel="noopener">查看完整发布说明</a></div>';
    }
    var breaking = breakingBanner(r.current_version, r.latest_version);
    layer.open({
      type: 1,
      title: '发现新版本 v' + escapeHtml(r.latest_version),
      area: ['720px', 'auto'],
      content: '<div class="update-panel">' +
        '<p class="update-versions">当前 <b>v' + escapeHtml(r.current_version) + '</b> → 最新 <b>v' + escapeHtml(r.latest_version) + '</b></p>' +
        breaking + notesHtml + '</div>',
      btn: ['立即下载更新', '忽略此版本', '暂不'],
      yes: function (index) {
        layer.close(index);
        startDownload(r.latest_version);
      },
      btn2: function (index) {
        layer.close(index);
        apiSend('/update/ignore', 'POST', { version: r.latest_version }).catch(function () {});
      },
      btn3: function (index) { layer.close(index); }
    });
  }

  function startDownload(version) {
    var index = layer.open({
      type: 1,
      title: '正在下载更新 v' + escapeHtml(version),
      area: ['440px', 'auto'],
      content: '<div class="update-download">' +
        '<div class="layui-progress layui-progress-big" lay-showpercent="true">' +
          '<div class="layui-progress-bar layui-bg-green" style="width:0%"></div>' +
        '</div>' +
        '<p class="update-download-status" id="update-dl-status">准备中…</p></div>',
      btn: ['取消下载'],
      yes: function (idx) {
        apiSend('/update/cancel', 'POST', {}).then(function () { layer.close(idx); }).catch(function () {});
      }
    });
    apiSend('/update/download', 'POST', { version: version }).then(function () {
      pollDownloadProgress(index, version);
    }).catch(function (e) {
      layer.close(index);
      layer.msg('开始下载失败：' + e.message, { icon: 2 });
    });
  }

  function pollDownloadProgress(layerIndex, version) {
    clearInterval(updateDlTimer);
    updateDlTimer = setInterval(function () {
      apiGet('/update/progress').then(function (p) {
        if (p.status === 'queued' || p.status === 'downloading') {
          var pct = p.total ? Math.min(100, Math.round(p.downloaded / p.total * 100)) : 0;
          var bar = document.querySelector('.update-download .layui-progress-bar');
          if (bar) bar.style.width = pct + '%';
          var st = document.getElementById('update-dl-status');
          if (st) st.textContent = '已下载 ' + fmtBytes(p.downloaded) + (p.total ? ' / ' + fmtBytes(p.total) : '');
        } else if (p.status === 'done') {
          clearInterval(updateDlTimer);
          layer.close(layerIndex);
          showApplyConfirm(version);
        } else if (p.status === 'cancelled') {
          clearInterval(updateDlTimer);
          layer.close(layerIndex);
          layer.msg('已取消下载', { icon: 0 });
        } else if (p.status === 'failed') {
          clearInterval(updateDlTimer);
          layer.close(layerIndex);
          layer.msg('下载失败：' + (p.error || '未知错误'), { icon: 2 });
        }
      }).catch(function () { /* 瞬时错误继续轮询 */ });
    }, 500);
  }

  function showApplyConfirm(version) {
    layer.open({
      type: 1,
      title: '更新包下载完成',
      area: ['420px', 'auto'],
      content: '<div class="update-panel">' +
        '<p>v' + escapeHtml(version) + ' 安装包已下载并通过 SHA256 校验。</p>' +
        '<p class="update-warn">是否现在运行安装程序完成更新？更新过程中 a4agent 将自动关闭。</p></div>',
      btn: ['立即更新', '稍后'],
      yes: function (index) {
        layer.close(index);
        doApply();
      },
      btn2: function (index) { layer.close(index); }
    });
  }

  function doApply() {
    var tip = layer.msg('正在启动更新安装程序，a4agent 即将关闭…', { icon: 1, time: 0 });
    apiSend('/update/apply', 'POST', {}).then(function () {
      // 应用随即退出，这里无需清理
    }).catch(function (e) {
      layer.close(tip);
      layer.msg('启动更新失败：' + e.message, { icon: 2 });
    });
  }

  /* ---------- 技能管理 ---------- */
  var TOOL_LABEL = { claude: 'Claude', codex: 'Codex', dsh: 'dsh', zcode: 'ZCode', pi: 'pi', qoder: 'Qoder' };
  // 六端清单唯一定义处：新增/删除端只改这里（后端 TOOLS 与 schemas 闸门需同步）
  var TOOL_KEYS = ['claude', 'codex', 'dsh', 'zcode', 'pi', 'qoder'];
  // MCP 项目级只有这四端有独立文件：dsh 是全局 profile 层，Qoder 复用 Claude Code 的 <repo>/.mcp.json
  var MCP_PROJECT_TOOLS = ['claude', 'codex', 'zcode', 'pi'];
  var skillView = 'global'; // global | project
  var skillData = null;
  var skillQuery = ''; // 搜索关键词，仅前端过滤已缓存的 skillData
  var migrationBusy = false; // 迁移/适配执行中：阻塞其他技能操作

  /* 迁移执行期间拦截一切点击（捕获阶段），进度层之外无任何可操作目标 */
  document.addEventListener('click', function (e) {
    if (!migrationBusy) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  function endBadge(t) {
    return '<span class="target-badge target-' + t + '">' + TOOL_LABEL[t] + '</span>';
  }

  function scopeLabel(scope, project) {
    return scope === 'global' ? '全局' : '项目「' + project + '」';
  }

  function findGroup(scope, project, name) {
    if (!skillData) return null;
    var pool = [];
    if (scope === 'global') pool = skillData.global || [];
    else {
      (skillData.projects || []).forEach(function (p) {
        if (p.project === project) pool = p.skills || [];
      });
    }
    var lowered = String(name || '').toLowerCase();
    return pool.find(function (g) { return g.name.toLowerCase() === lowered; }) || null;
  }

  function skillGroupCard(g, scope, project) {
    var dup = g.end_count > 1
      ? '<span class="target-badge dup-badge">' + g.end_count + ' 端存在</span>'
      : '';
    var badges = g.ends.map(endBadge).join('');
    var copies = g.copies.map(function (c) {
      var p = c.path || '';
      return '<div class="skill-copy" data-copy>' +
        '<span class="target-badge target-' + c.tool + '">' + TOOL_LABEL[c.tool] + '</span>' +
        '<span class="skill-copy-path" title="' + escapeHtml(p) + '">' + escapeHtml(p) + '</span>' +
        '<span class="skill-copy-actions">' +
          '<a href="javascript:;" data-sk="open" data-path="' + escapeHtml(p) + '">打开</a>' +
          '<a href="javascript:;" data-sk="delcopy" data-path="' + escapeHtml(p) + '">删除</a>' +
        '</span>' +
      '</div>';
    }).join('');
    var desc = g.description
      ? '<div class="card-meta skill-desc" title="' + escapeHtml(g.description) + '">' + escapeHtml(g.description) + '</div>'
      : '<div class="card-meta skill-desc skill-desc-none">无描述</div>';
    return '' +
      '<div class="config-card skill-card" data-group="' + escapeHtml(g.name) + '" data-scope="' + scope + '" data-project="' + escapeHtml(project || '') + '">' +
        '<div class="skill-badges">' + badges + dup + '</div>' +
        '<div class="card-name">' + escapeHtml(g.name) + '</div>' +
        desc +
        '<div class="skill-copies">' + copies + '</div>' +
        '<div class="card-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" data-sk="migrate">迁移…</button>' +
          '<button class="layui-btn layui-btn-sm" data-sk="preview" data-path="' + escapeHtml((g.copies[0] || {}).path || '') + '">预览</button>' +
        '</div>' +
      '</div>';
  }

  function renderSkills() {
    var box = document.getElementById('skills-content');
    if (!skillData) { box.innerHTML = '<div class="empty-tip">加载中…</div>'; return; }
    var match = makeMatcher(skillQuery);
    if (skillView === 'global') {
      box.classList.add('card-grid');
      var gs = skillData.global || [];
      if (match) gs = gs.filter(match);
      if (!gs.length) {
        if (match) { box.innerHTML = noMatchTip(skillQuery, '技能'); return; }
        box.innerHTML = '<div class="empty-tip">六端的全局目录还没有任何 skill<br>' +
          '<span style="font-size:12px;">~/.claude/skills · ~/.codex/skills · ~/.dsh/skills · ~/.zcode/skills · ~/.pi/agent/skills · ~/.qoder/skills</span></div>';
        return;
      }
      box.innerHTML = gs.map(function (g) { return skillGroupCard(g, 'global', ''); }).join('');
      return;
    }
    var projects = skillData.projects || [];
    box.classList.remove('card-grid');
    if (match) {
      projects = projects.map(function (p) {
        return {
          project: p.project,
          root: p.root,
          skills: (p.skills || []).filter(match),
        };
      }).filter(function (p) { return p.skills.length; });
      if (!projects.length) { box.innerHTML = noMatchTip(skillQuery, '技能'); return; }
    }
    if (!projects.length) {
      box.innerHTML = '<div class="empty-tip">未发现任何含 skill 的项目，可点击右上角「项目根目录」调整扫描范围</div>';
      return;
    }
    var html = '';
    projects.forEach(function (p) {
      html += '<div class="proj-block">' +
        '<div class="proj-head">' +
          '<span class="proj-name">' + escapeHtml(p.project) + '</span>' +
          '<span class="proj-root" title="' + escapeHtml(p.root) + '">' + escapeHtml(p.root) + '</span>' +
          '<button class="layui-btn layui-btn-xs" data-sk="adapt" data-project="' + escapeHtml(p.project) + '">一键适配六端</button>' +
        '</div>';
      if (!p.skills.length) {
        html += '<div class="empty-tip proj-empty">该项目下没有 skill</div>';
      } else {
        html += '<div class="proj-grid">' + p.skills.map(function (g) {
          return skillGroupCard(g, 'project', p.project);
        }).join('') + '</div>';
      }
      html += '</div>';
    });
    box.innerHTML = html;
  }

  function loadSkills() {
    document.getElementById('skills-content').innerHTML = '<div class="empty-tip">加载中…</div>';
    apiGet('/skills/discover').then(function (d) {
      skillData = d;
      renderSkills();
    }).catch(function (e) {
      document.getElementById('skills-content').innerHTML =
        '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function setSkillView(view) {
    skillView = view;
    document.querySelectorAll('#skills-scope-seg .seg-btn').forEach(function (b) {
      b.classList.toggle('seg-active', b.getAttribute('data-view') === view);
    });
    if (skillData) renderSkills();
  }

  /* ---- 迁移结果汇总提示 ---- */
  function migrationSummaryToast(res) {
    var tip = '已迁移 ' + res.migrated + ' 处';
    if (res.skipped) tip += '，跳过 ' + res.skipped;
    if (res.conflicts_trashed) tip += '，旧版入回收站 ' + res.conflicts_trashed;
    if (res.failed) tip += '，失败 ' + res.failed;
    layer.msg(tip, { icon: res.failed ? 2 : 1, time: 3200 });
  }

  /* ---- 迁移进度层：串行执行迁移任务并展示进度条 ----
   * 执行期间全屏遮罩且弹窗不可关闭，禁止用户进行其他点击与操作。
   * tasks: [{ label, source, targets }]，每个任务对应一次 migrate 调用；
   * endpoint 为迁移 API 路径（skills / mcp 二选一，默认技能）；
   * 单个任务失败不中断后续任务，最终由 done(agg) 汇总提示。 */
  function runMigrateTasks(tasks, title, done, endpoint) {
    var total = tasks.length;
    var agg = { migrated: 0, skipped: 0, conflicts_trashed: 0, failed: 0 };
    var migratePath = endpoint || '/skills/migrate';
    migrationBusy = true;

    function setProgress(finished, label, taskFailed) {
      var bar = document.getElementById('mig-progress-bar');
      var txt = document.getElementById('mig-progress-text');
      var pct = total ? Math.round((finished / total) * 100) : 100;
      if (bar) bar.style.width = pct + '%';
      if (txt) {
        txt.innerHTML = '<span class="mig-progress-count">（' + finished + '/' + total + '）</span>' +
          escapeHtml(label || '') + (taskFailed ? '<span class="mig-fail"> 失败</span>' : '');
      }
    }

    function run(i) {
      if (i >= total) {
        layer.close(layerIndex);
        done(agg);
        return;
      }
      var t = tasks[i];
      setProgress(i, t.label, false);
      apiSend(migratePath, 'POST', { sources: [t.source], targets: t.targets })
        .then(function (res) {
          agg.migrated += res.migrated || 0;
          agg.skipped += res.skipped || 0;
          agg.conflicts_trashed += res.conflicts_trashed || 0;
          agg.failed += res.failed || 0;
          setProgress(i + 1, t.label, false);
          run(i + 1);
        })
        .catch(function () {
          agg.failed += 1;
          setProgress(i + 1, t.label, true);
          run(i + 1);
        });
    }

    var layerIndex = layer.open({
      type: 1,
      title: false,
      closeBtn: 0,
      btn: false,
      shade: [0.45, '#000'],
      shadeClose: false,
      skin: 'mig-progress-skin',
      area: ['460px', 'auto'],
      content:
        '<div class="mig-progress">' +
          '<div class="mig-progress-title">' + escapeHtml(title) + '</div>' +
          '<div class="layui-progress layui-progress-big">' +
            '<div class="layui-progress-bar mig-progress-bar" id="mig-progress-bar" style="width:0%;"></div>' +
          '</div>' +
          '<div class="mig-progress-text" id="mig-progress-text">准备中…（0/' + total + '）</div>' +
          '<div class="mig-progress-hint">迁移进行中，请勿进行其他操作</div>' +
        '</div>',
      success: function () { run(0); },
      end: function () { migrationBusy = false; }
    });
  }

  /* ---- 迁移弹窗：指定源副本与任意（工具×项目/全局）目标 ---- */
  /* 原生选文件夹：桌面端走 pywebview 原生对话框；浏览器端退化为输入框。
   * 返回 Promise<string|null>，取消时 resolve(null)。 */
  function pickFolderAny() {
    var wv = window.pywebview;
    function call(api) {
      return Promise.resolve(api.select_folder()).then(function (p) { return p || null; });
    }
    if (wv && wv.api) return call(wv.api);
    if (wv) {
      return new Promise(function (resolve) {
        window.addEventListener('pywebviewready', function () {
          resolve(call(window.pywebview.api));
        }, { once: true });
      });
    }
    return new Promise(function (resolve) {
      layer.prompt({ title: '输入项目文件夹的完整路径', formType: 0 }, function (val, idx) {
        layer.close(idx);
        resolve((val || '').trim() || null);
      });
    });
  }

  function openMigrateDialog(scope, project, name) {
    if (migrationBusy) return;
    var g = findGroup(scope, project, name);
    if (!g) { layer.msg('数据已过期，请刷新后重试', { icon: 2 }); return; }

    var CUSTOM = '__custom__';
    var customRoot = '';  // 自选项目文件夹（跨源切换保持）

    function destRow(destScope, tool, destProject, srcCopy) {
      var isSrc = !!srcCopy
        && destScope === srcCopy.scope
        && tool === srcCopy.tool
        && (destProject || '') === (srcCopy.project || '');
      return '<label class="mig-item' + (isSrc ? ' mig-disabled' : '') + '">' +
        '<input type="checkbox" data-mig="' + destScope + '|' + tool + '|' + (destProject || '') + '"' + (isSrc ? ' disabled' : '') + '>' +
        '<span class="target-badge target-' + tool + '">' + TOOL_LABEL[tool] + '</span>' +
        '<span class="mig-place">' + (destScope === 'global' ? '全局' : escapeHtml(destProject)) + '</span>' +
      '</label>';
    }

    function buildPanel(srcIdx) {
      var src = g.copies[srcIdx] || {};
      var html = '<div class="mig-panel">' +
        '<div class="mig-section"><div class="mig-title">迁移哪一份？</div>' +
        g.copies.map(function (c, i) {
          return '<label class="mig-item">' +
            '<input type="radio" name="mig-src" value="' + i + '"' + (i === srcIdx ? ' checked' : '') + '>' +
            '<span class="target-badge target-' + c.tool + '">' + TOOL_LABEL[c.tool] + '</span>' +
            '<span class="mig-place" title="' + escapeHtml(c.path || '') + '">' + scopeLabel(c.scope, c.project) + '</span>' +
          '</label>';
        }).join('') + '</div>';
      html += '<div class="mig-section"><div class="mig-title">复制到哪些位置？（目标端同名旧版将移入回收站）</div>';
      html += '<div class="mig-group"><span class="mig-group-name">全局</span>';
      TOOL_KEYS.forEach(function (t) { html += destRow('global', t, '', src); });
      html += '</div>';
      (skillData.projects || []).forEach(function (p) {
        html += '<div class="mig-group"><span class="mig-group-name">' + escapeHtml(p.project) + '</span>';
        TOOL_KEYS.forEach(function (t) { html += destRow('project', t, p.project, src); });
        html += '</div>';
      });
      // 自选项目文件夹：不要求已被发现（可能还没有任何 skill）；
      // 目标 <文件夹>/.{tool}/skills 不存在时迁移会自动创建
      html += '<div class="mig-group"><span class="mig-group-name">自选项目文件夹</span>' +
        '<span style="font-size:11px;color:#b9bdc3;">目录缺失自动创建</span>' +
        '<div class="mig-item mig-custom-row">' +
          '<button type="button" class="layui-btn layui-btn-xs" data-mig-pick>选择文件夹</button>' +
          '<span class="mig-place" data-mig-custom-path title="' + escapeHtml(customRoot || '') + '">' +
            (customRoot ? escapeHtml(customRoot) : '未选择') + '</span>' +
        '</div>';
      TOOL_KEYS.forEach(function (t) {
        html += '<label class="mig-item' + (customRoot ? '' : ' mig-disabled') + '">' +
          '<input type="checkbox" data-mig="project|' + t + '|' + CUSTOM + '"' + (customRoot ? '' : ' disabled') + '>' +
          '<span class="target-badge target-' + t + '">' + TOOL_LABEL[t] + '</span>' +
          '<span class="mig-place" title="' + escapeHtml(customRoot || '') + '">所选文件夹</span>' +
        '</label>';
      });
      html += '</div>';
      html += '</div></div>';
      return html;
    }

    function rebuildPanel(panel, srcIdx) {
      var scroll = panel.scrollTop;
      var holder = document.createElement('div');
      holder.innerHTML = buildPanel(srcIdx);
      var newPanel = holder.firstChild;
      panel.parentNode.replaceChild(newPanel, panel);
      bindPanel(newPanel);
      newPanel.scrollTop = scroll;
      return newPanel;
    }

    function bindPanel(panel) {
      panel.addEventListener('change', function (e) {
        if (e.target.name !== 'mig-src') return;
        // 换源后整体重建面板，让禁用态与新源保持一致
        rebuildPanel(panel, Number(e.target.value));
      });
      panel.addEventListener('click', function (e) {
        if (!e.target.closest('[data-mig-pick]')) return;
        pickFolderAny().then(function (p) {
          if (!p) return;
          customRoot = p;
          rebuildPanel(panel, currentSrcIdx(panel));
        });
      });
    }

    function currentSrcIdx(panel) {
      var idx = 0;
      panel.querySelectorAll('input[name="mig-src"]').forEach(function (r, i) { if (r.checked) idx = i; });
      return idx;
    }

    layer.open({
      type: 1,
      title: '迁移「' + escapeHtml(name) + '」（复制，保留原件）',
      area: ['520px', 'auto'],
      content: buildPanel(0),
      btn: ['开始迁移', '取消'],
      success: function () {
        bindPanel(document.querySelector('.mig-panel'));
      },
      yes: function (index) {
        var srcIdx = 0;
        var radios = document.querySelectorAll('input[name="mig-src"]');
        radios.forEach(function (r, i) { if (r.checked) srcIdx = i; });
        var src = g.copies[srcIdx];
        var targets = [];
        var wantCustom = false;
        document.querySelectorAll('[data-mig]:checked').forEach(function (cb) {
          var parts = cb.getAttribute('data-mig').split('|');
          if (parts[2] === CUSTOM) { wantCustom = true; return; }
          targets.push({
            scope: parts[0], tool: parts[1],
            project: parts[2] === '' ? null : parts[2]
          });
        });
        if (wantCustom) {
          if (!customRoot) { layer.msg('请先选择项目文件夹', { icon: 2 }); return; }
          TOOL_KEYS.forEach(function (t) {
            var cb = document.querySelector('[data-mig="project|' + t + '|' + CUSTOM + '"]:checked');
            if (cb) targets.push({ scope: 'project', tool: t, project: null, project_root: customRoot });
          });
        }
        if (!targets.length) { layer.msg('请至少勾选一个迁移目标', { icon: 2 }); return; }
        layer.close(index);
        var sourceDesc = { scope: src.scope, tool: src.tool, project: src.project, name: g.name };
        // 每个目标一个任务，进度条按目标粒度推进
        var tasks = targets.map(function (t) {
          var place = t.project_root ? '自选「' + t.project_root + '」' : scopeLabel(t.scope, t.project);
          return {
            source: sourceDesc,
            targets: [t],
            label: '「' + g.name + '」→ ' + TOOL_LABEL[t.tool] + ' · ' + place
          };
        });
        runMigrateTasks(tasks, '迁移「' + g.name + '」（源端保留）', function (res) {
          migrationSummaryToast(res);
          loadSkills();
        });
      }
    });
  }

  /* ---- 项目一键适配六端：把项目内所有 skill 补齐到缺失的端 ---- */
  function adaptProject(project) {
    if (migrationBusy) return;
    var p = (skillData.projects || []).find(function (x) { return x.project === project; });
    if (!p || !p.skills.length) { layer.msg('该项目没有可迁移的 skill', { icon: 0 }); return; }
    var plan = []; // [{source, targets[], name, missing[]}]
    p.skills.forEach(function (g) {
      var missing = TOOL_KEYS.filter(function (t) { return g.ends.indexOf(t) === -1; });
      if (!missing.length || !g.copies.length) return;
      plan.push({
        source: { scope: 'project', tool: g.copies[0].tool, project: project, name: g.name },
        targets: missing.map(function (t) { return { scope: 'project', tool: t, project: project }; }),
        name: g.name,
        missing: missing
      });
    });
    if (!plan.length) { layer.msg('该项目的 skill 已在 ' + TOOL_KEYS.map(function (t) { return TOOL_LABEL[t]; }).join(' / ') + ' 六端齐全', { icon: 1 }); return; }
    var lines = plan.map(function (x) {
      return '<li>「' + escapeHtml(x.name) + '」→ ' + x.missing.map(function (t) { return TOOL_LABEL[t]; }).join('、') + '</li>';
    }).join('');
    layer.open({
      type: 1,
      title: '一键适配六端 · ' + escapeHtml(project),
      area: ['440px', 'auto'],
      content: '<div style="padding:18px 24px;"><p style="margin-bottom:10px;">将按以下计划复制补齐（源端保留）：</p><ul class="adapt-list">' + lines + '</ul></div>',
      btn: ['执行迁移', '取消'],
      yes: function (index) {
        layer.close(index);
        // 逐个 skill 分别带各自缺失的目标，串行提交避免同名冲突交叉
        var tasks = plan.map(function (x) {
          return {
            source: x.source,
            targets: x.targets,
            label: '「' + x.name + '」→ ' + x.missing.map(function (t) { return TOOL_LABEL[t]; }).join('、')
          };
        });
        runMigrateTasks(tasks, '一键适配六端 · ' + project + '（源端保留）', function (res) {
          if (!res.failed) {
            layer.msg('适配完成：已迁移 ' + res.migrated + ' 处', { icon: 1, time: 2600 });
          } else {
            migrationSummaryToast(res);
          }
          loadSkills();
        });
      }
    });
  }

  /* ---- 预览 SKILL.md ---- */
  function previewSkill(path) {
    apiGet('/skills/content?path=' + encodeURIComponent(path)).then(function (c) {
      var fm = '';
      if (c.description) fm += '<p class="skill-fm-line"><b>描述</b>：' + escapeHtml(c.description) + '</p>';
      fm += '<p class="skill-fm-line"><b>路径</b>：<code>' + escapeHtml(c.path) + '</code></p>';
      var bodyHtml = c.body ? renderMarkdown(c.body) : '<p style="color:#8a8e94;">SKILL.md 没有正文内容</p>';
      layer.open({
        type: 1,
        title: '预览 · ' + escapeHtml(c.name),
        area: ['680px', 'auto'],
        content: '<div class="skill-preview"><div class="skill-fm">' + fm + '</div>' +
          '<div class="update-notes skill-md">' + bodyHtml + '</div></div>',
        btn: ['关闭']
      });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  function openExplorer(path) {
    apiSend('/skills/open', 'POST', { path: path })
      .then(function () { layer.msg('已在资源管理器打开', { icon: 1, time: 1500 }); })
      .catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  function confirmDeleteCopy(path) {
    layer.confirm('确定删除这个 skill？<br><span style="font-size:12px;color:#8a8e94;">将移入回收站，30 天内可恢复</span>',
      { title: '删除确认' }, function (index) {
        apiSend('/skills/delete', 'POST', { path: path }).then(function (res) {
          layer.close(index);
          layer.msg(res.message || '已移入回收站', { icon: 1 });
          loadSkills();
        }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      });
  }

  /* ---- 回收站 ---- */
  function openTrash() {
    layer.open({
      type: 1,
      title: '回收站（30 天内可恢复）',
      area: ['700px', 'auto'],
      content: '<div id="trash-box" style="padding:14px 18px;max-height:480px;overflow-y:auto;">加载中…</div>',
      btn: ['刷新状态', '关闭'],
      success: function () { loadTrashBox(); },
      yes: function () { loadTrashBox(); } // GET 即执行惰性过期清理，重取即得最新状态
    });
  }

  function loadTrashBox() {
    apiGet('/skills/trash').then(function (r) {
      var box = document.getElementById('trash-box');
      if (!box) return;
      var html = '';
      if (r.purged_expired) {
        html += '<p class="trash-notice">已自动清理 ' + r.purged_expired + ' 条超过 30 天的过期条目</p>';
      }
      if (!r.items.length) {
        html += '<div class="empty-tip" style="padding:40px 0;">回收站是空的</div>';
      } else {
        html += '<table class="layui-table trash-table"><thead><tr>' +
          '<th>名称</th><th>原位置</th><th>剩余天数</th><th class="trash-col-actions">操作</th>' +
        '</tr></thead><tbody>' + r.items.map(function (it) {
          var place = (it.scope === 'global' ? '全局' : ('项目「' + escapeHtml(it.project || '') + '」'))
            + ' · ' + (TOOL_LABEL[it.tool] || it.tool || '?');
          return '<tr>' +
            '<td>' + escapeHtml(it.name) + '</td>' +
            '<td class="trash-origin" title="' + escapeHtml(it.original_path) + '">' + place + '</td>' +
            '<td>' + it.days_left + ' 天</td>' +
            '<td class="trash-col-actions"><button class="layui-btn layui-btn-xs" data-trash="restore" data-id="' + it.id + '">恢复</button>' +
            '<button class="layui-btn layui-btn-xs layui-btn-danger layui-btn-primary" data-trash="purge" data-id="' + it.id + '">彻底删除</button></td>' +
          '</tr>';
        }).join('') + '</tbody></table>';
      }
      box.innerHTML = html;
    }).catch(function (e) {
      var box = document.getElementById('trash-box');
      if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function trashAction(action, id) {
    var req;
    if (action === 'restore') {
      req = apiSend('/skills/trash/' + id + '/restore', 'POST', {});
    } else {
      req = fetch(API + '/skills/trash/' + id, { method: 'DELETE' }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || '请求失败'); });
        return r.json();
      });
    }
    req.then(function () {
      layer.msg(action === 'restore' ? '已恢复到原位置' : '已彻底删除', { icon: 1 });
      loadTrashBox();
      loadSkills();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---- 项目根目录配置 ---- */
  function openRootsDialog() {
    apiGet('/skills/project-roots').then(function (r) {
      layer.open({
        type: 1,
        title: '项目根目录（每行一个，一级子目录视为项目）',
        area: ['520px', 'auto'],
        content: '<div style="padding:16px 20px;">' +
          '<textarea id="roots-input" class="layui-textarea" rows="5" placeholder="如 C:\\ProgramMine">' +
          escapeHtml((r.roots || []).join('\n')) + '</textarea>' +
          '<p style="font-size:12px;color:#8a8e94;margin-top:8px;">只收录其中含有至少一个 skill 的项目。</p></div>',
        btn: ['保存', '取消'],
        yes: function (index) {
          var roots = document.getElementById('roots-input').value.split('\n')
            .map(function (s) { return s.trim(); }).filter(Boolean);
          apiSend('/skills/project-roots', 'PUT', { roots: roots }).then(function () {
            layer.close(index);
            layer.msg('已保存', { icon: 1 });
            loadSkills();
          }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
        }
      });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---- 迁移日志 ---- */
  function openMigrationLogs() {
    layer.open({
      type: 1,
      title: '迁移日志',
      area: ['720px', 'auto'],
      content: '<div id="miglog-box" style="padding:14px 18px;max-height:480px;overflow-y:auto;">加载中…</div>',
      btn: ['关闭'],
      success: function () {
        apiGet('/skills/migrations').then(function (list) {
          var box = document.getElementById('miglog-box');
          if (!box) return;
          if (!list.length) {
            box.innerHTML = '<div class="empty-tip" style="padding:40px 0;">暂无迁移记录</div>';
            return;
          }
          box.innerHTML = '<table class="layui-table trash-table"><thead><tr>' +
            '<th>时间</th><th>Skill</th><th>源</th><th>目标</th><th>结果</th>' +
          '</tr></thead><tbody>' + list.map(function (m) {
            return '<tr>' +
              '<td>' + escapeHtml(m.migrate_time) + '</td>' +
              '<td>' + escapeHtml(m.skill_name) + '</td>' +
              '<td>' + escapeHtml(m.source) + '</td>' +
              '<td>' + escapeHtml(m.target) + '</td>' +
              '<td class="' + (m.status === 'success' ? 'mig-ok' : 'mig-fail') + '">' +
                (m.status === 'success' ? '成功' : '失败：' + escapeHtml(m.detail)) + '</td>' +
            '</tr>';
          }).join('') + '</tbody></table>';
        }).catch(function (e) {
          var box = document.getElementById('miglog-box');
          if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
        });
      }
    });
  }

  /* ---------- MCP 管理 ---------- */
  var mcpView = 'global'; // global | project
  var mcpData = null;
  var mcpQuery = ''; // 搜索关键词，仅前端过滤已缓存的 mcpData

  function transportBadge(t) {
    var cls = t === 'stdio' ? 'target-claude' : (t === 'sse' ? 'target-codex' : 'target-dsh');
    return '<span class="target-badge ' + cls + '">' + escapeHtml(t) + '</span>';
  }

  function mcpCopyLine(c) {
    var kind = c.transport === 'stdio'
      ? escapeHtml(c.command || '')
      : escapeHtml(c.url || '');
    var envKeys = Object.keys(c.env || {});
    var envHint = envKeys.length
      ? '<span class="mcp-env-hint" title="' + escapeHtml(envKeys.join(', ')) + '">env×' + envKeys.length + '</span>'
      : '';
    return '<div class="skill-copy" data-copy>' +
      '<span class="target-badge target-' + c.tool + '">' + TOOL_LABEL[c.tool] + '</span>' +
      '<span class="skill-copy-path" title="' + escapeHtml(c.path || '') + '">' + kind + '</span>' +
      envHint +
      '<span class="skill-copy-actions">' +
        '<a href="javascript:;" data-mc="detail" data-tool="' + escapeHtml(c.tool) + '" data-scope="' + c.scope + '" data-project="' + escapeHtml(c.project || '') + '" data-name="' + escapeHtml(c.name) + '">详情</a>' +
        '<a href="javascript:;" data-mc="delcopy" data-tool="' + escapeHtml(c.tool) + '" data-scope="' + c.scope + '" data-project="' + escapeHtml(c.project || '') + '" data-name="' + escapeHtml(c.name) + '">删除</a>' +
      '</span>' +
    '</div>';
  }

  function mcpGroupCard(g, scope, project) {
    var dup = g.end_count > 1
      ? '<span class="target-badge dup-badge">' + g.end_count + ' 端存在</span>'
      : '';
    var badges = g.ends.map(endBadge).join('');
    var copies = g.copies.map(mcpCopyLine).join('');
    var desc = '<div class="card-meta skill-desc">' + transportBadge(g.transport) + '</div>';
    var srcLabel = { custom: '自定义', config: '配置自带', builtin: '自动识别', npm: 'npm 自动识别' }[g.description_source] || '';
    var intro = g.description
      ? '<div class="card-meta skill-desc mcp-desc" title="' + escapeHtml(g.description) + (srcLabel ? '（来源：' + srcLabel + '）' : '') + '">' + escapeHtml(g.description) + '</div>'
      : '<div class="card-meta skill-desc skill-desc-none">无功能介绍（可点「介绍」补充）</div>';
    return '' +
      '<div class="config-card skill-card" data-group="' + escapeHtml(g.name) + '" data-scope="' + scope + '" data-project="' + escapeHtml(project || '') + '">' +
        '<div class="skill-badges">' + badges + dup + '</div>' +
        '<div class="card-name">' + escapeHtml(g.name) + '</div>' +
        desc + intro +
        '<div class="skill-copies">' + copies + '</div>' +
        '<div class="card-actions">' +
          '<button class="layui-btn layui-btn-sm layui-btn-normal" data-mc="migrate">迁移…</button>' +
          '<button class="layui-btn layui-btn-sm" data-mc="intro" data-name="' + escapeHtml(g.name) + '" data-intro="' + escapeHtml(g.custom_description || '') + '">介绍</button>' +
          '<button class="layui-btn layui-btn-sm" data-mc="preview" data-name="' + escapeHtml(g.name) + '">预览</button>' +
        '</div>' +
      '</div>';
  }

  function renderMcps() {
    var box = document.getElementById('mcp-content');
    if (!mcpData) { box.innerHTML = '<div class="empty-tip">加载中…</div>'; return; }
    var match = makeMatcher(mcpQuery);
    if (mcpView === 'global') {
      box.classList.add('card-grid');
      var gs = mcpData.global || [];
      if (match) gs = gs.filter(match);
      if (!gs.length) {
        if (match) { box.innerHTML = noMatchTip(mcpQuery, ' MCP server'); return; }
        box.innerHTML = '<div class="empty-tip">六端还未配置任何 MCP server<br>' +
          '<span style="font-size:12px;">~/.claude.json · ~/.codex/config.toml · cordis.patch.yml · ~/.zcode/cli/config.json · ~/.pi/agent/mcp.json · ~/.qoder/mcp.json</span></div>';
        return;
      }
      box.innerHTML = gs.map(function (g) { return mcpGroupCard(g, 'global', ''); }).join('');
      return;
    }
    var projects = mcpData.projects || [];
    box.classList.remove('card-grid');
    if (match) {
      projects = projects.map(function (p) {
        return {
          project: p.project,
          root: p.root,
          servers: (p.servers || []).filter(match),
        };
      }).filter(function (p) { return p.servers.length; });
      if (!projects.length) { box.innerHTML = noMatchTip(mcpQuery, ' MCP server'); return; }
    }
    if (!projects.length) {
      box.innerHTML = '<div class="empty-tip">未发现任何含 MCP server 的项目</div>';
      return;
    }
    var html = '';
    projects.forEach(function (p) {
      html += '<div class="proj-block">' +
        '<div class="proj-head">' +
          '<span class="proj-name">' + escapeHtml(p.project) + '</span>' +
          '<span class="proj-root" title="' + escapeHtml(p.root) + '">' + escapeHtml(p.root) + '</span>' +
        '</div>';
      if (!p.servers.length) {
        html += '<div class="empty-tip proj-empty">该项目下没有 MCP server</div>';
      } else {
        html += '<div class="proj-grid">' + p.servers.map(function (g) {
          return mcpGroupCard(g, 'project', p.project);
        }).join('') + '</div>';
      }
      html += '</div>';
    });
    box.innerHTML = html;
  }

  function findMcpGroup(scope, project, name) {
    if (!mcpData) return null;
    var pool = [];
    if (scope === 'global') pool = mcpData.global || [];
    else {
      (mcpData.projects || []).forEach(function (p) {
        if (p.project === project) pool = p.servers || [];
      });
    }
    var lowered = String(name || '').toLowerCase();
    return pool.find(function (g) { return g.name.toLowerCase() === lowered; }) || null;
  }

  function loadMcps() {
    document.getElementById('mcp-content').innerHTML = '<div class="empty-tip">加载中…</div>';
    apiGet('/mcp/discover').then(function (d) {
      mcpData = d;
      renderMcps();
    }).catch(function (e) {
      document.getElementById('mcp-content').innerHTML =
        '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function setMcpView(view) {
    mcpView = view;
    document.querySelectorAll('#mcp-scope-seg .seg-btn').forEach(function (b) {
      b.classList.toggle('seg-active', b.getAttribute('data-view') === view);
    });
    if (mcpData) renderMcps();
  }

  /* ---- MCP 迁移弹窗：指定源副本与目标（传输不兼容的目标自动禁用） ---- */
  function openMcpMigrateDialog(scope, project, name) {
    if (migrationBusy) return;
    var g = findMcpGroup(scope, project, name);
    if (!g) { layer.msg('数据已过期，请刷新后重试', { icon: 2 }); return; }

    function destRow(destScope, tool, destProject, srcCopy) {
      var isSrc = !!srcCopy
        && destScope === srcCopy.scope
        && tool === srcCopy.tool
        && (destProject || '') === (srcCopy.project || '');
      var supported = (mcpData.capability || {})[tool] || [];
      var compat = supported.indexOf(srcCopy.transport) !== -1;
      var disabled = isSrc || !compat;
      var hint = !compat ? ' title="' + TOOL_LABEL[tool] + ' 端不支持 ' + srcCopy.transport + ' 传输"' : '';
      return '<label class="mig-item' + (isSrc ? ' mig-disabled' : '') + '"' + hint + '>' +
        '<input type="checkbox" data-mig="' + destScope + '|' + tool + '|' + (destProject || '') + '"' + (disabled ? ' disabled' : '') + '>' +
        '<span class="target-badge target-' + tool + '">' + TOOL_LABEL[tool] + '</span>' +
        '<span class="mig-place">' + (destScope === 'global' ? '全局' : escapeHtml(destProject)) + '</span>' +
        (!compat ? '<span class="mig-unsupported">不支持 ' + escapeHtml(srcCopy.transport) + '</span>' : '') +
      '</label>';
    }

    function buildPanel(srcIdx) {
      var src = g.copies[srcIdx] || {};
      var html = '<div class="mig-panel">' +
        '<div class="mig-section"><div class="mig-title">迁移哪一份？</div>' +
        g.copies.map(function (c, i) {
          return '<label class="mig-item">' +
            '<input type="radio" name="mig-src" value="' + i + '"' + (i === srcIdx ? ' checked' : '') + '>' +
            '<span class="target-badge target-' + c.tool + '">' + TOOL_LABEL[c.tool] + '</span>' +
            '<span class="mig-place" title="' + escapeHtml(c.path || '') + '">' + scopeLabel(c.scope, c.project) + '</span>' +
          '</label>';
        }).join('') + '</div>';
      html += '<div class="mig-section"><div class="mig-title">复制到哪些位置？（目标端同名旧版将移入回收站）</div>';
      html += '<div class="mig-group"><span class="mig-group-name">全局</span>';
      TOOL_KEYS.forEach(function (t) { html += destRow('global', t, '', src); });
      html += '</div>';
      (mcpData.projects || []).forEach(function (p) {
        html += '<div class="mig-group"><span class="mig-group-name">' + escapeHtml(p.project) + '</span>';
        MCP_PROJECT_TOOLS.forEach(function (t) { html += destRow('project', t, p.project, src); });
        html += '</div>';
      });
      html += '</div></div>';
      return html;
    }

    function bindPanel(panel) {
      panel.addEventListener('change', function (e) {
        if (e.target.name !== 'mig-src') return;
        var idx = Number(e.target.value);
        var scroll = panel.scrollTop;
        var holder = document.createElement('div');
        holder.innerHTML = buildPanel(idx);
        var newPanel = holder.firstChild;
        panel.parentNode.replaceChild(newPanel, panel);
        bindPanel(newPanel);
        newPanel.scrollTop = scroll;
      });
    }

    layer.open({
      type: 1,
      title: '迁移 MCP「' + escapeHtml(name) + '」（复制，保留原件）',
      area: ['520px', 'auto'],
      content: buildPanel(0),
      btn: ['开始迁移', '取消'],
      success: function () {
        bindPanel(document.querySelector('.mig-panel'));
      },
      yes: function (index) {
        var srcIdx = 0;
        document.querySelectorAll('input[name="mig-src"]').forEach(function (r, i) { if (r.checked) srcIdx = i; });
        var src = g.copies[srcIdx];
        var targets = [];
        document.querySelectorAll('[data-mig]:checked').forEach(function (cb) {
          var parts = cb.getAttribute('data-mig').split('|');
          targets.push({ scope: parts[0], tool: parts[1], project: parts[2] === '' ? null : parts[2] });
        });
        if (!targets.length) { layer.msg('请至少勾选一个迁移目标', { icon: 2 }); return; }
        layer.close(index);
        var sourceDesc = { scope: src.scope, tool: src.tool, project: src.project, name: g.name };
        var tasks = targets.map(function (t) {
          return {
            source: sourceDesc,
            targets: [t],
            label: '「' + g.name + '」→ ' + TOOL_LABEL[t.tool] + ' · ' + scopeLabel(t.scope, t.project)
          };
        });
        runMigrateTasks(tasks, '迁移 MCP「' + g.name + '」（源端保留）', function (res) {
          migrationSummaryToast(res);
          loadMcps();
        }, '/mcp/migrate');
      }
    });
  }

  /* ---- MCP 详情 / 预览 ---- */
  function mcpDetail(copy) {
    var q = '?tool=' + encodeURIComponent(copy.tool) +
      '&scope=' + copy.scope +
      '&name=' + encodeURIComponent(copy.name) +
      (copy.project ? '&project=' + encodeURIComponent(copy.project) : '');
    apiGet('/mcp/content' + q).then(function (c) {
      var lines = [];
      lines.push('<p class="skill-fm-line"><b>端</b>：' + escapeHtml(TOOL_LABEL[c.tool] || c.tool) + ' · ' + scopeLabel(c.scope, c.project) + '</p>');
      lines.push('<p class="skill-fm-line"><b>传输</b>：' + escapeHtml(c.transport || '') + '</p>');
      if (c.transport === 'stdio') {
        lines.push('<p class="skill-fm-line"><b>命令</b>：<code>' + escapeHtml(c.command || '') + '</code></p>');
        if (c.args && c.args.length) lines.push('<p class="skill-fm-line"><b>参数</b>：<code>' + escapeHtml(c.args.join(' ')) + '</code></p>');
      } else {
        lines.push('<p class="skill-fm-line"><b>URL</b>：<code>' + escapeHtml(c.url || '') + '</code></p>');
      }
      var envKeys = Object.keys(c.env || {});
      if (envKeys.length) {
        lines.push('<p class="skill-fm-line"><b>环境变量</b>（已脱敏）：</p>' +
          '<div class="mcp-env-list">' + envKeys.map(function (k) {
            return '<div class="mcp-env-row"><code>' + escapeHtml(k) + '</code><span class="mcp-env-val">' + escapeHtml(c.env[k]) + '</span></div>';
          }).join('') + '</div>');
      }
      lines.push('<p class="skill-fm-line"><b>配置文件</b>：<code>' + escapeHtml(c.path || '') + '</code></p>');
      layer.open({
        type: 1,
        title: 'MCP · ' + escapeHtml(c.name),
        area: ['620px', 'auto'],
        content: '<div class="skill-preview"><div class="skill-fm">' + lines.join('') + '</div></div>',
        btn: ['关闭']
      });
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  function previewMcp(name) {
    // 预览取该分组的第一个副本详情
    var box = document.querySelector('#mcp-content .config-card[data-group="' + CSS.escape(name) + '"]');
    if (!box) return;
    var copyEl = box.querySelector('[data-mc="detail"]');
    if (!copyEl) return;
    mcpDetail({
      tool: copyEl.getAttribute('data-tool'),
      scope: copyEl.getAttribute('data-scope'),
      project: copyEl.getAttribute('data-project'),
      name: copyEl.getAttribute('data-name')
    });
  }

  /* ---- MCP 功能介绍编辑 ---- */
  function openMcpIntro(name, current) {
    layer.open({
      type: 1,
      title: '功能介绍 · ' + escapeHtml(name),
      area: ['520px', 'auto'],
      content: '<div style="padding:18px 24px;">' +
        '<p style="font-size:13px;color:#8a8e94;margin-bottom:10px;">为这个 MCP server 备注功能说明，展示在卡片上（配置文件自带的 description 也会显示）。留空保存即清除自定义介绍。</p>' +
        '<textarea id="mcp-intro-input" class="layui-textarea" rows="5" placeholder="例如：GitHub 仓库与 Issue 管理；连接本地 Postgres 查询数据…">' + escapeHtml(current || '') + '</textarea>' +
      '</div>',
      btn: ['保存', '取消'],
      yes: function (index) {
        var text = document.getElementById('mcp-intro-input').value;
        apiSend('/mcp/descriptions', 'PUT', { name: name, description: text }).then(function () {
          layer.close(index);
          layer.msg('功能介绍已保存', { icon: 1 });
          loadMcps();
        }).catch(function (e) {
          layer.msg(e.message, { icon: 2 });
        });
      }
    });
  }

  /* ---- MCP 安装（向指定端新建 server） ---- */
  var MCP_TOOLS = TOOL_KEYS;  // 安装/导入面向全局配置，六端都支持全局

  function kvFromTextarea(text) {
    var out = {};
    String(text || '').split(/\r?\n/).forEach(function (line) {
      var i = line.indexOf('=');
      if (i <= 0) return;
      var k = line.slice(0, i).trim();
      if (k) out[k] = line.slice(i + 1).trim();
    });
    return out;
  }

  function openMcpInstall() {
    var cap = (mcpData && mcpData.capability) || {};
    function toolOptions(selected) {
      return MCP_TOOLS.map(function (t) {
        var sel = t === selected ? ' selected' : '';
        return '<option value="' + t + '"' + sel + '>' + TOOL_LABEL[t] + '</option>';
      }).join('');
    }
    var html =
      '<form class="layui-form" lay-filter="mcp-install-form" style="padding:16px 24px 4px;">' +
        '<div class="layui-form-item" style="margin-bottom:8px;">' +
          '<div class="layui-input-block" style="margin-left:0;">' +
            '<div class="seg-control" id="mi-mode-seg">' +
              '<button type="button" class="seg-btn seg-active" data-mode="json">粘贴 JSON</button>' +
              '<button type="button" class="seg-btn" data-mode="form">表单填写</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">安装到</label>' +
          '<div class="layui-input-inline" style="width:140px;">' +
            '<select name="mi_tool" id="mi-tool">' + toolOptions('claude') + '</select>' +
          '</div>' +
          '<div class="layui-form-mid layui-word-aux" style="margin-left:0;">全局配置</div>' +
        '</div>' +
        '<div id="mi-form-fields">' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">名称</label>' +
          '<div class="layui-input-block"><input type="text" name="mi_name" class="layui-input" placeholder="如：postgres / github"></div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">传输</label>' +
          '<div class="layui-input-block"><select name="mi_transport" id="mi-transport"></select></div>' +
        '</div>' +
        '<div id="mi-stdio-fields">' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">启动命令</label>' +
            '<div class="layui-input-block"><input type="text" name="mi_command" class="layui-input" placeholder="如：npx / python / 程序路径"></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">参数</label>' +
            '<div class="layui-input-block"><input type="text" name="mi_args" class="layui-input" placeholder="空格分隔，如：-y @modelcontextprotocol/server-github"></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">环境变量</label>' +
            '<div class="layui-input-block"><textarea name="mi_env" class="layui-textarea" rows="3" placeholder="每行 KEY=VALUE"></textarea></div>' +
          '</div>' +
        '</div>' +
        '<div id="mi-url-fields" style="display:none;">' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">服务地址</label>' +
            '<div class="layui-input-block"><input type="text" name="mi_url" class="layui-input" placeholder="http(s)://host:port/mcp 或 …/sse"></div>' +
          '</div>' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">请求头</label>' +
            '<div class="layui-input-block"><textarea name="mi_headers" class="layui-textarea" rows="3" placeholder="每行 KEY=VALUE，如 Authorization=Bearer xxx"></textarea></div>' +
          '</div>' +
        '</div>' +
        '<div class="layui-form-item">' +
          '<label class="layui-form-label">介绍</label>' +
          '<div class="layui-input-block"><input type="text" name="mi_description" class="layui-input" placeholder="可选：这个 MCP 是干什么的"></div>' +
        '</div>' +
        '</div>' +
        '<div id="mi-json-fields" style="display:none;">' +
          '<div class="layui-form-item">' +
            '<label class="layui-form-label">配置片段</label>' +
            '<div class="layui-input-block"><textarea id="mi-json-input" class="layui-textarea" rows="10" placeholder="{\n  &quot;amap-maps&quot;: {\n    &quot;command&quot;: &quot;npx&quot;,\n    &quot;args&quot;: [&quot;-y&quot;, &quot;@amap/amap-maps-mcp-server&quot;],\n    &quot;env&quot;: { &quot;AMAP_MAPS_API_KEY&quot;: &quot;&quot; }\n  }\n}"></textarea>' +
          '</div>' +
          '<div class="layui-form-mid layui-word-aux" style="margin-left:110px;padding-top:4px;">兼容「mcpServers」顶层 / 名称作键的 server 字典 / 单对象，多条可一次批量导入</div>' +
        '</div>' +
      '</form>';

    layer.open({
      type: 1,
      title: '安装 MCP 服务',
      area: ['560px', 'auto'],
      content: html,
      btn: ['安装', '取消'],
      success: function (layero) {
        // 归位关闭按钮：layui 在 type:1 内联内容时把 setwin 渲染进 content，
        // 移回层根标题栏（插入 content 之前 = title 之后），X 与标题同行。
        var root = layero[0] || layero;
        if (root && root.querySelector) {
          var setwin = root.querySelector('.layui-layer-setwin');
          var content = root.querySelector('.layui-layer-content');
          if (setwin && content && content.contains(setwin)) {
            root.insertBefore(setwin, content);
          }
        }
        function syncCapability() {
          var tool = document.getElementById('mi-tool').value;
          var transports = (cap[tool] || ['stdio']);
          document.getElementById('mi-transport').innerHTML = transports.map(function (t) {
            var label = t === 'http' ? 'HTTP（streamable）' : (t === 'sse' ? 'SSE' : 'stdio');
            return '<option value="' + t + '">' + label + '</option>';
          }).join('');
          syncTransport();
          form.render(null, 'mcp-install-form');
        }
        function syncTransport() {
          var t = document.getElementById('mi-transport').value;
          document.getElementById('mi-stdio-fields').style.display = (t === 'stdio') ? '' : 'none';
          document.getElementById('mi-url-fields').style.display = (t === 'stdio') ? 'none' : '';
        }
        document.getElementById('mi-tool').addEventListener('change', syncCapability);
        document.getElementById('mi-transport').addEventListener('change', syncTransport);
        var modeSeg = document.getElementById('mi-mode-seg');
        if (modeSeg) {
          modeSeg.addEventListener('click', function (e) {
            var btn = e.target.closest('.seg-btn');
            if (!btn) return;
            this.querySelectorAll('.seg-btn').forEach(function (b) {
              b.classList.toggle('seg-active', b === btn);
            });
            var json = btn.getAttribute('data-mode') === 'json';
            document.getElementById('mi-form-fields').style.display = json ? 'none' : '';
            document.getElementById('mi-json-fields').style.display = json ? '' : 'none';
          });
          // 按默认选中项（粘贴 JSON）初始化显隐
          var activeBtn = modeSeg.querySelector('.seg-btn.seg-active');
          var initJson = activeBtn && activeBtn.getAttribute('data-mode') === 'json';
          document.getElementById('mi-form-fields').style.display = initJson ? 'none' : '';
          document.getElementById('mi-json-fields').style.display = initJson ? '' : 'none';
        }
        syncCapability();
        form.render(null, 'mcp-install-form');
      },
      yes: function (index) {
        var tool = document.getElementById('mi-tool').value;
        var modeBtn = document.querySelector('#mi-mode-seg .seg-btn.seg-active');
        if (modeBtn && modeBtn.getAttribute('data-mode') === 'json') {
          var jsonText = (document.getElementById('mi-json-input') || {}).value || '';
          if (!jsonText.trim()) { layer.msg('请粘贴 MCP 配置 JSON', { icon: 2 }); return; }
          apiSend('/mcp/servers/import', 'POST', { scope: 'global', tool: tool, project: null, config_json: jsonText })
            .then(function (res) {
              layer.close(index);
              var parts = ['成功安装 ' + res.installed.length + ' 个'];
              if (res.failed && res.failed.length) {
                parts.push('失败 ' + res.failed.length + ' 个：' + res.failed.map(function (f) {
                  return f.name + '（' + f.error + '）';
                }).join('；'));
              }
              layer.msg(parts.join('，'), { icon: (res.failed && res.failed.length) ? 2 : 1, time: 4000 });
              loadMcps();
            }).catch(function (e) {
              layer.msg(e.message, { icon: 2 });
            });
          return;
        }
        var transport = document.getElementById('mi-transport').value;
        var name = (document.getElementById('mi-name') ? document.getElementById('mi-name').value : '') || document.querySelector('input[name="mi_name"]').value;
        var body = {
          scope: 'global',
          tool: tool,
          project: null,
          name: (name || '').trim(),
          transport: transport,
          description: (document.querySelector('input[name="mi_description"]').value || '').trim()
        };
        if (transport === 'stdio') {
          body.command = document.querySelector('input[name="mi_command"]').value || '';
          body.args = document.querySelector('input[name="mi_args"]').value.split(/\s+/).filter(Boolean);
          body.env = kvFromTextarea(document.querySelector('textarea[name="mi_env"]').value);
        } else {
          body.url = document.querySelector('input[name="mi_url"]').value || '';
          body.headers = kvFromTextarea(document.querySelector('textarea[name="mi_headers"]').value);
        }
        if (!body.name) { layer.msg('请填写 MCP server 名称', { icon: 2 }); return; }
        apiSend('/mcp/servers', 'POST', body).then(function (res) {
          layer.close(index);
          layer.msg('已安装到 ' + TOOL_LABEL[tool] + ' 全局配置', { icon: 1 });
          loadMcps();
        }).catch(function (e) {
          layer.msg(e.message, { icon: 2 });
        });
      }
    });
  }

  function confirmDeleteMcpCopy(copy) {
    layer.confirm('确定删除这个 MCP server？<br><span style="font-size:12px;color:#8a8e94;">将从配置中移除并移入回收站，30 天内可恢复</span>',
      { title: '删除确认' }, function (index) {
        apiSend('/mcp/delete', 'POST', copy).then(function (res) {
          layer.close(index);
          layer.msg(res.message || '已移入回收站', { icon: 1 });
          loadMcps();
        }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
      });
  }

  /* ---- MCP 回收站 ---- */
  function openMcpTrash() {
    layer.open({
      type: 1,
      title: 'MCP 回收站（30 天内可恢复）',
      area: ['700px', 'auto'],
      content: '<div id="mcp-trash-box" style="padding:14px 18px;max-height:480px;overflow-y:auto;">加载中…</div>',
      btn: ['刷新状态', '关闭'],
      success: function () { loadMcpTrashBox(); },
      yes: function () { loadMcpTrashBox(); }
    });
  }

  function loadMcpTrashBox() {
    apiGet('/mcp/trash').then(function (r) {
      var box = document.getElementById('mcp-trash-box');
      if (!box) return;
      var html = '';
      if (r.purged_expired) {
        html += '<p class="trash-notice">已自动清理 ' + r.purged_expired + ' 条超过 30 天的过期条目</p>';
      }
      if (!r.items.length) {
        html += '<div class="empty-tip" style="padding:40px 0;">回收站是空的</div>';
      } else {
        html += '<table class="layui-table trash-table"><thead><tr>' +
          '<th>名称</th><th>原位置</th><th>剩余天数</th><th class="trash-col-actions">操作</th>' +
        '</tr></thead><tbody>' + r.items.map(function (it) {
          var place = (it.scope === 'global' ? '全局' : ('项目「' + escapeHtml(it.project || '') + '」'))
            + ' · ' + (TOOL_LABEL[it.tool] || it.tool || '?');
          return '<tr>' +
            '<td>' + escapeHtml(it.name) + '</td>' +
            '<td class="trash-origin" title="' + escapeHtml(it.original_path) + '">' + place + '</td>' +
            '<td>' + it.days_left + ' 天</td>' +
            '<td class="trash-col-actions"><button class="layui-btn layui-btn-xs" data-mcp-trash="restore" data-id="' + it.id + '">恢复</button>' +
            '<button class="layui-btn layui-btn-xs layui-btn-danger layui-btn-primary" data-mcp-trash="purge" data-id="' + it.id + '">彻底删除</button></td>' +
          '</tr>';
        }).join('') + '</tbody></table>';
      }
      box.innerHTML = html;
    }).catch(function (e) {
      var box = document.getElementById('mcp-trash-box');
      if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  function mcpTrashAction(action, id) {
    var req;
    if (action === 'restore') {
      req = apiSend('/mcp/trash/' + id + '/restore', 'POST', {});
    } else {
      req = fetch(API + '/mcp/trash/' + id, { method: 'DELETE' }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || '请求失败'); });
        return r.json();
      });
    }
    req.then(function () {
      layer.msg(action === 'restore' ? '已恢复到原位置' : '已彻底删除', { icon: 1 });
      loadMcpTrashBox();
      loadMcps();
    }).catch(function (e) { layer.msg(e.message, { icon: 2 }); });
  }

  /* ---- MCP 迁移日志 ---- */
  function openMcpMigrationLogs() {
    layer.open({
      type: 1,
      title: 'MCP 迁移日志',
      area: ['760px', 'auto'],
      content: '<div id="mcpmiglog-box" style="padding:14px 18px;max-height:480px;overflow-y:auto;">加载中…</div>',
      btn: ['关闭'],
      success: function () {
        apiGet('/mcp/migrations').then(function (list) {
          var box = document.getElementById('mcpmiglog-box');
          if (!box) return;
          if (!list.length) {
            box.innerHTML = '<div class="empty-tip" style="padding:40px 0;">暂无迁移记录</div>';
            return;
          }
          box.innerHTML = '<table class="layui-table trash-table"><thead><tr>' +
            '<th>时间</th><th>Server</th><th>传输</th><th>源</th><th>目标</th><th>结果</th>' +
          '</tr></thead><tbody>' + list.map(function (m) {
            return '<tr>' +
              '<td>' + escapeHtml(m.migrate_time) + '</td>' +
              '<td>' + escapeHtml(m.server_name) + '</td>' +
              '<td>' + escapeHtml(m.transport || '') + '</td>' +
              '<td>' + escapeHtml(m.source) + '</td>' +
              '<td>' + escapeHtml(m.target) + '</td>' +
              '<td class="' + (m.status === 'success' ? 'mig-ok' : 'mig-fail') + '">' +
                (m.status === 'success' ? '成功' : '失败：' + escapeHtml(m.detail)) + '</td>' +
            '</tr>';
          }).join('') + '</tbody></table>';
        }).catch(function (e) {
          var box = document.getElementById('mcpmiglog-box');
          if (box) box.innerHTML = '<div class="empty-tip">加载失败：' + escapeHtml(e.message) + '</div>';
        });
      }
    });
  }

  /* ---------- 事件绑定 ---------- */
  document.getElementById('btn-add').addEventListener('click', function () {
    openForm(null);
  });

  document.getElementById('btn-add-provider').addEventListener('click', function () {
    providerReturnToConfig = false;
    openProviderForm(null);
  });

  document.getElementById('btn-check-update').addEventListener('click', function () {
    checkUpdate(false);
  });

  /* 关窗只是转入后台继续跑任务，真正的退出入口在这里（会先问清有没有任务在跑） */
  var quitBtn = document.getElementById('btn-quit');
  if (quitBtn) {
    quitBtn.addEventListener('click', function () {
      var api = window.pywebview && window.pywebview.api;
      if (!api || !api.quit_app) {
        layer.msg('浏览器形态请直接关闭页面', { icon: 0 });
        return;
      }
      function ask(running) {
        layer.confirm(
          running ? '仍有 ' + running + ' 个任务在跑，退出会终止它们。确定退出 a4agent？'
                  : '确定退出 a4agent？',
          { title: '退出', btn: ['退出', '取消'] },
          function (idx) { layer.close(idx); api.quit_app(); }
        );
      }
      if (api.running_tasks) {
        Promise.resolve(api.running_tasks())
          .then(function (r) { ask((r && r.running) || 0); })
          .catch(function () { ask(0); });
      } else {
        ask(0);
      }
    });
  }

  document.getElementById('card-grid').addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action]');
    if (!btn) return;
    var card = btn.closest('.config-card');
    var id = Number(card.getAttribute('data-id'));
    var action = btn.getAttribute('data-action');
    if (action === 'switch') confirmSwitch(id, btn.getAttribute('data-name'), card.getAttribute('data-targets'));
    else if (action === 'edit') openForm(id);
    else if (action === 'del') confirmDelete(id, btn.getAttribute('data-name'));
  });

  document.getElementById('provider-list').addEventListener('click', function (e) {
    var btn = e.target.closest('[data-provider-action]');
    if (!btn) return;
    var id = Number(btn.getAttribute('data-id'));
    var action = btn.getAttribute('data-provider-action');
    if (action === 'edit') openProviderForm(id);
    else if (action === 'del') confirmDeleteProvider(id, btn.getAttribute('data-name'));
  });

  /* ---------- 技能管理事件 ---------- */
  document.querySelectorAll('#skills-scope-seg .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { setSkillView(btn.getAttribute('data-view')); });
  });

  bindListSearch('skills-search', 'skills-search-clear', function (q) {
    skillQuery = q;
    renderSkills();
  });

  document.getElementById('btn-skills-refresh').addEventListener('click', function () { loadSkills(); });
  document.getElementById('btn-skills-trash').addEventListener('click', openTrash);
  document.getElementById('btn-skills-roots').addEventListener('click', openRootsDialog);
  document.getElementById('btn-skills-migrations').addEventListener('click', openMigrationLogs);

  document.getElementById('skills-content').addEventListener('click', function (e) {
    var btn = e.target.closest('[data-sk]');
    if (!btn) return;
    var sk = btn.getAttribute('data-sk');
    if (sk === 'open') openExplorer(btn.getAttribute('data-path'));
    else if (sk === 'delcopy') confirmDeleteCopy(btn.getAttribute('data-path'));
    else if (sk === 'preview') previewSkill(btn.getAttribute('data-path'));
    else if (sk === 'adapt') adaptProject(btn.getAttribute('data-project'));
    else if (sk === 'migrate') {
      var card = btn.closest('.skill-card');
      openMigrateDialog(
        card.getAttribute('data-scope'),
        card.getAttribute('data-project') || '',
        card.getAttribute('data-group')
      );
    }
  });

  // 回收站 / 迁移日志等弹窗内的动态按钮：document 级委托
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-trash]');
    if (!btn) return;
    trashAction(btn.getAttribute('data-trash'), Number(btn.getAttribute('data-id')));
  });

  /* ---------- MCP 管理事件 ---------- */
  document.querySelectorAll('#mcp-scope-seg .seg-btn').forEach(function (btn) {
    btn.addEventListener('click', function () { setMcpView(btn.getAttribute('data-view')); });
  });

  bindListSearch('mcp-search', 'mcp-search-clear', function (q) {
    mcpQuery = q;
    renderMcps();
  });

  document.getElementById('btn-mcp-refresh').addEventListener('click', function () { loadMcps(); });
  document.getElementById('btn-mcp-install').addEventListener('click', openMcpInstall);
  document.getElementById('btn-mcp-trash').addEventListener('click', openMcpTrash);
  document.getElementById('btn-mcp-migrations').addEventListener('click', openMcpMigrationLogs);

  document.getElementById('mcp-content').addEventListener('click', function (e) {
    var btn = e.target.closest('[data-mc]');
    if (!btn) return;
    var mc = btn.getAttribute('data-mc');
    var copy = {
      tool: btn.getAttribute('data-tool'),
      scope: btn.getAttribute('data-scope'),
      project: btn.getAttribute('data-project'),
      name: btn.getAttribute('data-name')
    };
    if (mc === 'detail') mcpDetail(copy);
    else if (mc === 'delcopy') confirmDeleteMcpCopy(copy);
    else if (mc === 'preview') previewMcp(btn.getAttribute('data-name'));
    else if (mc === 'intro') openMcpIntro(btn.getAttribute('data-name'), btn.getAttribute('data-intro'));
    else if (mc === 'migrate') {
      var card = btn.closest('.skill-card');
      openMcpMigrateDialog(
        card.getAttribute('data-scope'),
        card.getAttribute('data-project') || '',
        card.getAttribute('data-group')
      );
    }
  });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-mcp-trash]');
    if (!btn) return;
    mcpTrashAction(btn.getAttribute('data-mcp-trash'), Number(btn.getAttribute('data-id')));
  });

  /* ---------- 需求与反馈 ----------
     参考项目内 eolisten 的反馈机制（结构化内容 + 留档），适配本地应用：
     提交时组装成 Issue 文本（自动带环境信息 / 可选日志）→ 本地留档 →
     复制到剪贴板 → 用户点击 GitHub / Gitee 链接粘贴提交。 */
  function copyText(text) {
    function legacyCopy() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) { /* 剪贴板不可用时用户可手动复制 */ }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).catch(legacyCopy);
    }
    legacyCopy();
    return Promise.resolve();
  }

  /* 截图状态：随弹窗打开重置，关闭时回收预览 URL */
  var fbFiles = [];
  var FB_MAX_IMAGES = 10;
  var FB_IMAGE_MAX_BYTES = 1 * 1024 * 1024;

  function fbResetFiles() {
    fbFiles.forEach(function (f) { URL.revokeObjectURL(f._url); });
    fbFiles = [];
  }

  function fbAddFiles(list) {
    var rejected = [];
    Array.prototype.forEach.call(list, function (f) {
      if (fbFiles.length >= FB_MAX_IMAGES) { rejected.push(f.name + '：最多 ' + FB_MAX_IMAGES + ' 张'); return; }
      if (!f.type || f.type.indexOf('image/') !== 0) { rejected.push(f.name + '：不是图片'); return; }
      if (f.size > FB_IMAGE_MAX_BYTES) { rejected.push(f.name + '：超过 1MB'); return; }
      f._url = URL.createObjectURL(f);
      fbFiles.push(f);
    });
    fbRenderThumbs();
    if (rejected.length) layer.msg(rejected.join('；'), { icon: 0 });
  }

  function fbRenderThumbs() {
    var wrap = document.getElementById('fb-thumbs');
    if (!wrap) return;
    wrap.innerHTML = '';
    fbFiles.forEach(function (f, i) {
      var cell = document.createElement('div');
      cell.className = 'fb-thumb';
      var img = document.createElement('img');
      img.src = f._url;
      img.alt = f.name;
      var del = document.createElement('button');
      del.className = 'fb-thumb-del';
      del.type = 'button';
      del.textContent = '×';
      del.title = '移除 ' + f.name;
      del.addEventListener('click', function () {
        URL.revokeObjectURL(f._url);
        fbFiles.splice(i, 1);
        fbRenderThumbs();
      });
      cell.appendChild(img);
      cell.appendChild(del);
      wrap.appendChild(cell);
    });
  }

  function openFeedback() {
    apiGet('/feedback/context').then(function (ctx) {
      var kind = 'bug';
      fbResetFiles();
      layer.open({
        type: 1,
        title: '需求与反馈',
        area: ['520px', 'auto'],
        end: function () { fbResetFiles(); },
        content: '<div style="padding:16px 20px;">' +
          '<div class="seg-control" id="fb-kind-seg">' +
            '<button type="button" class="seg-btn seg-active" data-kind="bug">Bug 报告</button>' +
            '<button type="button" class="seg-btn" data-kind="feature">功能需求</button>' +
          '</div>' +
          '<textarea id="fb-content" class="layui-textarea" rows="5" maxlength="2000" style="margin-top:12px;" ' +
            'placeholder="Bug 请写清「做了什么 → 实际结果 → 预期结果」；需求请写清使用场景与期望方案"></textarea>' +
          '<div class="fb-dropzone dropzone" id="fb-dz">' +
            '<span>点击选择或拖入问题截图</span>' +
            '<span class="dz-file">选填 · 最多 10 张 · 单张不超过 1MB</span>' +
          '</div>' +
          '<input type="file" id="fb-files" accept="image/*" multiple style="display:none">' +
          '<div id="fb-thumbs" class="fb-thumbs"></div>' +
          '<input id="fb-contact" class="layui-input" style="margin-top:8px" maxlength="200" ' +
            'placeholder="联系方式：邮箱 / QQ / 微信（选填，方便回访）" autocomplete="off">' +
          '<label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#8a8e94;margin-top:10px;user-select:none;">' +
            '<input type="checkbox" id="fb-include-log"' + (ctx.log_exists ? '' : ' disabled') + '>' +
            '<span>附带最近 120 行日志（' + escapeHtml(ctx.log_path) + '）</span>' +
          '</label>' +
          '<p style="font-size:12px;color:#8a8e94;margin-top:8px;line-height:1.7;">' +
            '提交后反馈与截图将直接发送给开发者；自动附带系统环境信息。' +
            '<b>请勿包含 API Key 等敏感信息</b>，日志建议先自查。' +
            '也可直接联系：QQ 3886370035 ｜ 微信 eogee2022。</p>' +
        '</div>',
        btn: ['提交反馈', '取消'],
        yes: function (index) {
          var content = document.getElementById('fb-content').value.trim();
          if (content.length < 5) {
            layer.msg('请先填写问题描述（至少 5 个字）', { icon: 0 });
            return;
          }
          var btn = document.querySelector('.layui-layer-btn .layui-layer-btn0');
          btn.disabled = true;
          btn.textContent = '正在提交…';
          var fd = new FormData();
          fd.append('kind', kind);
          fd.append('content', content);
          fd.append('contact', document.getElementById('fb-contact').value.trim());
          fd.append('include_log', document.getElementById('fb-include-log').checked ? '1' : '0');
          fbFiles.forEach(function (f) { fd.append('images', f, f.name); });
          fetch(API + '/feedback', { method: 'POST', body: fd }).then(function (r) {
            return r.json().then(function (j) {
              btn.disabled = false;
              btn.textContent = '提交反馈';
              if (!r.ok) { layer.msg(j.detail || '提交失败，请稍后重试', { icon: 2 }); return; }
              layer.close(index);
              layer.msg(j.emailed ? '反馈已提交，感谢您的支持！' : '反馈已保存，但邮件发送失败（网络问题不影响留档）',
                { icon: j.emailed ? 1 : 0 });
            });
          }).catch(function () {
            btn.disabled = false;
            btn.textContent = '提交反馈';
            layer.msg('网络异常，提交失败', { icon: 2 });
          });
        },
        success: function () {
          document.querySelectorAll('#fb-kind-seg .seg-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
              document.querySelectorAll('#fb-kind-seg .seg-btn').forEach(function (b) { b.classList.remove('seg-active'); });
              btn.classList.add('seg-active');
              kind = btn.getAttribute('data-kind');
            });
          });
          var dz = document.getElementById('fb-dz');
          var input = document.getElementById('fb-files');
          dz.addEventListener('click', function () { input.click(); });
          dz.addEventListener('dragover', function (e) { e.preventDefault(); dz.classList.add('dz-over'); });
          dz.addEventListener('dragleave', function () { dz.classList.remove('dz-over'); });
          dz.addEventListener('drop', function (e) {
            e.preventDefault();
            dz.classList.remove('dz-over');
            if (e.dataTransfer.files.length) fbAddFiles(e.dataTransfer.files);
          });
          input.addEventListener('change', function () {
            if (input.files.length) fbAddFiles(input.files);
            input.value = '';
          });
        }
      });
    }).catch(function (e) {
      layer.msg(e.message || '加载失败', { icon: 2 });
    });
  }

  document.getElementById('btn-feedback').addEventListener('click', openFeedback);

  /* ---------- 版本与更新：点击页脚版本号打开 ---------- */
  function openVersionDialog() {
    layer.open({
      type: 1,
      title: '版本与更新',
      // 固定高度：area 用 auto 时 layer 按初始「加载中」高度定位，changelog
      // 异步撑高后不再重算，弹窗会偏离垂直居中
      area: ['700px', '560px'],
      content: '<div style="padding:16px 20px;height:100%;box-sizing:border-box;display:flex;flex-direction:column;">' +
        '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;flex:none;">' +
          '<span id="ver-current" style="font-size:14px;font-weight:600;color:#3d4044;">当前版本 ' +
            escapeHtml(document.getElementById('footer-version').textContent || '') + '</span>' +
          '<button class="layui-btn layui-btn-normal layui-btn-sm" id="btn-version-check">检查更新</button>' +
          '<a class="update-link" style="font-size:12px;" href="https://github.com/eogee/a4agent/releases" target="_blank" rel="noopener">完整发布说明（GitHub Releases）</a>' +
        '</div>' +
        '<div id="ver-notes" class="update-notes" style="margin-top:14px;flex:1 1 auto;min-height:0;max-height:none;overflow-y:auto;">加载中…</div>' +
      '</div>',
      success: function () {
        document.getElementById('btn-version-check').addEventListener('click', function () {
          checkUpdate(false);
        });
        fetch('changelog.md').then(function (r) {
          if (!r.ok) throw new Error('changelog.md ' + r.status);
          return r.text();
        }).then(function (md) {
          var box = document.getElementById('ver-notes');
          if (box) box.innerHTML = renderMarkdown(md);
        }).catch(function () {
          var box = document.getElementById('ver-notes');
          if (box) box.innerHTML = '<p style="color:#8a8e94;font-size:13px;">暂无更新说明，可点击上方链接查看 GitHub Releases。</p>';
        });
      }
    });
  }

  document.getElementById('footer-version').addEventListener('click', openVersionDialog);

  /* 页脚联系方式：点击复制（QQ 号 / 微信号） */
  document.querySelectorAll('.footer-copy').forEach(function (el) {
    el.addEventListener('click', function () {
      var label = el.getAttribute('data-copy');
      copyText(label).then(function () {
        layer.msg('已复制：' + label, { icon: 1 });
      });
    });
  });

  /* ---------- 页面级悬浮滚动条 ---------- */
  /* 原生根滚动条已通过 CSS 隐藏（不再向左挤占页面宽度），这里自绘一条
   * 悬浮在内容上方的细滑块：滚动页面或鼠标靠近右缘时浮现，停止操作约
   * 1 秒后淡出，支持按住拖动。只处理页面级滚动，卡片内部滚动条不受影响。 */
  (function () {
    var bar = document.createElement('div');
    bar.className = 'page-scrollbar';
    var thumb = document.createElement('div');
    thumb.className = 'page-scrollbar-thumb';
    bar.appendChild(thumb);
    document.body.appendChild(bar);

    var hideTimer = null;
    var dragging = false;

    function metrics() {
      var doc = document.documentElement;
      return {
        scrollH: doc.scrollHeight,
        viewH: window.innerHeight,
        top: window.scrollY || doc.scrollTop || 0
      };
    }

    function show() {
      if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
      bar.classList.add('visible');
    }

    function scheduleHide() {
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(function () {
        hideTimer = null;
        if (!dragging) bar.classList.remove('visible');
      }, 1000);
    }

    /* 仅重新计算滑块位置与尺寸；appear=false 时不主动浮现（如内容高度变化） */
    function position(appear) {
      var m = metrics();
      if (m.scrollH <= m.viewH + 1) {
        bar.classList.remove('visible');
        thumb.style.height = '0px';
        return;
      }
      var trackH = m.viewH - 8; // 上下各留 4px
      var h = Math.max(36, Math.round(trackH * m.viewH / m.scrollH));
      var maxTop = trackH - h;
      var y = Math.round(maxTop * m.top / (m.scrollH - m.viewH));
      thumb.style.height = h + 'px';
      thumb.style.top = (4 + y) + 'px';
      if (appear) {
        show();
        scheduleHide();
      }
    }

    window.addEventListener('scroll', function () { position(true); }, { passive: true });
    window.addEventListener('resize', function () { position(false); });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { position(false); }).observe(document.documentElement);
    }

    /* 鼠标靠近右缘时浮现 */
    document.addEventListener('mousemove', function (e) {
      if (e.clientX > window.innerWidth - 28) {
        show();
        scheduleHide();
      }
    });

    /* 按住滑块拖动 */
    thumb.addEventListener('pointerdown', function (e) {
      var m = metrics();
      var trackH = m.viewH - 8;
      var h = thumb.getBoundingClientRect().height;
      var range = m.scrollH - m.viewH;
      var trackRange = trackH - h;
      if (range <= 0 || trackRange <= 0) return;
      dragging = true;
      bar.classList.add('dragging');
      thumb.setPointerCapture(e.pointerId);
      var startY = e.clientY;
      var startTop = m.top;
      function onMove(ev) {
        var dy = ev.clientY - startY;
        window.scrollTo(0, startTop + dy * range / trackRange);
      }
      function onUp() {
        dragging = false;
        bar.classList.remove('dragging');
        thumb.removeEventListener('pointermove', onMove);
        thumb.removeEventListener('pointerup', onUp);
        thumb.removeEventListener('pointercancel', onUp);
        scheduleHide();
      }
      thumb.addEventListener('pointermove', onMove);
      thumb.addEventListener('pointerup', onUp);
      thumb.addEventListener('pointercancel', onUp);
      e.preventDefault();
    });

    position(false);
  })();

  /* ---------- 初始化 ---------- */
  loadConfigs();
  loadStatus();
  loadProxyStatus();
  loadProviders();
  // 启动时静默检查一次更新；失败不打扰（silent=true 只弹更新，不弹错误）
  checkUpdate(true);
});
