#!/usr/bin/env python3
"""Rasterise the paper-plane mark to PNG. Run once; the output is committed.

iOS ignores SVG for `apple-touch-icon` and substitutes a screenshot of the page,
which looks broken on a home screen — so the PNGs exist purely for that. Written
with zlib + struct rather than Pillow to keep this repo dependency-free (it has to
run on the Pi from cron alongside nothing else).

    ./dev/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BG = (0x17, 0x16, 0x12)
PLANE = (0xD8, 0xA9, 0x4F)
WING = (0xA8, 0x80, 0x2F)

# Same geometry as icon.svg, in a 64x64 space.
BODY = [(9, 37.5), (55, 15), (45.5, 40.5), (33.5, 34.5), (23, 43), (22.5, 34.2)]
FOLD = [(22.5, 34.2), (55, 15), (33.5, 34.5)]
RADIUS = 14.0  # rounded-square corner radius, matching the SVG


def inside(poly, x: float, y: float) -> bool:
    """Even-odd ray cast."""
    hit = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0)
            if x < x0 + t * (x1 - x0):
                hit = not hit
    return hit


def in_rounded_square(x: float, y: float, side: float, r: float) -> bool:
    cx = min(max(x, r), side - r)
    cy = min(max(y, r), side - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r or (r <= x <= side - r) or (r <= y <= side - r)


def render(size: int, rounded: bool, samples: int = 3) -> bytes:
    """Rows of RGB bytes, supersampled `samples`x per axis."""
    scale = 64.0 / size
    r = RADIUS / scale
    rows = []
    step = 1.0 / (samples + 1)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0]
            hits = 0
            for sy in range(1, samples + 1):
                for sx in range(1, samples + 1):
                    fx, fy = px + sx * step, py + sy * step
                    if rounded and not in_rounded_square(fx, fy, float(size), r):
                        continue  # transparent-equivalent: leave it unaccumulated
                    ux, uy = fx * scale, fy * scale
                    if inside(FOLD, ux, uy):
                        col = WING
                    elif inside(BODY, ux, uy):
                        col = PLANE
                    else:
                        col = BG
                    acc[0] += col[0]
                    acc[1] += col[1]
                    acc[2] += col[2]
                    hits += 1
            if hits == 0:
                row += bytes(BG)  # outside the rounded corner
            else:
                row += bytes(c // hits for c in acc)
        rows.append(bytes(row))
    return b"".join(b"\x00" + r for r in rows)


def write_png(path: Path, size: int, rounded: bool) -> None:
    raw = render(size, rounded)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    print(f"wrote {path.name} ({size}x{size}, {len(png):,} bytes)")


if __name__ == "__main__":
    # 180: what iOS asks for. 512: Android / manifest fallback.
    write_png(ROOT / "apple-touch-icon.png", 180, rounded=True)
    write_png(ROOT / "icon-512.png", 512, rounded=False)
