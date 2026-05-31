# Drop Sigma — Shopify App Store Branding Assets

Generated assets ready for Partner Dashboard submission.

## Files

| File | Spec | Use |
|------|------|-----|
| `icon.png` | 1024×1024 PNG | App icon — appears in App Store search, install page, merchant's Shopify admin |
| `banner.png` | 1920×1080 PNG | Feature banner — hero image on the app's listing page |

## Design

**Brand mark:** Greek capital Σ (Sigma) — ties directly to the name "Drop Sigma", instantly recognizable, mathematical/professional feel, readable down to 32×32 in App Store search results.

**Palette:**
- Indigo `#6366F1`
- Purple `#A855F7`
- Navy `#0F112F` (banner background depth)
- White text on dark/gradient

**Icon style:** iOS-style rounded square (22% corner radius), diagonal indigo→purple gradient, soft top-left highlight + bottom-right shadow for depth.

**Banner layout:** dark navy→indigo→purple gradient with decorative blurred blobs. Left side carries the brand chip + wordmark + bold three-line tagline ("AI replies. / Returns. / **Done.**") + "Free to install" pill. Right side anchored by a large semi-transparent Σ mark.

## Regenerating

If you want to tweak colors, copy, or layout — edit `generate_assets.py` and re-run:

```bash
source venv/bin/activate
python branding/generate_assets.py
```

Both PNGs will be overwritten in place.

## Submission notes

These assets meet Shopify Partner Dashboard's App Listing requirements:
- Icon: 1024×1024 PNG, transparent corners (rounded mask applied), no Shopify trademarks
- Banner: 1920×1080 PNG, RGB
- Both are under 5 MB (the dashboard upload cap)

The icon is intentionally NOT pre-rounded as a perfect circle — Shopify auto-displays it inside its own corner-radius mask on different surfaces, so a square-with-soft-corners is the safest format.
