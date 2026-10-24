# -*- coding: utf-8 -*-
# a4agent logo：基线（含斜笔）重建验证 + 新版（去斜笔）
# 坐标系：viewBox 0..100，笔画宽 10，竖杆 x 45..55，横杆 y 45..55
# 结构：单一外轮廓环 + 内腔环（counter1/counter2）+ 点，nonzero 填充
import os, subprocess, math

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = os.path.dirname(os.path.abspath(__file__))

def rp(pts):
    """圆角多边形 path d。pts: [(x,y,r),...]，按顶点顺序连线，角处插圆弧。"""
    n = len(pts)
    d = []
    for i in range(n):
        x0, y0, r0 = pts[i]
        xp, yp, _ = pts[(i - 1) % n]
        xn, yn, _ = pts[(i + 1) % n]
        din = (x0 - xp, y0 - yp); dout = (xn - x0, yn - y0)
        lin = math.hypot(*din); lout = math.hypot(*dout)
        din = (din[0]/lin, din[1]/lin); dout = (dout[0]/lout, dout[1]/lout)
        cross = din[0]*dout[1] - din[1]*dout[0]
        dot = din[0]*dout[0] + din[1]*dout[1]
        interior = math.pi - math.atan2(abs(cross), dot)
        if r0 <= 0:
            d.append(f"L{x0},{y0}" if d else f"M{x0},{y0}")
            continue
        t = min(r0/math.tan(interior/2), 0.499*lin, 0.499*lout)
        ax, ay = x0-din[0]*t, y0-din[1]*t
        bx, by = x0+dout[0]*t, y0+dout[1]*t
        sf = 1 if cross > 0 else 0
        d.append(f"L{ax:.2f},{ay:.2f}" if d else f"M{ax:.2f},{ay:.2f}")
        d.append(f"A{r0},{r0} 0 0 {sf} {bx:.2f},{by:.2f}")
    d.append("Z")
    return "".join(d)

def rrect(x0, y0, x1, y1, r):
    return rp([(x0, y0, r), (x1, y0, r), (x1, y1, r), (x0, y1, r)])

DOT1 = rrect(73, 28, 83, 38, 2.5)
DOT2 = rrect(28, 73, 38, 83, 2.5)
COUNTER1 = [(55, 10, 6), (55, 45, 0), (90, 45, 6), (90, 10, 6)]
COUNTER2 = [(10, 55, 6), (10, 90, 6), (45, 90, 6), (45, 55, 0)]

# 基线外轮廓：左上被斜笔外缘斜切，(0,48) 处小圆角接入左缘
BASE_OUTER = [(48, 0, 0), (100, 0, 16), (100, 55, 16), (55, 55, 0),
              (55, 100, 16), (0, 100, 16), (0, 48, 3)]
# 基线内腔：4 的三角内腔（尖角）+ 两个 a 碗内腔
BASE_SLIVER = [(45, 17, 0), (17, 45, 0), (45, 45, 0)]

# 新版外轮廓：去斜笔，竖杆顶到 y=0、横杆左端到 x=0，新端角 (45,0)/(0,45) 取 r16
# 十字交叉中心四角 (45,45)/(55,45)/(45,55)/(55,55) 保持直角（用户定稿），碗内腔其余内角 r6
NEW_OUTER = [(45, 0, 16), (100, 0, 16), (100, 55, 16), (55, 55, 0),
             (55, 100, 16), (0, 100, 16), (0, 45, 16), (45, 45, 0)]

def glyph_svg(outer, holes):
    parts = [rp(outer)] + [rp(h) for h in holes] + [DOT1, DOT2]
    return '<path fill-rule="nonzero" d="' + "".join(parts) + '"/>'

def tile_svg(inner, size=512):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
            f'width="{size}" height="{size}">'
            f'<rect width="100" height="100" rx="22" fill="#16BAAA"/>'
            f'<g transform="translate(14,14) scale(0.72)" fill="#FFFFFF">{inner}</g></svg>')

def render(svg, name, size=512):
    html = os.path.join(OUT, name + ".html")
    png = os.path.join(OUT, name + ".png")
    with open(html, "w", encoding="utf-8") as f:
        f.write(f'<html><body style="margin:0">{svg}</body></html>')
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--force-device-scale-factor=1",
                    f"--window-size={size},{size}", "--default-background-color=00000000",
                    f"--screenshot={png}", html], capture_output=True, timeout=60)
    return png

base_inner = glyph_svg(BASE_OUTER, [BASE_SLIVER, COUNTER1, COUNTER2])
new_inner = glyph_svg(NEW_OUTER, [COUNTER1, COUNTER2])

if __name__ == "__main__":
    render(tile_svg(base_inner), "baseline-tile")
    render(tile_svg(new_inner), "new-tile")
    print("rendered")
