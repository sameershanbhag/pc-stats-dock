#!/usr/bin/env python3
"""Draws the app icon (a dark rounded tile with a gauge) as a 512 px PNG, with no libraries.

  python3 make-icon.py out.png [--accent 224,140,76]
"""
import argparse
import math
import struct
import zlib

N = 512


def pixel(x, y, accent):
    r, cx, cy = 96, N / 2, N / 2
    dx, dy = max(abs(x - cx) - (N / 2 - r), 0), max(abs(y - cy) - (N / 2 - r), 0)
    if math.hypot(dx, dy) > r:
        return (0, 0, 0, 0)                                   # outside the rounded square
    col = (18, 25, 23)
    d = math.hypot(x - cx, y - cy + 12)
    a = (math.degrees(math.atan2(y - cy + 12, x - cx)) + 360) % 360
    on_arc = 143 <= d <= 177 and (a >= 135 or a <= 45)        # gauge ring through the top
    if on_arc:
        col = accent if (a >= 135 or a <= 10) else (51, 65, 61)
    nx, ny = math.cos(math.radians(10)), math.sin(math.radians(10))   # needle towards 10°
    t = (x - cx) * nx + (y - cy + 12) * ny
    perp = abs(-(x - cx) * ny + (y - cy + 12) * nx)
    if 0 <= t <= 120 and perp <= 9 - 6 * t / 120:
        col = (230, 235, 232)
    if d <= 20:
        col = (230, 235, 232)
    if d <= 11:
        col = (18, 25, 23)
    return (*col, 255)


def png(accent):
    rows = []
    for y in range(N):
        row = bytearray([0])
        for x in range(N):
            row += bytes(pixel(x + .5, y + .5, accent))
        rows.append(bytes(row))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--accent", default="88,170,220", help="ring colour r,g,b")
    a = ap.parse_args()
    accent = tuple(int(v) for v in a.accent.split(","))
    with open(a.out, "wb") as f:
        f.write(png(accent))
