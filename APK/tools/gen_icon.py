"""
BeaconMFG 应用图标生成器
- 源图: APK/Beacon.jpg (RGB)
- 产出: app/src/main/res/mipmap-*/{ic_launcher.png, ic_launcher_foreground.png}
       + mipmap-anydpi-v26/{ic_launcher.xml, ic_launcher_round.xml}
       + values/ic_launcher_background.xml (背景色，从源图采样)
- 算法:
   * 从源图采样深色作为 adaptive icon 背景
   * ic_launcher.png: 方形 + 背景色 + 图铺满
   * ic_launcher_foreground.png: 透明背景，图居中并按 66/108 安全区缩放
"""

from PIL import Image
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "Beacon.jpg"
RES = ROOT / "app/src/main/res"

# Android 标准 launcher 密度像素
DENSITIES = {
    "mdpi":    48,
    "hdpi":    72,
    "xhdpi":   96,
    "xxhdpi":  144,
    "xxxhdpi": 192,
}

# 前景 108dp 画布中的可见安全区直径 66dp → 缩放比
SAFE = 66 / 108  # ≈ 0.611

def sample_bg_color(img: Image.Image) -> tuple[int, int, int]:
    """从图像边缘（左/右/上/下 5% 像素）采样最常见的深色作为背景色。"""
    w, h = img.size
    band = max(2, min(w, h) // 20)
    edge = Image.new("RGB", (w, h))
    edge.paste(img, (0, 0))
    pixels = []
    # 上、下、左、右
    for x in range(w):
        pixels += [edge.getpixel((x, y)) for y in range(band)]
        pixels += [edge.getpixel((x, h - 1 - y)) for y in range(band)]
    for y in range(h):
        pixels += [edge.getpixel((x, y)) for x in range(band)]
        pixels += [edge.getpixel((w - 1 - x, y)) for x in range(band)]
    # 取深色 (亮度 < 80) 众数
    dark = [p for p in pixels if (p[0]*299 + p[1]*587 + p[2]*114)//1000 < 80]
    if not dark:
        dark = pixels
    most = Counter(dark).most_common(1)[0][0]
    # 取整为 0x33 步进，看起来干净
    return tuple((c // 0x33) * 0x33 for c in most)  # type: ignore

def make_square(img: Image.Image, size: int) -> Image.Image:
    """缩放到 size×size 方形，保持比例，背景填 bg。"""
    bg = bg_color
    canvas = Image.new("RGB", (size, size), bg)
    img2 = img.copy()
    img2.thumbnail((size, size), Image.LANCZOS)
    x = (size - img2.width) // 2
    y = (size - img2.height) // 2
    if img2.mode != "RGB":
        img2 = img2.convert("RGB")
    canvas.paste(img2, (x, y))
    return canvas

def make_foreground(src: Image.Image, size: int) -> Image.Image:
    """前景画布 size×size（对应 108dp），内容按 SAFE 缩放居中，透明背景。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    inner = int(size * SAFE)
    img2 = src.copy()
    if img2.mode != "RGBA":
        img2 = img2.convert("RGBA")
    img2.thumbnail((inner, inner), Image.LANCZOS)
    x = (size - img2.width) // 2
    y = (size - img2.height) // 2
    canvas.alpha_composite(img2, (x, y))
    return canvas

# --- 主流程 ---
img = Image.open(SRC).convert("RGB")
print(f"源图尺寸: {img.size}, 模式: {img.mode}")

bg_color = sample_bg_color(img)
print(f"采样背景色: #{bg_color[0]:02X}{bg_color[1]:02X}{bg_color[2]:02X}")

# 写各密度 PNG
for dens, px in DENSITIES.items():
    out_dir = RES / f"mipmap-{dens}"
    out_dir.mkdir(parents=True, exist_ok=True)

    full = make_square(img, px)
    full.save(out_dir / "ic_launcher.png", "PNG", optimize=True)

    fg = make_foreground(img, px)
    fg.save(out_dir / "ic_launcher_foreground.png", "PNG", optimize=True)

    print(f"  mipmap-{dens}: ic_launcher.png ({px}px), ic_launcher_foreground.png ({px}px)")

# 写 adaptive icon XML (API 26+)
anydpi = RES / "mipmap-anydpi-v26"
anydpi.mkdir(parents=True, exist_ok=True)

adaptive_xml = '''<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
'''
(anydpi / "ic_launcher.xml").write_text(adaptive_xml, encoding="utf-8")
(anydpi / "ic_launcher_round.xml").write_text(adaptive_xml, encoding="utf-8")
print("  mipmap-anydpi-v26/ic_launcher.xml + ic_launcher_round.xml")

# 写背景色资源
vals = RES / "values"
vals.mkdir(parents=True, exist_ok=True)
color_xml = f'''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <color name="ic_launcher_background">#{bg_color[0]:02X}{bg_color[1]:02X}{bg_color[2]:02X}</color>
</resources>
'''
(vals / "ic_launcher_background.xml").write_text(color_xml, encoding="utf-8")
print(f"  values/ic_launcher_background.xml = #{bg_color[0]:02X}{bg_color[1]:02X}{bg_color[2]:02X}")

print("\n✓ 图标资源生成完毕")