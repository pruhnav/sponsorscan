#!/usr/bin/env python3
"""Render assets/job-boards.gif, the bar under the README banner: a picker
wheel rolling through the job sources SponsorScan reads, each with the number
of boards shipped in companies.yaml.

Re-run it after adding a fetcher or changing companies.yaml:

    python scripts/make_job_boards_gif.py

Needs Pillow, PyYAML and cairosvg. The type is Hanken Grotesk, the dashboard's
bundled font, so the bar and the dashboard read as one product.

The logos in assets/logos belong to their owners and appear only to show which
job sources SponsorScan reads.
"""

import io
import math
import sys
from pathlib import Path

import cairosvg
import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
LOGOS = ROOT / "assets" / "logos"
FONT = ROOT / "dashboard" / "hanken-grotesk.ttf"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "assets" / "job-boards.gif"

# companies.yaml key, logo file, display height in px (wordmarks differ in how
# much of their height is letters), a vertical nudge in px that lines the
# letters up with the lead text (Greenhouse's "g" hangs below its baseline),
# colors to draw white on the dark bar, and the colors the logo keeps, for the
# palette.
BOARDS = [("greenhouse", "greenhouse.svg", 29, 4, [], [(76, 179, 152)]),
          ("lever", "lever.png", 19, 0, [], [(5, 119, 103)]),
          ("ashby", "ashby.svg", 24, 0, ["currentColor"], []),
          ("workday", "workday.svg", 36, -2, [], [(252, 91, 5)]),
          ("eightfold", "eightfold.png", 30, 0, ["dark"], [(224, 128, 96), (224, 160, 64), (192, 96, 160)]),
          ("amazon", "amazon.svg", 30, 5, ["#221f1f"], [(255, 153, 0)])]

S = 2                      # draw at twice the size, then downscale for clean edges
W, H = 1200, 76            # same width as assets/banner.svg
BG = (13, 13, 15)
INK = (236, 236, 236)
DIM = (128, 128, 134)
ACCENT = (122, 167, 255)   # the dashboard's blue, lightened for a dark ground
WHITE = (255, 255, 255)


def font(size, weight):
    face = ImageFont.truetype(str(FONT), size * S)
    face.set_variation_by_axes([weight])
    return face


F_LEAD = font(26, 500)
F_COUNT = font(16, 600)
F_TAG = font(16, 500)

SPACING = 36 * S           # distance between wheel rows
HOLD_FRAMES, HOLD_MS = 8, 170
STEPS, STEP_MS = 12, 30


def load_logo(filename, height, to_white):
    """The logo as RGBA at `height` (supersampled), with its dark parts drawn
    white for the dark bar: SVG colors by name, PNG by darkness ("dark")."""
    path = LOGOS / filename
    if path.suffix == ".svg":
        svg = path.read_text(encoding="utf-8")
        for color in to_white:
            svg = svg.replace(color, "#FFFFFF")
        png = cairosvg.svg2png(bytestring=svg.encode(), output_height=height * S)
        img = Image.open(io.BytesIO(png)).convert("RGBA")
    else:
        img = Image.open(path).convert("RGBA")
        if "dark" in to_white:
            pixels = img.load()
            for y in range(img.height):
                for x in range(img.width):
                    r, g, b, a = pixels[x, y]
                    if max(r, g, b) < 110:
                        pixels[x, y] = (255, 255, 255, a)
        img = img.resize((round(img.width * height * S / img.height), height * S),
                         Image.LANCZOS)
    return img.crop(img.getbbox())


def board_counts():
    with open(ROOT / "companies.yaml", encoding="utf-8") as fh:
        companies = (yaml.safe_load(fh) or {}).get("companies") or {}
    return {key: len(companies.get(key) or []) for key, *_ in BOARDS}


COUNTS = board_counts()
LOGO_IMAGES = {key: load_logo(filename, height, white)
               for key, filename, height, _, white, _ in BOARDS}
NUDGE = {key: dy * S for key, _, _, dy, *_ in BOARDS}
TAGLINE = f"{sum(COUNTS.values())} boards, all at employers with real H-1B filings"


def ease(t):
    return 0.5 - 0.5 * math.cos(math.pi * t)


def mix(color, amount):
    """Blend color toward the background; amount 0 is full color, 1 is BG."""
    return tuple(round(c + (b - c) * amount) for c, b in zip(color, BG))


def count_label(key):
    n = COUNTS[key]
    # A reader with no boards listed yet is still one you can add boards to.
    return "supported" if n == 0 else f"{n} board{'s' if n != 1 else ''}"


def wheel_row(key, fade):
    """One wheel row, the logo then its board count, faded toward BG."""
    logo = LOGO_IMAGES[key]
    row_h = 44 * S
    row = Image.new("RGBA", (logo.width + 220 * S, row_h), BG + (255,))
    faded = logo.copy()
    faded.putalpha(faded.getchannel("A").point(lambda a: round(a * (1 - fade))))
    row.alpha_composite(faded, (0, (row_h - logo.height) // 2 + NUDGE[key]))
    ImageDraw.Draw(row).text((logo.width + 14 * S, row_h // 2 + 1 * S), count_label(key),
                             font=F_COUNT, fill=mix(ACCENT, 0.15 + 0.85 * fade), anchor="lm")
    return row.convert("RGB")


def frame(position):
    """position is a float index into BOARDS."""
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    cy = H * S // 2

    x = 40 * S
    d.text((x, cy), "Live postings from", font=F_LEAD, fill=INK, anchor="lm")
    x += d.textlength("Live postings from ", font=F_LEAD) + 6 * S

    tag_w = d.textlength(TAGLINE, font=F_TAG)
    d.text((W * S - 40 * S - tag_w, cy), TAGLINE, font=F_TAG, fill=DIM, anchor="lm")

    # The wheel: neighbours above and below, fading and flattening with
    # distance, clipped by the bar like a physical picker window.
    for k in range(-2, 3):
        i = math.floor(position) + k
        offset = i - position
        if abs(offset) > 1.2:
            continue
        key = BOARDS[i % len(BOARDS)][0]
        row = wheel_row(key, min(1.0, abs(offset) * 1.05))
        squash = max(0.3, math.cos(offset * 1.1))
        row = row.resize((row.width, max(1, int(row.height * squash))), Image.LANCZOS)
        img.paste(row, (int(x), int(cy + offset * SPACING - row.height / 2)))

    return img.resize((W, H), Image.LANCZOS)


def main():
    frames, durations = [], []
    for i in range(len(BOARDS)):
        for _ in range(HOLD_FRAMES):
            frames.append(frame(i))
            durations.append(HOLD_MS)
        for s in range(1, STEPS):
            frames.append(frame(i + ease(s / STEPS)))
            durations.append(STEP_MS)

    # One palette for every frame: ramps from the background to white, the
    # accent and each logo color, which is every tone the bar uses. Shared, so
    # unchanged pixels match across frames and the encoder only stores what moved.
    ramps = list(dict.fromkeys([WHITE, INK, ACCENT, DIM] +
                               [c for *_, colors in BOARDS for c in colors]))
    steps = 256 // len(ramps)
    colors = [mix(c, 1 - k / (steps - 1)) for c in ramps for k in range(steps)]
    pal = Image.new("P", (1, 1))
    pal.putpalette([v for c in colors for v in c] + [0] * 3 * (256 - len(colors)))
    frames = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True, disposal=1)
    print(f"Wrote {OUT} ({OUT.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
