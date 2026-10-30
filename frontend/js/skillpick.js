/* 技能选择公共弹层（window.A4SkillPicker）。
 *
 * 一处实现，任务下发（tasks.js）与循环任务（loops.js，批 4 接入）共用：
 * - 数据源 GET /api/v1/skills/discover（后端聚合全局与各项目技能）；
 * - 工作目录已选 → 匹配 projects[].root（与工作目录相等，或工作目录位于 root 之下）
 *   的项目技能 + 全局技能；未选 → 仅全局技能，顶部提示「不注入、仅作提示词建议」；
 * - 按端兼容性标注（ends 含当前引擎，或 opencode_visible）：不兼容项标黄但不隐藏、
 *   不自动删——用户可能就是想跨端选；
 * - 搜索按名称/描述过滤；上限 20（与 schemas.TaskCreate.skills 同值）；
 * - 确认后回传 SkillRef 数组：name / source_tool / source_scope / source_project /
 *   source_path，source_* 取自 copies 里与当前引擎匹配的那一份（无匹配副本时回退
 *   第一份，注入层会按名字跨端兜底）。
 *
 * pick(opts) -> Promise<Array<SkillRef>|null>：
 *   opts = { tool: 引擎 tool, workdir: 工作目录绝对路径或 '', selected: 已选 SkillRef 数组 }
 *   点「确定」resolve SkillRef 数组（可为空数组），取消 resolve null。
 */
layui.use(['layer'], function () {
  var layer = layui.layer;
  var API = '/api/v1/skills/discover';
  var MAX_SKILLS = 20;  // 与 schemas.TaskCreate.skills.max_length 同值（方案 §4.2）

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fetchDiscover(workdir) {
    var url = API + (workdir ? '?workdir=' + encodeURIComponent(workdir) : '');
    return fetch(url).then(function (r) {
      if (!r.ok) {
        return r.json().then(
          function (j) { throw new Error(j.detail || ('HTTP ' + r.status)); },
          function () { throw new Error('请求失败（HTTP ' + r.status + '）'); }
        );
      }
      return r.json();
    });
  }

  /* Windows 路径归一化：斜杠统一、去尾分隔符、小写比较 */
  function normPath(p) {
    return String(p || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  }

  /* 工作目录与项目根匹配：相等，或工作目录位于项目根之下（子目录沿用该项目的技能） */
  function underRoot(root, dir) {
    var r = normPath(root), d = normPath(dir);
    return !!r && !!d && (d === r || d.indexOf(r + '\\') === 0);
  }

  function pick(opts) {
    opts = opts || {};
    var tool = opts.tool || '';
    var workdir = opts.workdir || '';
    var selected = {};   // key -> SkillRef（含 _ends/_visible 渲染元数据，提交前由调用方剥掉）
    function keyOf(name, scope) { return name + '|' + (scope || 'global'); }
    ((opts.selected) || []).forEach(function (ref) {
      if (ref && ref.name) selected[keyOf(ref.name, ref.source_scope)] = ref;
    });

    return fetchDiscover(workdir).then(function (data) { return open(data); });

    function open(data) {
      return new Promise(function (resolve) {
        var settled = false;
        function finish(value) {
          if (settled) return;
          settled = true;
          resolve(value);
        }

        /* 候选分组：工作目录命中的项目在前，全局在后。
           source === 'workdir' 是 discover(workdir) 合成的「任务工作目录」条目
           （未注册目录也能看到自己的技能），分组标签与角标单独标出 */
        var groups = [];
        if (workdir) {
          (data.projects || []).forEach(function (p) {
            if (underRoot(p.root, workdir)) {
              groups.push({
                scope: 'project', project: p.project || '', root: p.root || '',
                skills: p.skills || [], isWorkdir: p.source === 'workdir'
              });
            }
          });
        }
        groups.push({ scope: 'global', project: '', root: '', skills: data.global || [] });

        var groupItems = groups.map(function (g) {
          return (g.skills || []).map(function (e) {
            return { entry: e, scope: g.scope, project: g.project, root: g.root, isWorkdir: g.isWorkdir };
          });
        });

        /* 端兼容：ends 含当前引擎，或（引擎为 opencode 时）迁移后已对其可见。
           不兼容不隐藏——用户可能有意跨端选，只标黄提示。 */
        function compat(item) {
          if (!tool) return true;
          var ends = item.entry.ends || [];
          if (ends.indexOf(tool) >= 0) return true;
          return tool === 'opencode' && !!item.entry.opencode_visible;
        }

        /* 取 copies 里与当前引擎匹配的一份；没有就回退第一份（注入层按名字跨端兜底） */
        function copyOf(item) {
          var copies = item.entry.copies || [];
          for (var i = 0; i < copies.length; i++) if (copies[i].tool === tool) return copies[i];
          return copies[0] || null;
        }

        function keyOfItem(item) {
          var copy = copyOf(item);
          return keyOf(item.entry.name, copy ? copy.scope : item.scope);
        }

        function itemByKey(key) {
          for (var i = 0; i < groupItems.length; i++) {
            for (var j = 0; j < groupItems[i].length; j++) {
              if (keyOfItem(groupItems[i][j]) === key) return groupItems[i][j];
            }
          }
          return null;
        }

        function refOf(item) {
          var copy = copyOf(item);
          return {
            name: item.entry.name,
            source_tool: copy && copy.tool ? copy.tool : tool,
            source_scope: copy && copy.scope ? copy.scope : item.scope,
            source_project: copy && copy.project ? copy.project : (item.project || ''),
            source_path: copy && copy.path ? copy.path : '',
            _ends: (item.entry.ends || []).slice(),
            _visible: !!item.entry.opencode_visible
          };
        }

        function countSelected() {
          var names = {};
          Object.keys(selected).forEach(function (k) { names[selected[k].name] = 1; });
          return Object.keys(names).length;
        }

        function updateCount() {
          var el = document.getElementById('a4sp-count');
          if (el) el.textContent = String(countSelected());
        }

        function rowHtml(item) {
          var e = item.entry;
          var key = keyOfItem(item);
          var ok = compat(item);
          var desc = e.description || '';
          var ends = (e.ends || []).map(function (t) {
            return '<em class="skillpick-end">' + escapeHtml(t) + '</em>';
          }).join('');
          return '<label class="skillpick-item' + (ok ? '' : ' is-warn') + '" data-key="' + escapeHtml(key) + '">' +
            '<input type="checkbox"' + (selected[key] ? ' checked' : '') + '>' +
            '<span class="skillpick-main">' +
              '<span class="skillpick-line">' +
                '<span class="skillpick-name">' + escapeHtml(e.display_name || e.name) + '</span>' +
                '<span class="skillpick-scope">' + escapeHtml(item.scope === 'project' ? (item.isWorkdir ? '工作目录' : '项目') : '全局') + '</span>' +
              '</span>' +
              (desc ? '<span class="skillpick-desc" title="' + escapeHtml(desc) + '">' + escapeHtml(desc) + '</span>' : '') +
            '</span>' +
            (ends ? '<span class="skillpick-ends">' + ends + '</span>' : '') +
            (ok ? '' : '<span class="skillpick-warn">该端不会加载此技能</span>') +
          '</label>';
        }

        function matchKw(item, kw) {
          if (!kw) return true;
          var e = item.entry;
          return ((e.display_name || '') + ' ' + (e.name || '') + ' ' + (e.description || ''))
            .toLowerCase().indexOf(kw) >= 0;
        }

        function renderList() {
          var box = document.getElementById('a4sp-list');
          var search = document.getElementById('a4sp-search');
          if (!box || !search) return;   // 弹窗已被用户关掉
          var kw = (search.value || '').trim().toLowerCase();
          var any = false;
          var html = '';
          groupItems.forEach(function (list, gi) {
            var rows = list.filter(function (it) { return matchKw(it, kw); });
            if (!rows.length) return;
            any = true;
            var g = groups[gi];
            html += '<div class="skillpick-group">' +
              '<span class="skillpick-group-label">' +
                (g.scope === 'project' ? (g.isWorkdir ? '工作目录技能' : '项目技能') : '全局技能') + '</span>' +
              (g.root ? '<span class="skillpick-group-path" title="' + escapeHtml(g.root) + '">' + escapeHtml(g.root) + '</span>' : '') +
            '</div>';
            html += rows.map(rowHtml).join('');
          });
          box.innerHTML = any ? html
            : '<div class="empty-tip">' + (kw ? '没有匹配的技能' : '没有可用的技能') + '</div>';
          box.querySelectorAll('.skillpick-item input[type="checkbox"]').forEach(function (cb) {
            cb.onchange = function () {
              var label = cb.closest('.skillpick-item');
              var key = label ? label.getAttribute('data-key') : '';
              if (cb.checked) {
                if (countSelected() >= MAX_SKILLS) {
                  cb.checked = false;
                  layer.msg('一次最多选择 20 个技能', { icon: 0 });
                  return;
                }
                var item = itemByKey(key);
                if (item) selected[key] = refOf(item);
              } else {
                delete selected[key];
              }
              updateCount();
            };
          });
        }

        var index = layer.open({
          type: 1,
          title: '选择技能',
          area: ['700px', '560px'],
          btn: ['确定', '取消'],
          content: '<div class="skill-picker">' +
            (workdir ? '' :
              '<div class="skillpick-tip">未选工作目录时技能不会注入到项目，仅作为提示词建议</div>') +
            '<div class="skillpick-search">' +
              '<input type="text" class="layui-input" id="a4sp-search" placeholder="搜索名称或描述">' +
            '</div>' +
            '<div class="skillpick-list" id="a4sp-list"><div class="empty-tip">加载中…</div></div>' +
            '<div class="skillpick-foot">' +
              '<span>已选 </span><span id="a4sp-count">0</span><span> / ' + MAX_SKILLS + '</span>' +
            '</div>' +
          '</div>',
          success: function () {
            renderList();
            updateCount();
            var search = document.getElementById('a4sp-search');
            if (search) search.oninput = renderList;
          },
          yes: function (openIndex) {
            /* 确认时按展示顺序收集并按名字去重（同名跨 scope 勾了两份只会注入一份） */
            var refs = [];
            var names = {};
            groupItems.forEach(function (list) {
              list.forEach(function (item) {
                var ref = selected[keyOfItem(item)];
                if (ref && !names[ref.name]) {
                  names[ref.name] = 1;
                  refs.push(ref);
                }
              });
            });
            finish(refs);
            layer.close(openIndex);
          },
          cancel: function () { finish(null); },
          end: function () { finish(null); }
        });
      });
    }
  }

  window.A4SkillPicker = { pick: pick };
});
