"""调查项目里所有下拉框的实际渲染样式：截图 + 采集计算样式。

用法：uv run --with playwright python tools/probe_selects.py
输出：.run/select_probe.json（数据）、.run/select_*.png（截图）
"""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18900"
OUT = Path(__file__).resolve().parent.parent / ".run"
OUT.mkdir(exist_ok=True)

# (说明, 页签 lay-id, 需先点击的按钮文案或None)
PROBES = [
    ("任务下发", "tasks", None),
    ("通知提醒", "phone", None),
    ("供应商管理-弹窗", "providers", "新增供应商"),
    ("本地模型", "llama", None),
    ("任务下发-引擎回传", "tasks", None),
]

# 额外：把某个 select 展开，拍下原生下拉面板的样子
EXPAND_TARGETS = [
    ("任务下发", "tasks", "#task-timeout"),
    ("通知提醒", "phone", "#phone-hook-timeout"),
]

PROBE_JS = """
() => {
  const out = [];
  document.querySelectorAll('select').forEach(el => {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const label = el.closest('.task-form-row,.layui-input-block,.config-form,.llama-row,div');
    let name = '';
    // 上溯找最近的label 文案
    let p = el;
    for (let i = 0; i < 4 && p; i++) {
      const lb = p.querySelector && p.querySelector('label');
      if (lb && lb.textContent.trim()) { name = lb.textContent.trim().slice(0, 20); break; }
      p = p.parentElement;
    }
    out.push({
      id: el.id || '(无 id)',
      className: el.className || '(无 class)',
      label: name,
      width: Math.round(r.width),
      height: Math.round(r.height),
      visible: r.width > 0 && r.height > 0,
      appearance: cs.appearance,
      background: cs.backgroundColor,
      border: cs.border,
      borderRadius: cs.borderRadius,
      padding: cs.padding,
      fontSize: cs.fontSize,
      color: cs.color,
      optionCount: el.options ? el.options.length : 0,
      firstOptions: el.options ? Array.from(el.options).slice(0, 4).map(o => o.text) : []
    });
  });
  // layui 会把带 lay-filter/lay-search 的 select 渲染成自定义下拉
  // （原select 隐藏，外层是 .layui-form-select + .layui-select-title dl）
  document.querySelectorAll('.layui-form-select').forEach(box => {
    const src = box.previousElementSibling;
    const title = box.querySelector('.layui-select-title input, .layui-select-title');
    const r = (title || box).getBoundingClientRect();
    out.push({
      id: (src && src.name ? src.name : '(layui)'),
      className: 'layui-form-select(自定义渲染)',
      label: 'layui form.select',
      width: Math.round(r.width),
      height: Math.round(r.height),
      visible: r.width > 0 && r.height > 0,
      appearance: 'layui 自定义',
      background: getComputedStyle(title || box).backgroundColor,
      border: getComputedStyle(title || box).border,
      borderRadius: getComputedStyle(title || box).borderRadius,
      padding: '',
      fontSize: getComputedStyle(title || box).fontSize,
      color: getComputedStyle(title || box).color,
      optionCount: src && src.options ? src.options.length : 0,
      firstOptions: src && src.options ? Array.from(src.options).slice(0,4).map(o=>o.text) : []
    });
  });
  return out;
}
"""


def main() -> None:
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for label, tab_id, opener in PROBES:
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(800)
            # 真实点击页签：layui 绑定在 li 上，点击才会派发 main-tab-changed
            try:
                page.click(f'li[lay-id="{tab_id}"]')
                page.wait_for_timeout(1400)
            except Exception as exc:
                print(f"[warn] {label} 点击页签异常: {exc}")
            # 部分页面的 select 在弹窗里，需先点开
            if opener:
                try:
                    btn = page.locator(f'text={opener}').first
                    btn.click(timeout=3000)
                    page.wait_for_timeout(1200)
                except Exception as exc:
                    print(f"[warn] {label} 未找到/未点开弹窗({opener}): {exc}")
            page.wait_for_timeout(500)
            data = page.evaluate(PROBE_JS)
            results[label] = data
            print(f"\n=== {label} ===")
            for d in data:
                if not d["visible"]:
                    print(f"  [隐藏] {d['id']}")
                    continue
                print(f"  {d['id']} ({d['className']})")
                print(f"label={d['label']!r}渲染尺寸={d['width']}x{d['height']}px "
                      f"appearance={d['appearance']}")
                print(f"border={d['border']} radius={d['borderRadius']} bg={d['background']}")
                print(f"选项 {d['optionCount']} 个: {d['firstOptions']}")
            # 截图该页签
            safe = "".join(ch if ch.isalnum() else "_" for ch in label)
            shot = OUT / f"select_{safe}.png"
            page.screenshot(path=str(shot), full_page=False)
            print(f"  截图 -> {shot.name}")

        # 抓展开态：原生下拉面板在 headless 下无法真正展开，
        # 改为截图其所在表单区域，用于观察收起态观感
        for label, tab_id, sel in EXPAND_TARGETS:
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(700)
            page.click(f'li[lay-id="{tab_id}"]')
            page.wait_for_timeout(1300)
            try:
                el = page.locator(sel)
                el.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(400)
                row = el.locator("xpath=ancestor::div[contains(@class,'task-form-row')]")
                target = row if row.count() else el
                target.screenshot(path=str(OUT / f"select_closeup_{sel.strip('#')}.png"))
                print(f"\n特写 -> select_closeup_{sel.strip('#')}.png")
            except Exception as exc:
                print(f"[warn] 特写 {sel} 失败: {exc}")
        browser.close()

    (OUT / "select_probe.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n数据 -> {(OUT / 'select_probe.json').name}")


if __name__ == "__main__":
    main()