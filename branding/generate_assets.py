"""
Generate Shopify App Store assets for Drop Sigma.

Produces two PNGs in this folder:
  • icon.png   — 1024×1024 (App icon)
  • banner.png — 1920×1080 (Feature banner)

Brand palette:
  Indigo  #6366F1
  Purple  #A855F7
  Ink-1   #E5E7EB (off-white text on dark bg)

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
SIGMA_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Apple Symbols.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def gradient_fill(size, start_rgb, end_rgb, angle_deg=135):
    """Create an RGB gradient image at the given angle.
    Uses a diagonal sweep: precomputes the dot-product of each pixel
    with the gradient direction vector for a perfectly smooth ramp."""
    import math
    w, h = size
    img = Image.new("RGB", size, start_rgb)
    px = img.load()
    rad = math.radians(angle_deg)
    dx, dy = math.cos(rad), math.sin(rad)
    # Project corners to find the gradient extent along the direction.
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
    """Create an alpha mask with rounded corners — for clipping the icon."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def measure(draw, text, font):
    """Return (width, height) of the rendered text."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0], bbox[3] - bbox[1]), (bbox[0], bbox[1])


# ──────────────────────────────────────────────────────────────────────
# 1. APP ICON — 1024×1024
# Design: rounded-square gradient (indigo → purple) with a bold white
# Σ centered. Includes subtle inner highlight and bottom-right glow
# for depth so the icon doesn't read as flat at small sizes.
# ──────────────────────────────────────────────────────────────────────
def make_icon():
    SIZE = (1024, 1024)
    INDIGO = (99, 102, 241)
    PURPLE = (168, 85, 247)

    # 1) Diagonal gradient background.
    bg = gradient_fill(SIZE, INDIGO, PURPLE, angle_deg=135)

    # 2) Soft radial highlight in the top-left to give a 3D feel.
    highlight = Image.new("RGBA", SIZE, (255, 255, 255, 0))
    hi_draw = ImageDraw.Draw(highlight)
    # large semi-transparent white circle off-canvas top-left
    hi_draw.ellipse((-450, -450, 700, 700), fill=(255, 255, 255, 38))
    highlight = highlight.filter(ImageFilter.GaussianBlur(180))
    bg = bg.convert("RGBA")
    bg.alpha_composite(highlight)

    # 3) Deep shadow in the bottom-right for depth.
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(shadow)
    sh_draw.ellipse((400, 400, 1500, 1500), fill=(20, 12, 60, 80))
    shadow = shadow.filter(ImageFilter.GaussianBlur(200))
    bg.alpha_composite(shadow)

    # 4) Draw the Σ glyph (Greek capital Sigma) centered.
    sigma_font = best_font(SIGMA_CANDIDATES, 720)
    draw = ImageDraw.Draw(bg)
    glyph = "Σ"
    (gw, gh), (ox, oy) = measure(draw, glyph, sigma_font)
    # Center horizontally; nudge upward slightly for visual centering
    # (capital letters often look low-balanced).
    x = (SIZE[0] - gw) // 2 - ox
    y = (SIZE[1] - gh) // 2 - oy - 30

    # Subtle drop shadow under the glyph.
    shadow_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    sd.text((x + 8, y + 14), glyph, font=sigma_font, fill=(15, 10, 50, 140))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(12))
    bg.alpha_composite(shadow_layer)

    # The glyph itself in soft white.
    draw.text((x, y), glyph, font=sigma_font, fill=(255, 255, 255, 255))

    # 5) Clip to rounded corners (Shopify auto-rounds but our own
    # rounding renders cleaner and preserves edges at small sizes).
    radius = int(SIZE[0] * 0.22)  # ~22% — modern iOS-style corner radius
    mask = rounded_mask(SIZE, radius)
    final = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    final.paste(bg, (0, 0), mask)

    out_path = os.path.join(HERE, "icon.png")
    final.save(out_path, "PNG", optimize=True)
    print(f"✅ Icon saved → {out_path}  ({os.path.getsize(out_path):,} bytes)")
    return out_path


# ──────────────────────────────────────────────────────────────────────
# 2. FEATURE BANNER — 1920×1080
# Design: dark navy → indigo → purple gradient. Left side: brand mark
# + tagline + sub-tagline + free-install pill. Right side: large
# semi-transparent Σ as visual anchor + subtle blob shapes.
# ──────────────────────────────────────────────────────────────────────
def make_banner():
    SIZE = (1920, 1080)
    NAVY = (15, 17, 47)      # deep navy
    INDIGO = (60, 65, 170)
    PURPLE = (130, 70, 200)

    # 1) Multi-stop gradient — paint in two passes.
    bg = gradient_fill(SIZE, NAVY, INDIGO, angle_deg=120).convert("RGBA")
    overlay = gradient_fill(SIZE, INDIGO, PURPLE, angle_deg=60).convert("RGBA")
    # Soften the overlay so navy dominates.
    overlay.putalpha(110)
    bg.alpha_composite(overlay)

    # 2) Decorative blurred blobs (gives depth without being noisy).
    blobs = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    bd = ImageDraw.Draw(blobs)
    bd.ellipse((1100, -150, 1900, 750), fill=(168, 85, 247, 120))  # purple top-right
    bd.ellipse((-200, 600, 700, 1300), fill=(99, 102, 241, 100))   # indigo bottom-left
    blobs = blobs.filter(ImageFilter.GaussianBlur(140))
    bg.alpha_composite(blobs)

    draw = ImageDraw.Draw(bg)

    # 3) Brand wordmark — top-left.
    brand_font = best_font(BOLD_CANDIDATES, 56)
    sig_font_small = best_font(SIGMA_CANDIDATES, 72)

    # Σ chip
    chip_size = 92
    chip_x, chip_y = 120, 110
    chip_bg = Image.new("RGBA", (chip_size, chip_size), (0, 0, 0, 0))
    cb = ImageDraw.Draw(chip_bg)
    cb.rounded_rectangle((0, 0, chip_size, chip_size), radius=22,
                         fill=(255, 255, 255, 235))
    bg.alpha_composite(chip_bg, (chip_x, chip_y))
    # Σ glyph inside chip
    (sw, sh), (sox, soy) = measure(draw, "Σ", sig_font_small)
    draw.text((chip_x + (chip_size - sw) // 2 - sox,
               chip_y + (chip_size - sh) // 2 - soy - 4),
              "Σ", font=sig_font_small, fill=(99, 102, 241, 255))

    # Wordmark next to chip
    draw.text((chip_x + chip_size + 26, chip_y + 18),
              "Drop Sigma", font=brand_font, fill=(255, 255, 255, 255))

    # 4) Headline tagline.
    headline_font = best_font(BOLD_CANDIDATES, 120)
    sub_font      = best_font(REG_CANDIDATES, 44)
    pill_font     = best_font(BOLD_CANDIDATES, 28)

    head_x = 120
    head_y = 330
    # Two lines for visual rhythm.
    draw.text((head_x, head_y), "AI replies.",
              font=headline_font, fill=(255, 255, 255))
    draw.text((head_x, head_y + 145), "Returns.",
              font=headline_font, fill=(255, 255, 255))
    draw.text((head_x, head_y + 290), "Done.",
              font=headline_font, fill=(168, 85, 247))  # accent purple on punch word

    # 5) Sub-headline.
    sub_y = head_y + 460
    draw.text((head_x, sub_y),
              "Reply faster. Refund smarter. Coordinate vendors —",
              font=sub_font, fill=(220, 222, 240))
    draw.text((head_x, sub_y + 60),
              "all from one dashboard built for Shopify merchants.",
              font=sub_font, fill=(220, 222, 240))

    # 6) Free-install pill, bottom-left.
    pill_text = "Free to install · No credit card required"
    (pw, ph), (pox, poy) = measure(draw, pill_text, pill_font)
    pill_x = 120
    pill_y = 920
    pad_x, pad_y = 28, 16
    pill_bg = Image.new("RGBA", (pw + pad_x * 2, ph + pad_y * 2),
                        (0, 0, 0, 0))
    pb = ImageDraw.Draw(pill_bg)
    pb.rounded_rectangle((0, 0, pw + pad_x * 2, ph + pad_y * 2),
                         radius=(ph + pad_y * 2) // 2,
                         fill=(255, 255, 255, 38),
                         outline=(255, 255, 255, 90), width=2)
    bg.alpha_composite(pill_bg, (pill_x, pill_y))
    draw.text((pill_x + pad_x - pox, pill_y + pad_y - poy),
              pill_text, font=pill_font, fill=(255, 255, 255))

    # 7) Giant Σ on the right side as visual anchor (semi-transparent).
    giant_font = best_font(SIGMA_CANDIDATES, 980)
    giant_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    gd = ImageDraw.Draw(giant_layer)
    (gw, gh), (gox, goy) = measure(gd, "Σ", giant_font)
    gx = SIZE[0] - gw - 100 - gox + 40
    gy = (SIZE[1] - gh) // 2 - goy - 20
    gd.text((gx, gy), "Σ", font=giant_font, fill=(255, 255, 255, 38))
    # subtle inner glow
    glow = giant_layer.filter(ImageFilter.GaussianBlur(8))
    bg.alpha_composite(glow)
    gd2 = ImageDraw.Draw(bg)
    gd2.text((gx, gy), "Σ", font=giant_font, fill=(255, 255, 255, 28))

    out_path = os.path.join(HERE, "banner.png")
    bg.convert("RGB").save(out_path, "PNG", optimize=True)
    print(f"✅ Banner saved → {out_path}  ({os.path.getsize(out_path):,} bytes)")
    return out_path


if __name__ == "__main__":
    print("Generating Drop Sigma Shopify App Store assets…\n")
    make_icon()
    make_banner()
    print("\nDone. Both PNGs are in branding/")
