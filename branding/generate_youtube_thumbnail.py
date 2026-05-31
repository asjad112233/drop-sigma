"""
Generate YouTube thumbnail for Drop Sigma demo video.
Spec: 1280×720, PNG, branded.
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "youtube-thumbnail.png")

SIZE   = (1280, 720)
INDIGO = (99, 102, 241)
CYAN   = (6, 182, 212)
CYAN_2 = (34, 211, 238)
NAVY   = (15, 17, 47)
DEEP   = (5, 6, 20)
WHITE  = (255, 255, 255)
SLATE  = (220, 230, 245)

# ─────────────────────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────────────────────
def best_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()

BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
REG = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def gradient_fill(size, start, end, angle=135):
    import math
    w, h = size
    img = Image.new("RGB", size, start)
    px = img.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    dots = [x * dx + y * dy for x, y in corners]
    dmin, dmax = min(dots), max(dots)
    span = max(dmax - dmin, 1)
    for y in range(h):
        for x in range(w):
            t = ((x * dx + y * dy) - dmin) / span
            r = int(start[0] + (end[0] - start[0]) * t)
            g = int(start[1] + (end[1] - start[1]) * t)
            b = int(start[2] + (end[2] - start[2]) * t)
            px[x, y] = (r, g, b)
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return m


def measure(d, text, font):
    b = d.textbbox((0, 0), text, font=font)
    return (b[2] - b[0], b[3] - b[1]), (b[0], b[1])


# ─────────────────────────────────────────────────────────────
# Build thumbnail
# ─────────────────────────────────────────────────────────────
def make_thumbnail():
    # Background: deep navy with subtle gradient
    bg = gradient_fill(SIZE, DEEP, NAVY, 120).convert("RGBA")

    # Decorative glow blobs
    blobs = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    bd.ellipse((850, -100, 1500, 550), fill=(*CYAN, 130))   # cyan top-right
    bd.ellipse((-150, 400, 500, 1000), fill=(*INDIGO, 110))  # indigo bottom-left
    blobs = blobs.filter(ImageFilter.GaussianBlur(120))
    bg.alpha_composite(blobs)

    draw = ImageDraw.Draw(bg)

    # ── Top-left brand chip (small) ──
    chip_size = 60
    chip_x, chip_y = 60, 60
    chip_bg = gradient_fill((chip_size, chip_size), INDIGO, CYAN, 135).convert("RGBA")
    chip_rounded = Image.new("RGBA", (chip_size, chip_size), (0, 0, 0, 0))
    chip_rounded.paste(chip_bg, (0, 0), rounded_mask((chip_size, chip_size), 14))
    cd = ImageDraw.Draw(chip_rounded)
    chip_font = best_font(BOLD, 26)
    (cw, ch), (cox, coy) = measure(cd, "DS", chip_font)
    cd.text(((chip_size - cw) // 2 - cox,
             (chip_size - ch) // 2 - coy - 2),
            "DS", font=chip_font, fill=WHITE)
    bg.alpha_composite(chip_rounded, (chip_x, chip_y))

    # Wordmark
    brand_font = best_font(BOLD, 36)
    draw.text((chip_x + chip_size + 16, chip_y + 14),
              "Drop Sigma", font=brand_font, fill=WHITE)

    # ── Center-left big headline ──
    # Line 1: "AI Email" (white)
    # Line 2: "+ Returns" (cyan)
    # Line 3: "+ Vendors" (white)
    h1_font = best_font(BOLD, 120)

    head_x = 60
    head_y = 200

    draw.text((head_x, head_y), "AI Email",
              font=h1_font, fill=WHITE)

    draw.text((head_x, head_y + 130), "+ Returns",
              font=h1_font, fill=CYAN_2)

    draw.text((head_x, head_y + 260), "+ Vendors",
              font=h1_font, fill=WHITE)

    # ── Sub-tagline ──
    sub_font = best_font(REG, 30)
    sub_y = head_y + 390
    draw.text((head_x, sub_y),
              "Built for Shopify merchants",
              font=sub_font, fill=SLATE)

    # ── "60s DEMO" badge bottom-left ──
    badge_text = "60s DEMO"
    badge_font = best_font(BOLD, 22)
    (bw, bh), (bx_o, by_o) = measure(draw, badge_text, badge_font)
    badge_pad_x, badge_pad_y = 18, 10
    badge_w = bw + badge_pad_x * 2
    badge_h = bh + badge_pad_y * 2
    badge_x = 60
    badge_y = 640
    badge_bg = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    bb = ImageDraw.Draw(badge_bg)
    bb.rounded_rectangle((0, 0, badge_w, badge_h), radius=badge_h // 2,
                         fill=(*CYAN, 230))
    bg.alpha_composite(badge_bg, (badge_x, badge_y))
    draw.text((badge_x + badge_pad_x - bx_o,
               badge_y + badge_pad_y - by_o),
              badge_text, font=badge_font, fill=DEEP)

    # ── Right side: Big DS logo card ──
    # A larger version of the icon — clean, prominent.
    icon_size = 380
    icon_x = SIZE[0] - icon_size - 90
    icon_y = (SIZE[1] - icon_size) // 2

    icon_bg = gradient_fill((icon_size, icon_size), INDIGO, CYAN, 135).convert("RGBA")
    icon_rounded = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    icon_rounded.paste(icon_bg, (0, 0), rounded_mask((icon_size, icon_size), int(icon_size * 0.22)))

    # Top-half white highlight (matches our app icon)
    hi = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    for y in range(icon_size // 2):
        a = int(56 * (1 - y / (icon_size / 2)))
        ImageDraw.Draw(hi).rectangle([(0, y), (icon_size, y + 1)],
                                     fill=(255, 255, 255, a))
    hi_masked = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    hi_masked.paste(hi, (0, 0), rounded_mask((icon_size, icon_size), int(icon_size * 0.22)))
    icon_rounded.alpha_composite(hi_masked)

    # DS text on the icon
    ds_font = best_font(BOLD, 220)
    icon_draw = ImageDraw.Draw(icon_rounded)
    (dw, dh), (dox, doy) = measure(icon_draw, "DS", ds_font)
    icon_draw.text(((icon_size - dw) // 2 - dox,
                    (icon_size - dh) // 2 - doy - 15),
                   "DS", font=ds_font, fill=WHITE)

    # Drop shadow under icon
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(shadow)
    sh_draw.rounded_rectangle(
        (icon_x + 14, icon_y + 24, icon_x + icon_size + 14, icon_y + icon_size + 24),
        radius=int(icon_size * 0.22), fill=(0, 0, 0, 100)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    bg.alpha_composite(shadow)

    # Place icon
    bg.alpha_composite(icon_rounded, (icon_x, icon_y))

    # ── Small "FREE" pill near icon ──
    pill_text = "FREE"
    pill_font = best_font(BOLD, 28)
    (pw, ph), (px_o, py_o) = measure(draw, pill_text, pill_font)
    pill_pad_x, pill_pad_y = 22, 12
    pill_w = pw + pill_pad_x * 2
    pill_h = ph + pill_pad_y * 2
    pill_x = icon_x + (icon_size - pill_w) // 2
    pill_y = icon_y + icon_size + 24
    pill_bg = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pb = ImageDraw.Draw(pill_bg)
    pb.rounded_rectangle((0, 0, pill_w, pill_h), radius=pill_h // 2,
                         fill=WHITE)
    bg.alpha_composite(pill_bg, (pill_x, pill_y))
    draw.text((pill_x + pill_pad_x - px_o,
               pill_y + pill_pad_y - py_o),
              pill_text, font=pill_font, fill=DEEP)

    # Save as PNG (and JPEG for size flexibility)
    final = bg.convert("RGB")
    final.save(OUT, "PNG", optimize=True)
    size_bytes = os.path.getsize(OUT)
    print(f"✅ {OUT}")
    print(f"   {SIZE[0]}×{SIZE[1]} · {size_bytes:,} bytes ({size_bytes / 1024:.1f} KB)")


if __name__ == "__main__":
    make_thumbnail()
