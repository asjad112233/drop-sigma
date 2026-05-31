"""
Generate Shopify App Store assets for Drop Sigma.

Matches the brand mark already in production:
  • coreapp/static/favicon.svg — "DS" on indigo→cyan gradient,
    rounded square with a soft top-half highlight.

Produces two PNGs in this folder:
  • icon.png   — 1024×1024 (App icon)
  • banner.png — 1920×1080 (Feature banner)

Brand palette (from coreapp/static/favicon.svg + templates/home.html CSS):
  Indigo  #6366F1  (gradient start)
  Cyan    #06B6D4  (gradient end — primary brand)
  Purple  #A855F7  (accent — used sparingly on banner)
  Navy    #0F112F  (banner background depth)

Run:
  source venv/bin/activate
  python branding/generate_assets.py
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def best_font(candidates, size):
    """Return ImageFont.truetype from the first existing path, else default."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]
REG_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def gradient_fill(size, start_rgb, end_rgb, angle_deg=135):
    """Create an RGB gradient image at the given angle."""
    import math
    w, h = size
    img = Image.new("RGB", size, start_rgb)
    px = img.load()
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)
    corners = [(0, 0), (w, 0), (0, h), (w, h)]
    dots = [x * dx + y * dy for (x, y) in corners]
    dmin, dmax = min(dots), max(dots)
    span = max(dmax - dmin, 1)
    for y in range(h):
        for x in range(w):
            t = ((x * dx + y * dy) - dmin) / span
            r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * t)
            g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * t)
            b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * t)
            px[x, y] = (r, g, b)
    return img


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def measure(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1]), (bbox[0], bbox[1])


# ──────────────────────────────────────────────────────────────────────
# 1. APP ICON — 1024×1024
# Exact match to coreapp/static/favicon.svg, scaled up:
#   • Rounded square (22% radius)
#   • Indigo→cyan gradient (top-left → bottom-right)
#   • Top-half soft white highlight overlay
#   • Bold "DS" wordmark in white, centered
# ──────────────────────────────────────────────────────────────────────
def make_icon():
    SIZE = (1024, 1024)
    INDIGO = (99, 102, 241)   # #6366F1
    CYAN   = (6, 182, 212)    # #06B6D4

    # 1) Diagonal indigo→cyan gradient (matches favicon).
    bg = gradient_fill(SIZE, INDIGO, CYAN, angle_deg=135).convert("RGBA")

    # 2) Top-half white highlight (matches favicon's <rect height="32"> overlay,
    # which covers the top half with 22% white).
    highlight = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    # Vertical gradient: 22% white at top fading to 0% at midline.
    for y in range(SIZE[1] // 2):
        alpha = int(56 * (1 - y / (SIZE[1] / 2)))  # 56 ≈ 22% of 255
        hd.rectangle([(0, y), (SIZE[0], y + 1)], fill=(255, 255, 255, alpha))
    bg.alpha_composite(highlight)

    # 3) Drop shadow under the wordmark for depth.
    word_font = best_font(BOLD_CANDIDATES, 560)
    draw = ImageDraw.Draw(bg)
    text = "DS"

    (tw, th), (ox, oy) = measure(draw, text, word_font)
    x = (SIZE[0] - tw) // 2 - ox
    y = (SIZE[1] - th) // 2 - oy - 20  # nudge up a touch for visual balance

    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.text((x + 6, y + 12), text, font=word_font, fill=(15, 30, 80, 110))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    bg.alpha_composite(shadow)

    # 4) The white "DS" wordmark.
    draw.text((x, y), text, font=word_font, fill=(255, 255, 255, 255))

    # 5) Clip to rounded corners (22% — matches favicon's rx=14/64).
    radius = int(SIZE[0] * 0.22)
    mask = rounded_mask(SIZE, radius)
    final = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    final.paste(bg, (0, 0), mask)

    out_path = os.path.join(HERE, "icon.png")
    final.save(out_path, "PNG", optimize=True)
    print(f"✅ Icon saved → {out_path}  ({os.path.getsize(out_path):,} bytes)")
    return out_path


# ──────────────────────────────────────────────────────────────────────
# 2. FEATURE BANNER — 1920×1080
# Uses same indigo→cyan brand mark in a chip, with a dark gradient
# canvas and bold three-line tagline. Purple is used ONLY for the
# accent word "Done." to retain the marketing palette of home.html.
# ──────────────────────────────────────────────────────────────────────
def make_banner():
    SIZE = (1920, 1080)
    NAVY   = (15, 17, 47)
    INDIGO = (60, 65, 170)
    CYAN_DK = (12, 90, 130)

    bg = gradient_fill(SIZE, NAVY, INDIGO, angle_deg=120).convert("RGBA")
    overlay = gradient_fill(SIZE, INDIGO, CYAN_DK, angle_deg=60).convert("RGBA")
    overlay.putalpha(95)
    bg.alpha_composite(overlay)

    # Decorative blobs.
    blobs = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    bd.ellipse((1100, -150, 1900, 750), fill=(6, 182, 212, 110))   # cyan top-right
    bd.ellipse((-200, 600, 700, 1300), fill=(99, 102, 241, 110))   # indigo bottom-left
    blobs = blobs.filter(ImageFilter.GaussianBlur(140))
    bg.alpha_composite(blobs)

    draw = ImageDraw.Draw(bg)

    # ── Brand chip (Σ → "DS" — matches favicon) ──
    chip_size = 92
    chip_x, chip_y = 120, 110
    # Mini indigo→cyan gradient for the chip itself so it visually
    # mirrors the favicon at small scale.
    chip_bg = gradient_fill((chip_size, chip_size), (99, 102, 241), (6, 182, 212), 135).convert("RGBA")
    # Rounded corners on the chip.
    chip_mask = rounded_mask((chip_size, chip_size), 22)
    chip_rounded = Image.new("RGBA", (chip_size, chip_size), (0, 0, 0, 0))
    chip_rounded.paste(chip_bg, (0, 0), chip_mask)
    # Top-half highlight on chip
    chip_hi = Image.new("RGBA", (chip_size, chip_size), (0, 0, 0, 0))
    chip_hd = ImageDraw.Draw(chip_hi)
    for y in range(chip_size // 2):
        a = int(56 * (1 - y / (chip_size / 2)))
        chip_hd.rectangle([(0, y), (chip_size, y + 1)], fill=(255, 255, 255, a))
    chip_hi_masked = Image.new("RGBA", (chip_size, chip_size), (0, 0, 0, 0))
    chip_hi_masked.paste(chip_hi, (0, 0), chip_mask)
    chip_rounded.alpha_composite(chip_hi_masked)
    # "DS" text inside chip
    chip_draw = ImageDraw.Draw(chip_rounded)
    chip_font = best_font(BOLD_CANDIDATES, 38)
    (cw, ch), (cox, coy) = measure(chip_draw, "DS", chip_font)
    chip_draw.text(((chip_size - cw) // 2 - cox,
                    (chip_size - ch) // 2 - coy - 2),
                   "DS", font=chip_font, fill=(255, 255, 255))
    bg.alpha_composite(chip_rounded, (chip_x, chip_y))

    # Wordmark next to chip
    brand_font = best_font(BOLD_CANDIDATES, 56)
    draw.text((chip_x + chip_size + 26, chip_y + 18),
              "Drop Sigma", font=brand_font, fill=(255, 255, 255))

    # ── Headline tagline ──
    headline_font = best_font(BOLD_CANDIDATES, 120)
    sub_font      = best_font(REG_CANDIDATES, 44)
    pill_font     = best_font(BOLD_CANDIDATES, 28)

    head_x = 120
    head_y = 330
    draw.text((head_x, head_y), "AI replies.",
              font=headline_font, fill=(255, 255, 255))
    draw.text((head_x, head_y + 145), "Returns.",
              font=headline_font, fill=(255, 255, 255))
    # "Done." accent — cyan (matches brand mark) instead of purple
    draw.text((head_x, head_y + 290), "Done.",
              font=headline_font, fill=(34, 211, 238))  # cyan-2

    # ── Sub-headline ──
    sub_y = head_y + 460
    draw.text((head_x, sub_y),
              "Reply faster. Refund smarter. Coordinate vendors —",
              font=sub_font, fill=(220, 230, 245))
    draw.text((head_x, sub_y + 60),
              "all from one dashboard built for Shopify merchants.",
              font=sub_font, fill=(220, 230, 245))

    # ── Free-install pill ──
    pill_text = "Free to install · No credit card required"
    (pw, ph), (pox, poy) = measure(draw, pill_text, pill_font)
    pill_x = 120
    pill_y = 920
    pad_x, pad_y = 28, 16
    pill_bg = Image.new("RGBA", (pw + pad_x * 2, ph + pad_y * 2), (0, 0, 0, 0))
    pb = ImageDraw.Draw(pill_bg)
    pb.rounded_rectangle((0, 0, pw + pad_x * 2, ph + pad_y * 2),
                         radius=(ph + pad_y * 2) // 2,
                         fill=(255, 255, 255, 38),
                         outline=(255, 255, 255, 90), width=2)
    bg.alpha_composite(pill_bg, (pill_x, pill_y))
    draw.text((pill_x + pad_x - pox, pill_y + pad_y - poy),
              pill_text, font=pill_font, fill=(255, 255, 255))

    # ── Large "DS" visual anchor on the right (semi-transparent) ──
    giant_font = best_font(BOLD_CANDIDATES, 620)
    giant_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    gd = ImageDraw.Draw(giant_layer)
    (gw, gh), (gox, goy) = measure(gd, "DS", giant_font)
    gx = SIZE[0] - gw - 110 - gox
    gy = (SIZE[1] - gh) // 2 - goy - 20
    gd.text((gx, gy), "DS", font=giant_font, fill=(255, 255, 255, 38))
    glow = giant_layer.filter(ImageFilter.GaussianBlur(8))
    bg.alpha_composite(glow)
    gd2 = ImageDraw.Draw(bg)
    gd2.text((gx, gy), "DS", font=giant_font, fill=(255, 255, 255, 26))

    out_path = os.path.join(HERE, "banner.png")
    bg.convert("RGB").save(out_path, "PNG", optimize=True)
    print(f"✅ Banner saved → {out_path}  ({os.path.getsize(out_path):,} bytes)")
    return out_path


if __name__ == "__main__":
    print("Generating Drop Sigma Shopify App Store assets…\n")
    print("Source of truth: coreapp/static/favicon.svg")
    print("Brand mark: 'DS' on indigo→cyan gradient, rounded square.\n")
    make_icon()
    make_banner()
    print("\nDone. Both PNGs are in branding/")
