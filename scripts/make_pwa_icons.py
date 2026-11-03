"""Draw the installable-app icons in frontend/public/icons/.

The icons are a red blood drop, the same mark as the 🩸 favicon, drawn as a
vector shape so it stays sharp at every size.

  - drop-192.png, drop-512.png: the drop on transparency (manifest purpose "any").
  - drop-maskable-512.png: the drop on white, inside the central safe zone, so
    Android can crop it to any launcher shape (purpose "maskable").
  - drop-apple-touch.png: the drop on white at 180 px; iOS rounds it itself.

The file names change whenever the design does: Cloudflare and installed apps
cache icons by URL.

Usage (needs Pillow):
  python3 scripts/make_pwa_icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

RED = (220, 40, 40)  # hsl(0 72% 51%), the app's --glucose-low / --destructive
HIGHLIGHT = (255, 255, 255, 120)
WHITE = (255, 255, 255)
OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"
SUPERSAMPLE = 4
TIP = 1.95  # distance from the bulb's center to the tip, in bulb radii
PINCH = 0.12  # how far the sides bow inward toward the tip, in bulb radii


def _outline(steps: int = 360) -> list[tuple[float, float]]:
    """A round bulb with the tip up, joined by slightly concave sides, as
    (x, y) with y growing downward, scaled to a unit-height box on x = 0."""
    alpha = math.acos(1 / TIP)  # where a straight side would touch the bulb
    tangent = (math.sin(alpha), -math.cos(alpha))
    tip = (0.0, -TIP)

    def along(f: float) -> tuple[float, float]:
        return (tip[0] + f * (tangent[0] - tip[0]), tip[1] + f * (tangent[1] - tip[1]))

    # Cubic Bezier for the right side: the first control point is pulled in
    # toward the axis (a fine, slightly concave tip); the second lies on the
    # straight side, so the curve meets the bulb without a corner.
    p1 = along(0.4)
    p1 = (p1[0] - PINCH * math.sin(alpha), p1[1] + PINCH * math.cos(alpha))
    p2 = along(0.75)
    right = []
    for i in range(steps + 1):
        t = i / steps
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        right.append((
            a * tip[0] + b * p1[0] + c * p2[0] + d * tangent[0],
            a * tip[1] + b * p1[1] + c * p2[1] + d * tangent[1],
        ))
    # Around the bottom of the bulb, from the right tangent point to the left.
    start = -math.pi / 2 + alpha
    sweep = 2 * math.pi - 2 * alpha
    arc = [(math.cos(start + sweep * i / steps), math.sin(start + sweep * i / steps)) for i in range(1, steps)]
    left = [(-x, y) for x, y in reversed(right)]
    height = TIP + 1
    return [(x / height, (y + TIP) / height) for x, y in right + arc + left]


def _drop(img: Image.Image, height: float, center_y: float) -> None:
    """Draw the drop height px tall, its box centered horizontally and its
    vertical box center at center_y."""
    size = img.width
    top = center_y - height / 2
    shape = [(size / 2 + x * height, top + y * height) for x, y in _outline()]
    ImageDraw.Draw(img).polygon(shape, fill=RED)

    # Highlight: a soft arc inside the lower-left of the bulb, like the emoji's.
    half_width = height / (TIP + 1)
    bulb_y = top + height - half_width
    r = half_width * 0.62
    stroke = half_width * 0.16
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = (size / 2 - r, bulb_y - r, size / 2 + r, bulb_y + r)
    start, end = 125, 195
    draw.arc(box, start, end, fill=HIGHLIGHT, width=round(stroke))
    for angle in (start, end):
        cx = size / 2 + (r - stroke / 2) * math.cos(math.radians(angle))
        cy = bulb_y + (r - stroke / 2) * math.sin(math.radians(angle))
        draw.ellipse((cx - stroke / 2, cy - stroke / 2, cx + stroke / 2, cy + stroke / 2), fill=HIGHLIGHT)
    img.alpha_composite(layer)


def plain(size: int) -> Image.Image:
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _drop(img, big * 0.92, big / 2)
    return img.resize((size, size), Image.LANCZOS)


def on_white(size: int, height: float) -> Image.Image:
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), WHITE + (255,))
    # Sit the drop a little low so the bulb, not the box, looks centered.
    _drop(img, big * height, big * 0.52)
    return img.convert("RGB").resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    plain(192).save(OUT / "drop-192.png", optimize=True)
    plain(512).save(OUT / "drop-512.png", optimize=True)
    # The maskable safe zone is the centered circle of radius 40%; a drop
    # 62% tall stays inside it.
    on_white(512, 0.62).save(OUT / "drop-maskable-512.png", optimize=True)
    on_white(180, 0.70).save(OUT / "drop-apple-touch.png", optimize=True)
    print(f"wrote 4 icons to {OUT}")


if __name__ == "__main__":
    main()
