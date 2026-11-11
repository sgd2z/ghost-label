"""Draws resources/icon.png (512x512): a friendly ghost on an indigo-to-violet rounded square.
Original artwork, generated with the standard library only (4x supersampled)."""
import math, os, struct, zlib

N, S = 512, 4

def bg(x, y, r=112):
    cx = min(max(x, r), N - r); cy = min(max(y, r), N - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

def ghost(x, y):
    cx, top, r = 256, 236, 118
    if (x - cx) ** 2 + (y - top) ** 2 <= r * r:
        body = True
    elif cx - r <= x <= cx + r and top <= y:
        # wavy hem: three scallops
        hem = 386 + 20 * math.cos((x - (cx - r)) / (2 * r) * 3 * 2 * math.pi)
        body = y <= hem
    else:
        body = False
    if not body:
        return None
    for ex in (214, 298):
        if ((x - ex) / 17) ** 2 + ((y - 230) / 24) ** 2 <= 1:
            return "eye"
    return "body"

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

TOP, BOTTOM = (29, 27, 58), (109, 74, 255)
rows = []
for y in range(N):
    row = bytearray([0])
    for x in range(N):
        acc = [0, 0, 0, 0]
        for sy in range(S):
            for sx in range(S):
                px, py = x + (sx + 0.5) / S, y + (sy + 0.5) / S
                if not bg(px, py):
                    continue
                g = ghost(px, py)
                c = (248, 247, 255) if g == "body" else (29, 27, 58) if g == "eye" else lerp(TOP, BOTTOM, (px + py * 1.4) / (N * 2.4))
                acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
        n = S * S
        a = acc[3] // n
        row += bytes([acc[0] // max(1, acc[3] // 255), acc[1] // max(1, acc[3] // 255), acc[2] // max(1, acc[3] // 255), a]) if a else bytes(4)
    rows.append(bytes(row))

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "icon.png")
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
open(out, "wb").write(png)
print("wrote", os.path.normpath(out))
