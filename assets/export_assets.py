# -*- coding: utf-8 -*-
# 导出 a4agent 新 logo 资产包：SVG 三版 / PNG 四档 / ICO / 预览页
import os, subprocess, shutil
from PIL import Image
import build_logo as B

OUT = B.OUT
ASSETS = os.path.join(OUT, "a4agent-icon-assets")
os.makedirs(ASSETS, exist_ok=True)

glyph_new = B.glyph_svg(B.NEW_OUTER, [B.COUNTER1, B.COUNTER2])

# SVG 三版：currentColor 裸字形 / 白色裸字形 / 贴片
with open(os.path.join(OUT, "a4agent-glyph.svg"), "w", encoding="utf-8") as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + glyph_new.replace('<path fill-rule', '<path fill="currentColor" fill-rule', 1)
            + '</svg>')
with open(os.path.join(OUT, "a4agent-glyph-white.svg"), "w", encoding="utf-8") as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            + glyph_new.replace('<path fill-rule', '<path fill="#FFFFFF" fill-rule', 1)
            + '</svg>')
with open(os.path.join(OUT, "a4agent-tile.svg"), "w", encoding="utf-8") as f:
    f.write(B.tile_svg(glyph_new))

# PNG 四档（贴片）+ 裸字形 1024 透明
for s in (256, 512, 1024, 2048):
    B.render(B.tile_svg(glyph_new, size=s), f"tile-{s}", size=s)
    shutil.move(os.path.join(OUT, f"tile-{s}.png"), os.path.join(ASSETS, f"a4agent-tile-{s}.png"))
B.render('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="1024" height="1024">'
         + glyph_new.replace('<path fill-rule', '<path fill="#16BAAA" fill-rule', 1) + '</svg>',
         "glyph-1024", size=1024)
shutil.move(os.path.join(OUT, "glyph-1024.png"), os.path.join(ASSETS, "a4agent-glyph-1024.png"))

# ICO：从 512 贴片生成六尺寸
img = Image.open(os.path.join(ASSETS, "a4agent-tile-512.png"))
img.save(os.path.join(ASSETS, "logo.ico"), sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])

# 预览页：定稿展示（旧版对照图已随定稿清理，存在时才附新旧对比）
import base64
old_png = ""
_old = os.path.join(OUT, "baseline-tile.png")
if os.path.exists(_old):
    old_png = "data:image/png;base64," + base64.b64encode(open(_old, "rb").read()).decode()
new_png = "data:image/png;base64," + __import__("base64").b64encode(
    open(os.path.join(ASSETS, "a4agent-tile-512.png"), "rb").read()).decode()
glyph_fill = lambda c: glyph_new.replace('<path fill-rule', f'<path fill="{c}" fill-rule', 1)
tile_card = f'<div class="card"><img src="{new_png}"><p>定稿应用图标 512</p></div>'
old_card = ""
if old_png:
    old_card = f'<div class="card"><img src="{old_png}"><p>旧版（含斜笔）</p></div>'
html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:'Segoe UI',sans-serif;background:#f5f5f5;margin:0;padding:32px;color:#222}}
h1{{font-size:22px}} h2{{font-size:16px;margin:28px 0 12px}}
.row{{display:flex;gap:24px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:12px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.08);text-align:center}}
.card img{{width:180px;height:180px;display:block}}
.card p{{margin:8px 0 0;font-size:13px;color:#666}}
.dark{{background:#1a1a1a}} .brand{{background:#16BAAA}}
.glyph{{width:180px;height:180px}}
.dark .cap{{color:#aaa}}
</style></head><body>
<h1>a4agent Logo 定稿：去除左上斜笔 + 新端角 r16 圆角 + 十字中心四直角</h1>
<h2>定稿应用图标</h2>
<div class="row">{old_card}{tile_card}</div>
<h2>定稿字形三背景</h2>
<div class="row">
  <div class="card"><svg class="glyph" viewBox="0 0 100 100">{glyph_fill("#16BAAA")}</svg><p>白底 / 主色</p></div>
  <div class="card dark"><svg class="glyph" viewBox="0 0 100 100">{glyph_fill("#FFFFFF")}</svg><p class="cap">深底 / 白</p></div>
  <div class="card brand"><svg class="glyph" viewBox="0 0 100 100">{glyph_fill("#FFFFFF")}</svg><p style="color:#fff">品牌底 / 白</p></div>
</div>
<h2>资产清单（a4agent-icon-assets/）</h2>
<p>a4agent-tile-256/512/1024/2048.png、a4agent-glyph-1024.png（透明，主色）、logo.ico（16-256 七尺寸）</p>
</body></html>"""
with open(os.path.join(OUT, "preview.html"), "w", encoding="utf-8") as f:
    f.write(html)

# 清理渲染副产物（保留 preview.html）
import glob
for f in glob.glob(os.path.join(OUT, "tile-*.html")) + \
         glob.glob(os.path.join(OUT, "glyph-*.html")) + \
         glob.glob(os.path.join(OUT, "baseline-*.html")) + \
         glob.glob(os.path.join(OUT, "new-tile.html")):
    os.remove(f)
shutil.rmtree(os.path.join(OUT, "__pycache__"), ignore_errors=True)
print("exported to", ASSETS)
