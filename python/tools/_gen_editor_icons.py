"""Generate missing editor UI icons into python/Infernux/resources/icons/.

Pure stdlib PNG writer (no Pillow). Style: light glyphs on transparent 64x64.
"""
from __future__ import annotations

import math
import os
import struct
import zlib

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Infernux",
    "resources",
    "icons",
)

SIZE = 64
FG = (220, 220, 222, 255)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path: str, pixels: list[list[tuple[int, int, int, int]]]) -> None:
    h = len(pixels)
    w = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    data = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
            _png_chunk(b"IEND", b""),
        ]
    )
    with open(path, "wb") as f:
        f.write(data)
    print(f"wrote {path}")


def blank() -> list[list[tuple[int, int, int, int]]]:
    return [[(0, 0, 0, 0) for _ in range(SIZE)] for _ in range(SIZE)]


def set_px(px, x: int, y: int, color=FG) -> None:
    if 0 <= x < SIZE and 0 <= y < SIZE:
        px[y][x] = color


def blend(px, x: int, y: int, color=FG, alpha: float = 1.0) -> None:
    if not (0 <= x < SIZE and 0 <= y < SIZE):
        return
    a = max(0.0, min(1.0, alpha))
    sr, sg, sb, sa = color
    dr, dg, db, da = px[y][x]
    out_a = a * (sa / 255.0) + (da / 255.0) * (1.0 - a * (sa / 255.0))
    if out_a <= 1e-6:
        px[y][x] = (0, 0, 0, 0)
        return
    out_r = (sr * a * (sa / 255.0) + dr * (da / 255.0) * (1.0 - a * (sa / 255.0))) / out_a
    out_g = (sg * a * (sa / 255.0) + dg * (da / 255.0) * (1.0 - a * (sa / 255.0))) / out_a
    out_b = (sb * a * (sa / 255.0) + db * (da / 255.0) * (1.0 - a * (sa / 255.0))) / out_a
    px[y][x] = (int(out_r), int(out_g), int(out_b), int(out_a * 255))


def draw_line(px, x0, y0, x1, y1, width: float = 5.0, color=FG) -> None:
    x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    steps = max(1, int(length * 2))
    half = width * 0.5
    for i in range(steps + 1):
        t = i / steps
        cx = x0 + dx * t
        cy = y0 + dy * t
        for oy in range(-int(half) - 1, int(half) + 2):
            for ox in range(-int(half) - 1, int(half) + 2):
                if ox * ox + oy * oy <= half * half:
                    set_px(px, int(round(cx + ox)), int(round(cy + oy)), color)


def draw_circle(px, cx, cy, r, width: float = 4.0, fill: bool = False, color=FG) -> None:
    for y in range(SIZE):
        for x in range(SIZE):
            d = math.hypot(x - cx, y - cy)
            if fill:
                if d <= r:
                    set_px(px, x, y, color)
            else:
                if abs(d - r) <= width * 0.5:
                    set_px(px, x, y, color)


def fill_poly(px, points, color=FG) -> None:
    if len(points) < 3:
        return
    min_y = max(0, int(min(p[1] for p in points)))
    max_y = min(SIZE - 1, int(max(p[1] for p in points)))
    for y in range(min_y, max_y + 1):
        xs = []
        for i in range(len(points)):
            x0, y0 = points[i]
            x1, y1 = points[(i + 1) % len(points)]
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0) if y1 != y0 else 0.0
                xs.append(x0 + (x1 - x0) * t)
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            x_start = max(0, int(math.floor(xs[i])))
            x_end = min(SIZE - 1, int(math.ceil(xs[i + 1])))
            for x in range(x_start, x_end + 1):
                set_px(px, x, y, color)


def save(name: str, px) -> None:
    write_png(os.path.join(OUT_DIR, f"{name}.png"), px)


def icon_plus() -> None:
    px = blank()
    c = SIZE // 2
    draw_line(px, c - 18, c, c + 18, c, 6)
    draw_line(px, c, c - 18, c, c + 18, 6)
    save("plus", px)


def icon_minus() -> None:
    px = blank()
    c = SIZE // 2
    draw_line(px, c - 16, c, c + 16, c, 6)
    save("minus", px)


def icon_remove() -> None:
    px = blank()
    c = SIZE // 2
    draw_line(px, c - 14, c - 14, c + 14, c + 14, 6)
    draw_line(px, c - 14, c + 14, c + 14, c - 14, 6)
    save("remove", px)


def icon_picker() -> None:
    px = blank()
    c = SIZE // 2
    draw_circle(px, c, c, 18, width=5)
    draw_circle(px, c, c, 4, fill=True)
    save("picker", px)


def icon_warning() -> None:
    px = blank()
    pts = [(32, 10), (54, 52), (10, 52)]
    # thick outline via edge lines
    draw_line(px, *pts[0], *pts[1], 5)
    draw_line(px, *pts[1], *pts[2], 5)
    draw_line(px, *pts[2], *pts[0], 5)
    draw_line(px, 32, 22, 32, 38, 4)
    draw_circle(px, 32, 46, 2, fill=True)
    save("warning", px)


def icon_error() -> None:
    px = blank()
    c = SIZE // 2
    draw_circle(px, c, c, 20, width=5)
    draw_line(px, c - 10, c - 10, c + 10, c + 10, 5)
    draw_line(px, c - 10, c + 10, c + 10, c - 10, 5)
    save("error", px)


def icon_ui_text() -> None:
    px = blank()
    draw_line(px, 16, 16, 48, 16, 6)
    draw_line(px, 32, 16, 32, 50, 6)
    save("ui_text", px)


def icon_ui_image() -> None:
    px = blank()
    # frame
    draw_line(px, 12, 14, 52, 14, 4)
    draw_line(px, 52, 14, 52, 50, 4)
    draw_line(px, 52, 50, 12, 50, 4)
    draw_line(px, 12, 50, 12, 14, 4)
    draw_line(px, 18, 40, 28, 28, 3)
    draw_line(px, 28, 28, 36, 36, 3)
    draw_line(px, 36, 36, 44, 24, 3)
    draw_line(px, 44, 24, 50, 40, 3)
    draw_circle(px, 22, 22, 3, fill=True)
    save("ui_image", px)


def icon_ui_button() -> None:
    px = blank()
    draw_line(px, 10, 20, 54, 20, 4)
    draw_line(px, 54, 20, 54, 44, 4)
    draw_line(px, 54, 44, 10, 44, 4)
    draw_line(px, 10, 44, 10, 20, 4)
    draw_line(px, 20, 32, 44, 32, 4)
    save("ui_button", px)


def icon_ui_canvas() -> None:
    px = blank()
    draw_line(px, 12, 12, 52, 12, 4)
    draw_line(px, 52, 12, 52, 52, 4)
    draw_line(px, 52, 52, 12, 52, 4)
    draw_line(px, 12, 52, 12, 12, 4)
    draw_line(px, 20, 20, 44, 20, 3)
    draw_line(px, 44, 20, 44, 44, 3)
    draw_line(px, 44, 44, 20, 44, 3)
    draw_line(px, 20, 44, 20, 20, 3)
    save("ui_canvas", px)


def icon_tool_none() -> None:
    px = blank()
    pts = [(18, 12), (18, 48), (28, 38), (36, 54), (42, 51), (34, 35), (48, 35)]
    fill_poly(px, pts)
    save("tool_none", px)


def icon_tool_move() -> None:
    px = blank()
    c = SIZE // 2
    draw_line(px, c, 12, c, 52, 5)
    draw_line(px, 12, c, 52, c, 5)
    fill_poly(px, [(c, 8), (c - 7, 18), (c + 7, 18)])
    fill_poly(px, [(c, 56), (c - 7, 46), (c + 7, 46)])
    fill_poly(px, [(8, c), (18, c - 7), (18, c + 7)])
    fill_poly(px, [(56, c), (46, c - 7), (46, c + 7)])
    save("tool_move", px)


def icon_tool_rotate() -> None:
    px = blank()
    c = SIZE // 2
    r = 18
    pts = []
    for deg in range(-40, 220, 3):
        rad = math.radians(deg)
        pts.append((c + r * math.cos(rad), c + r * math.sin(rad)))
    for i in range(len(pts) - 1):
        draw_line(px, *pts[i], *pts[i + 1], 5)
    end = pts[-1]
    prev = pts[-5]
    dx, dy = end[0] - prev[0], end[1] - prev[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    px_, py_ = -uy, ux
    tip = (end[0] + ux * 2, end[1] + uy * 2)
    left = (end[0] - ux * 8 + px_ * 7, end[1] - uy * 8 + py_ * 7)
    right = (end[0] - ux * 8 - px_ * 7, end[1] - uy * 8 - py_ * 7)
    fill_poly(px, [tip, left, right])
    save("tool_rotate", px)


def icon_tool_scale() -> None:
    px = blank()
    for x, y in ((14, 14), (50, 14), (14, 50), (50, 50)):
        for yy in range(y - 4, y + 5):
            for xx in range(x - 4, x + 5):
                set_px(px, xx, yy)
    draw_line(px, 20, 20, 44, 44, 4)
    draw_line(px, 28, 28, 36, 28, 3)
    draw_line(px, 36, 28, 36, 36, 3)
    draw_line(px, 36, 36, 28, 36, 3)
    draw_line(px, 28, 36, 28, 28, 3)
    save("tool_scale", px)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    icon_plus()
    icon_minus()
    icon_remove()
    icon_picker()
    icon_warning()
    icon_error()
    icon_ui_text()
    icon_ui_image()
    icon_ui_button()
    icon_ui_canvas()
    icon_tool_none()
    icon_tool_move()
    icon_tool_rotate()
    icon_tool_scale()
    print("done")


if __name__ == "__main__":
    main()
