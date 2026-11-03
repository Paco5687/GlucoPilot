"""Draw the installable-app icons in frontend/public/icons/.

The icons repeat the in-app brand mark: "GP" in bold monospace on the primary
teal (--primary, hsl(168 80% 30%)).

  - icon-192.png, icon-512.png: the rounded-square mark on transparency
    (manifest purpose "any").
  - icon-maskable-512.png: full-bleed teal with the letters inside the central
    safe zone, so Android can crop it to any launcher shape (purpose "maskable").
  - apple-touch-icon.png: the full-bleed version at 180 px; iOS rounds it itself.

Usage (needs Pillow and the DejaVu fonts):
  python3 scripts/make_pwa_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TEAL = (15, 138, 113)  # hsl(168 80% 30%)
WHITE = (255, 255, 255)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"
SUPERSAMPLE = 4


def _letters(draw: ImageDraw.ImageDraw, size: int, text_width: float) -> None:
    """Center "GP" so it spans text_width of the canvas."""
    font = ImageFont.truetype(FONT, 100)
    left, top, right, bottom = draw.textbbox((0, 0), "GP", font=font)
    font = ImageFont.truetype(FONT, round(100 * text_width * size / (right - left)))
    left, top, right, bottom = draw.textbbox((0, 0), "GP", font=font)
    x = (size - (right - left)) / 2 - left
    y = (size - (bottom - top)) / 2 - top
    draw.text((x, y), "GP", font=font, fill=WHITE)


def rounded(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big * 0.22, fill=TEAL)
    _letters(draw, big, 0.58)
    return img.resize((size, size), Image.LANCZOS)


def full_bleed(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    img = Image.new("RGB", (big, big), TEAL)
    # The maskable safe zone is the centered circle of radius 40%; letters
    # spanning 46% of the width stay inside it.
    _letters(ImageDraw.Draw(img), big, 0.46)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rounded(192).save(OUT / "icon-192.png", optimize=True)
    rounded(512).save(OUT / "icon-512.png", optimize=True)
    full_bleed(512).save(OUT / "icon-maskable-512.png", optimize=True)
    full_bleed(180).save(OUT / "apple-touch-icon.png", optimize=True)
    print(f"wrote 4 icons to {OUT}")


if __name__ == "__main__":
    main()
