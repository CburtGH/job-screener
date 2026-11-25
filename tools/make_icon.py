"""Generates the app icons (a briefcase on a rounded square). No dependencies.
    static/icon.png / .ico           blue, production
    static/icon-staging.png / .ico   orange, staging
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static"
N, SS = 256, 4  # size, supersampling factor

WHITE, CLEAR = (255, 255, 255), None
BLUE = (47, 111, 222)  # replaced per variant below


def inside_round_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def color_at(x, y):
    if not inside_round_rect(x, y, 8, 8, 248, 248, 52):
        return CLEAR
    # briefcase body
    if inside_round_rect(x, y, 52, 92, 204, 200, 16):
        if 140 <= y <= 150:  # lid seam
            return BLUE
        return WHITE
    # handle (ring above the body)
    if inside_round_rect(x, y, 96, 58, 160, 100, 14) and not inside_round_rect(x, y, 110, 72, 146, 100, 6):
        return WHITE
    return BLUE


def render():
    rows = []
    for py in range(N):
        row = bytearray([0])
        for px in range(N):
            acc = [0, 0, 0, 0]
            for sy in range(SS):
                for sx in range(SS):
                    c = color_at(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS)
                    if c:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            n = SS * SS
            a = acc[3] // n
            k = max(acc[3] // 255, 1)
            row += bytes([acc[0] // k, acc[1] // k, acc[2] // k, a]) if a else b"\0\0\0\0"
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


for name, color in (("icon", (47, 111, 222)), ("icon-staging", (217, 115, 13))):
    BLUE = color
    png = render()
    (OUT / f"{name}.png").write_bytes(png)
    # ICO with one embedded 256x256 PNG (supported since Windows Vista).
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png
    (OUT / f"{name}.ico").write_bytes(ico)
    print("wrote", OUT / f"{name}.png", OUT / f"{name}.ico")
