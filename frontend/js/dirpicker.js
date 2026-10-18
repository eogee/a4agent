/* 目录选择公共组件（window.A4DirPicker）。
 *
 * 一处实现，项目根目录弹窗、技能迁移弹窗、任务下发的工作目录共用：
 * - 桌面壳：走 pywebview 原生对话框（系统级，能直接点到 UNC 共享）；
 * - 浏览器打开 127.0.0.1：JS 拿不到本机绝对路径（浏览器安全策略），只能让同机
 *   后端逐级列目录（/api/v1/fs/dirs）。
 *
 * 统一返回 Promise<string|null>：字符串是所选目录的绝对路径，null 表示用户取消。
 * 调用方不需要关心当前是桌面壳还是浏览器。
 *
 * 浏览器弹窗里保留「手动输入路径」入口——盘符栏只列得出本地盘，\\server\share
 * 这类 UNC 路径在逐级浏览里根本进不去。
 */
layui.use(['layer'], function () {
  var layer = layui.layer;

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function listDirs(path) {
    return fetch('/api/v1/fs/dirs?path=' + encodeURIComponent(path || ''))
      .then(function (r) {
        if (!r.ok) {
          return r.json().then(
            function (j) { throw new Error(j.detail || ('HTTP ' + r.status)); },
            function () { throw new Error('请求失败（HTTP ' + r.status + '）'); }
          );
        }
        return r.json();
      });
  }

  /* 原生对话框：返回 {available, path}。
     available=false 表示当前不是桌面壳（或桥未就绪），调用方应退回内置浏览；
     available=true 且 path=null 表示用户主动取消——此时不能再弹一个浏览窗，
     否则「取消」会变成「又开一个」。 */
  function nativeFolder() {
    var wv = window.pywebview;
    if (!wv || !wv.api || typeof wv.api.select_folder !== 'function') {
      return Promise.resolve({ available: false, path: null });
    }
    return Promise.resolve(wv.api.select_folder()).then(function (p) {
      return { available: true, path: p || null };
    }, function () {
      return { available: false, path: null };
    });
  }

  function browse(opts) {
    return new Promise(function (resolve) {
      var settled = false;
      function finish(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }

      var state = { path: '', parent: null, home: '', drives: [], dirs: [], truncated: false };

      function render() {
        var current = document.getElementById('a4dp-current');
        if (!current) return;
        current.textContent = state.path || '我的电脑';
        document.getElementById('a4dp-up').disabled = state.parent === null;

        var drives = document.getElementById('a4dp-drives');
        drives.innerHTML = (state.drives || []).map(function (d) {
          return '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" ' +
            'data-a4dp-drive="' + escapeHtml(d) + '">' + escapeHtml(d) + '</button>';
        }).join('');
        drives.hidden = !(state.drives || []).length;

        var box = document.getElementById('a4dp-list');
        box.innerHTML = (state.dirs || []).length
          ? state.dirs.map(function (d) {
              return '<button type="button" class="dir-item" data-a4dp-path="' + escapeHtml(d.path) +
                '" title="' + escapeHtml(d.path) + '">' + escapeHtml(d.name) + '</button>';
            }).join('') + (state.truncated ? '<div class="dir-more">子目录过多，仅列出前面部分</div>' : '')
          : '<div class="empty-tip">没有子文件夹</div>';

        box.querySelectorAll('[data-a4dp-path]').forEach(function (b) {
          b.onclick = function () { load(b.getAttribute('data-a4dp-path')); };
        });
        drives.querySelectorAll('[data-a4dp-drive]').forEach(function (b) {
          b.onclick = function () { load(b.getAttribute('data-a4dp-drive')); };
        });
      }

      function load(path) {
        listDirs(path).then(function (data) {
          state = data;
          render();
        }).catch(function (e) {
          var current = document.getElementById('a4dp-current');
          if (current) current.textContent = path || '我的电脑';
          var box = document.getElementById('a4dp-list');
          if (box) box.innerHTML = '<div class="empty-tip">读取失败：' + escapeHtml(e.message) + '</div>';
        });
      }

      var index = layer.open({
        type: 1,
        title: opts.title || '选择文件夹',
        area: ['620px', '520px'],
        btn: ['选择此文件夹', '取消'],
        content: '<div class="dir-picker">' +
          '<div class="dir-bar">' +
            '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" id="a4dp-up">上级</button>' +
            '<button type="button" class="layui-btn layui-btn-xs layui-btn-primary" id="a4dp-home">主目录</button>' +
            '<span class="dir-current" id="a4dp-current">加载中…</span>' +
          '</div>' +
          '<div class="dir-drives" id="a4dp-drives" hidden></div>' +
          '<div class="dir-list" id="a4dp-list"></div>' +
          '<div class="dir-tip">' +
            '<span>' + escapeHtml(opts.tip || '点文件夹进入下一级；进到目标文件夹后点「选择此文件夹」') + '</span>' +
            '<a href="javascript:;" class="dir-manual" id="a4dp-manual">手动输入路径</a>' +
          '</div>' +
        '</div>',
        success: function (layero) {
          layero.find('#a4dp-up').on('click', function () {
            if (state.parent !== null) load(state.parent);
          });
          layero.find('#a4dp-home').on('click', function () { load(state.home || '~'); });
          layero.find('#a4dp-manual').on('click', function () {
            layer.prompt({
              title: '输入文件夹完整路径（支持 \\\\服务器\\共享 这类 UNC 路径）',
              value: state.path || opts.start || '',
              formType: 0
            }, function (value, promptIndex) {
              layer.close(promptIndex);
              var text = String(value || '').trim();
              if (text) load(text);  // 先验证可读，再让用户确认
            });
          });
          load(opts.start || '');
        },
        yes: function (openIndex) {
          if (!state.path) {
            layer.msg('请先进入一个文件夹再确认', { icon: 0 });
            return;
          }
          // 先落结果再关窗：layer 关窗会同步触发 end，晚一步就会被 end 的 null 覆盖
          finish(state.path);
          layer.close(openIndex);
        },
        cancel: function () { finish(null); },
        end: function () { finish(null); }
      });
    });
  }

  function pick(opts) {
    opts = opts || {};
    return nativeFolder().then(function (result) {
      if (result.available) return result.path;   // null = 用户取消
      return browse(opts);
    });
  }

  window.A4DirPicker = { pick: pick, browse: browse, listDirs: listDirs };
});
