"""Generated placeholder artwork (PNG) for when no image endpoint is available. Purely
mechanical: colors and shapes come from a hash of the title, so each release looks distinct and
stays the same across runs. Stdlib only (zlib PNG writer)."""

from __future__ import annotations

import colorsys
import hashlib
import math
import struct
import zlib


def _png(width: int, height: int, rows) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def artwork(text: str, size: int = 512, style: str = "cover") -> bytes:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    h1, h2 = digest[0] / 255, digest[1] / 255
    c1 = colorsys.hsv_to_rgb(h1, 0.55 + digest[2] / 255 * 0.4, 0.35 + digest[3] / 255 * 0.5)
    c2 = colorsys.hsv_to_rgb(h2, 0.5 + digest[4] / 255 * 0.5, 0.25 + digest[5] / 255 * 0.6)
    accent = colorsys.hsv_to_rgb((h1 + 0.5) % 1, 0.7, 0.95)
    angle = digest[6] / 255 * math.tau
    ca, sa = math.cos(angle), math.sin(angle)
    circles = [((digest[7 + i * 3] / 255) * size, (digest[8 + i * 3] / 255) * size,
                (0.08 + digest[9 + i * 3] / 255 * 0.3) * size) for i in range(3 if style == "cover" else 1)]
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            t = ((x - size / 2) * ca + (y - size / 2) * sa) / size + 0.5
            t = min(max(t, 0.0), 1.0)
            r, g, b = (c1[i] * (1 - t) + c2[i] * t for i in range(3))
            for cx, cy, rad in circles:
                d = math.hypot(x - cx, y - cy)
                if d < rad:
                    a = 0.55 if style == "cover" else 0.35
                    edge = min(1.0, (rad - d) / 2.0)
                    r, g, b = (v * (1 - a * edge) + accent[i] * a * edge for i, v in enumerate((r, g, b)))
            grain = (digest[(x * 7 + y * 13) % 32] / 255 - 0.5) * 0.03
            row += bytes(int(max(0, min(1, v + grain)) * 255) for v in (r, g, b))
        rows.append(row)
    return _png(size, size, rows)
