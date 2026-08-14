"""Generate ATLAS PWA icons into ../frontend/. Run once: python generate_icons.py
Requires Pillow (build-time only, not a runtime dependency)."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT = Path(__file__).parent.parent / "frontend"
NAVY, BLUE, VIOLET = (7, 9, 18), (79, 107, 255), (162, 75, 255)


def diagonal_gradient(size, c1, c2):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return img


def make_icon(size=512):
    base = diagonal_gradient(size, (18, 24, 60), (30, 18, 70))
    # soft central glow
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    r = int(size * 0.42)
    gd.ellipse([size//2 - r, size//2 - r, size//2 + r, size//2 + r], fill=180)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    blue_layer = Image.new("RGB", (size, size), BLUE)
    base = Image.composite(blue_layer, base, glow.point(lambda v: int(v * 0.55)))
    # ring
    d = ImageDraw.Draw(base)
    lw = max(4, size // 60)
    rr = int(size * 0.40)
    d.ellipse([size//2 - rr, size//2 - rr, size//2 + rr, size//2 + rr],
              outline=(150, 180, 255), width=lw)
    # letter A
    font = None
    for fp in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"):
        if Path(fp).exists():
            font = ImageFont.truetype(fp, int(size * 0.6)); break
    if font is None:
        font = ImageFont.load_default()
    text = "A"
    bb = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    pos = ((size - tw) / 2 - bb[0], (size - th) / 2 - bb[1] - size * 0.02)
    d.text((pos[0] + 3, pos[1] + 4), text, font=font, fill=(20, 30, 70))  # shadow
    d.text(pos, text, font=font, fill=(240, 246, 255))
    return base


def main():
    icon = make_icon(512)
    icon.save(OUT / "icon-512.png")
    icon.resize((192, 192), Image.LANCZOS).save(OUT / "icon-192.png")
    icon.save(OUT / "icon-maskable-512.png")            # content is centered/safe
    icon.resize((180, 180), Image.LANCZOS).save(OUT / "apple-touch-icon.png")
    icon.resize((32, 32), Image.LANCZOS).save(OUT / "favicon.png")
    print("Icons written to", OUT)


if __name__ == "__main__":
    main()
