#!/usr/bin/env python3
"""Draw the home-screen icons (public/icon-*.png, apple-touch-icon.png) with the
standard library only: a dark square with a teal crescent moon and three die pips.
Full-bleed, art inside the centre ~80% so Android's circular mask and iOS's rounded
mask both keep it whole. Also writes the iPhone app's 1024 px icon.  Run:  python3 icons.py"""
import os
import struct
import zlib

BG_TOP, BG_BOT = (0x19, 0x1D, 0x31), (0x0F, 0x12, 0x20)
MOON, PIP = (0x5E, 0xE0, 0xC0), (0x7C, 0x9C, 0xFF)


def cov(d):            # signed-distance (in pixels, negative = inside) -> coverage 0..1
    return min(1.0, max(0.0, 0.5 - d))


def draw(size):
    px = 1.0 / size
    moon_c, moon_r = (0.45, 0.52), 0.30
    cut_c, cut_r = (0.60, 0.42), 0.27
    pips = [((0.70, 0.66), 0.046), ((0.80, 0.78), 0.046), ((0.58, 0.80), 0.046)]
    rows = []
    for y in range(size):
        v = (y + 0.5) * px
        bg = tuple(round(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * v) for i in range(3))
        row = bytearray()
        for x in range(size):
            u = (x + 0.5) * px
            r, g, b = bg
            dm = (((u - moon_c[0]) ** 2 + (v - moon_c[1]) ** 2) ** .5 - moon_r) / px
            dc = (((u - cut_c[0]) ** 2 + (v - cut_c[1]) ** 2) ** .5 - cut_r) / px
            a = cov(dm) * (1 - cov(dc))
            if a > 0:
                r, g, b = (round(r + (MOON[0] - r) * a), round(g + (MOON[1] - g) * a), round(b + (MOON[2] - b) * a))
            for (cx, cy), pr in pips:
                dp = (((u - cx) ** 2 + (v - cy) ** 2) ** .5 - pr) / px
                a = cov(dp)
                if a > 0:
                    r, g, b = (round(r + (PIP[0] - r) * a), round(g + (PIP[1] - g) * a), round(b + (PIP[2] - b) * a))
            row += bytes((r, g, b))
        rows.append(b"\x00" + bytes(row))
    return b"".join(rows)


def png(size, raw):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public")
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
        with open(os.path.join(here, name), "wb") as f:
            f.write(png(size, draw(size)))
        print("wrote", name)
    # the iPhone app's icon (ios/): one full-bleed 1024 px square, no alpha; iOS rounds the corners itself
    ios = os.path.join(os.path.dirname(here), "ios", "SleepBandit", "Assets.xcassets", "AppIcon.appiconset")
    if os.path.isdir(ios):
        with open(os.path.join(ios, "icon-1024.png"), "wb") as f:
            f.write(png(1024, draw(1024)))
        print("wrote ios icon-1024.png")
