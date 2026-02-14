"""
FILENAME: capture.py
PIPELINE: main.py -> execute.py -> capture.py -> execute.py -> main.py

This process returns a base64 PNG screenshot. In normal mode it captures the primary monitor via GDI.
In sandbox mode it injects a persistent black canvas and updates it from actions:
- drag(x1,y1,x2,y2) draws a white line onto the persistent canvas and saves it to disk
- the red VLM annotations are applied on a copy so the persistence is not polluted

focus(...) is ignored (no cropping).
"""
from __future__ import annotations

import ast
import base64
import ctypes
import ctypes.wintypes
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Final

type Color = tuple[int, int, int, int]
type Point = tuple[int, int]

_SRCCOPY: Final[int] = 0x00CC0020
_CAPTUREBLT: Final[int] = 0x40000000
_BI_RGB: Final[int] = 0
_DIB_RGB: Final[int] = 0
_HALFTONE: Final[int] = 4

MARK_FILL: Final[Color] = (255, 0, 0, 180)
MARK_OUTLINE: Final[Color] = (255, 255, 255, 230)
MARK_TEXT: Final[Color] = (255, 255, 255, 255)
TRAIL_COLOR: Final[Color] = (255, 0, 0, 120)

SANDBOX_WHITE: Final[Color] = (255, 255, 255, 255)
BLACK: Final[Color] = (0, 0, 0, 255)

SANDBOX_DEFAULT: Final[bool] = False
SANDBOX_RESET_DEFAULT: Final[bool] = False
SANDBOX_CANVAS: Final[Path] = Path(__file__).with_name("sandbox_canvas.bmp")

_shcore: Final = ctypes.WinDLL("shcore", use_last_error=True)
_shcore.SetProcessDpiAwareness(2)
_user32: Final = ctypes.WinDLL("user32", use_last_error=True)
_gdi32: Final = ctypes.WinDLL("gdi32", use_last_error=True)

_screen_w: Final[int] = _user32.GetSystemMetrics(0)
_screen_h: Final[int] = _user32.GetSystemMetrics(1)

class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]

class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]

def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _BI_RGB
    return bmi

def _capture_bgra(w: int, h: int) -> bytes:
    sdc = _user32.GetDC(0)
    memdc = _gdi32.CreateCompatibleDC(sdc)
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB, ctypes.byref(bits), None, 0)
    old = _gdi32.SelectObject(memdc, hbmp)
    _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)
    raw = bytes((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value))
    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(0, sdc)
    return raw

def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes:
    sdc = _user32.GetDC(0)
    src_dc = _gdi32.CreateCompatibleDC(sdc)
    dst_dc = _gdi32.CreateCompatibleDC(sdc)

    src_bmp = _gdi32.CreateCompatibleBitmap(sdc, sw, sh)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    _gdi32.SetDIBits(sdc, src_bmp, 0, sh, src, ctypes.byref(_make_bmi(sw, sh)), _DIB_RGB)

    dst_bits = ctypes.c_void_p()
    dst_bmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(dw, dh)), _DIB_RGB, ctypes.byref(dst_bits), None, 0)
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)

    _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
    _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)

    out = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits.value))

    _gdi32.SelectObject(dst_dc, old_dst)
    _gdi32.SelectObject(src_dc, old_src)
    _gdi32.DeleteObject(dst_bmp)
    _gdi32.DeleteObject(src_bmp)
    _gdi32.DeleteDC(dst_dc)
    _gdi32.DeleteDC(src_dc)
    _user32.ReleaseDC(0, sdc)
    return out

def _bgra_to_rgba(bgra: bytes) -> bytearray:
    n = len(bgra)
    out = bytearray(n)
    out[0::4] = bgra[2::4]
    out[1::4] = bgra[1::4]
    out[2::4] = bgra[0::4]
    out[3::4] = b"\xff" * (n // 4)
    return out

def _rgba_to_bgra(rgba: bytes) -> bytes:
    n = len(rgba)
    out = bytearray(n)
    out[0::4] = rgba[2::4]
    out[1::4] = rgba[1::4]
    out[2::4] = rgba[0::4]
    out[3::4] = b"\xff" * (n // 4)
    return bytes(out)

def _encode_png(rgba: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)

    def _chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")

class Canvas:
    __slots__ = ("buf", "w", "h")

    def __init__(self, buf: bytearray, w: int, h: int) -> None:
        self.buf = buf
        self.w = w
        self.h = h

    def put(self, x: int, y: int, c: Color) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        i = (y * self.w + x) << 2
        sa = c[3]
        if sa >= 255:
            self.buf[i] = c[0]
            self.buf[i + 1] = c[1]
            self.buf[i + 2] = c[2]
            self.buf[i + 3] = 255
            return
        da = 255 - sa
        self.buf[i] = (c[0] * sa + self.buf[i] * da) // 255
        self.buf[i + 1] = (c[1] * sa + self.buf[i + 1] * da) // 255
        self.buf[i + 2] = (c[2] * sa + self.buf[i + 2] * da) // 255
        self.buf[i + 3] = 255

    def put_opaque(self, x: int, y: int, c: Color) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        i = (y * self.w + x) << 2
        self.buf[i] = c[0]
        self.buf[i + 1] = c[1]
        self.buf[i + 2] = c[2]
        self.buf[i + 3] = 255

    def put_thick_opaque(self, x: int, y: int, c: Color, t: int) -> None:
        half = t >> 1
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                self.put_opaque(x + dx, y + dy, c)

    def put_thick(self, x: int, y: int, c: Color, t: int) -> None:
        half = t >> 1
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                self.put(x + dx, y + dy, c)

    def line(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy
        x, y = x1, y1
        while True:
            self.put_thick(x, y, c, t)
            if x == x2 and y == y2:
                break
            e2 = err << 1
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy

    def line_opaque(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx - dy
        x, y = x1, y1
        while True:
            self.put_thick_opaque(x, y, c, t)
            if x == x2 and y == y2:
                break
            e2 = err << 1
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy

    def circle(self, cx: int, cy: int, r: int, c: Color, filled: bool, thickness: int) -> None:
        r2o = r * r
        r2i = max(0, (r - thickness)) ** 2
        for oy in range(-r, r + 1):
            for ox in range(-r, r + 1):
                d2 = ox * ox + oy * oy
                if filled:
                    if d2 <= r2o:
                        self.put(cx + ox, cy + oy, c)
                else:
                    if r2i <= d2 <= r2o:
                        self.put(cx + ox, cy + oy, c)

    def rect(self, x: int, y: int, w: int, h: int, c: Color, t: int) -> None:
        self.line(x, y, x + w, y, c, t)
        self.line(x + w, y, x + w, y + h, c, t)
        self.line(x + w, y + h, x, y + h, c, t)
        self.line(x, y + h, x, y, c, t)

    def fill_polygon(self, pts: list[Point], c: Color) -> None:
        if len(pts) < 3:
            return
        ys = [p[1] for p in pts]
        lo = 0 if min(ys) < 0 else min(ys)
        hi = (self.h - 1) if max(ys) >= self.h else max(ys)
        n = len(pts)
        for y in range(lo, hi + 1):
            nodes: list[int] = []
            j = n - 1
            for i in range(n):
                yi, yj = pts[i][1], pts[j][1]
                if (yi < y <= yj) or (yj < y <= yi):
                    nodes.append(int(pts[i][0] + (y - yi) / (yj - yi) * (pts[j][0] - pts[i][0])))
                j = i
            nodes.sort()
            for k in range(0, len(nodes) - 1, 2):
                x0 = 0 if nodes[k] < 0 else nodes[k]
                x1 = self.w - 1 if nodes[k + 1] >= self.w else nodes[k + 1]
                for x in range(x0, x1 + 1):
                    self.put(x, y, c)

    def arrow(self, x1: int, y1: int, x2: int, y2: int, c: Color, t: int) -> None:
        self.line(x1, y1, x2, y2, c, t)
        ang = math.atan2(y2 - y1, x2 - x1)
        ha = math.radians(25.0)
        ln = 28.0
        lx = int(x2 - ln * math.cos(ang - ha))
        ly = int(y2 - ln * math.sin(ang - ha))
        rx = int(x2 - ln * math.cos(ang + ha))
        ry = int(y2 - ln * math.sin(ang + ha))
        self.fill_polygon([(x2, y2), (lx, ly), (rx, ry)], c)

_DIGITS: Final[list[list[int]]] = [
    [0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110],
    [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    [0b01110, 0b10001, 0b00001, 0b00110, 0b01000, 0b10000, 0b11111],
    [0b01110, 0b10001, 0b00001, 0b00110, 0b00001, 0b10001, 0b01110],
    [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
    [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
    [0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110],
    [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
    [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
    [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100],
]

def _render_digit(cv: Canvas, cx: int, cy: int, d: int, fill: Color, outline: Color, scale: int) -> None:
    gw = 5 * scale
    gh = 7 * scale
    ox = cx - gw // 2
    oy = cy - gh // 2
    g = _DIGITS[d]
    for ddy in (-1, 0, 1):
        for ddx in (-1, 0, 1):
            if ddx == 0 and ddy == 0:
                continue
            for ri, row in enumerate(g):
                for ci in range(5):
                    if row & (1 << (4 - ci)):
                        for sy in range(scale):
                            for sx in range(scale):
                                cv.put_opaque(ox + ci * scale + sx + ddx * 2, oy + ri * scale + sy + ddy * 2, outline)
    for ri, row in enumerate(g):
        for ci in range(5):
            if row & (1 << (4 - ci)):
                for sy in range(scale):
                    for sx in range(scale):
                        cv.put_opaque(ox + ci * scale + sx, oy + ri * scale + sy, fill)

def _render_number(cv: Canvas, cx: int, cy: int, n: int, fill: Color, outline: Color, scale: int) -> None:
    s = str(n)
    gw = 5 * scale
    gap = 1 * scale
    tw = len(s) * gw + (len(s) - 1) * gap
    start = cx - tw // 2 + gw // 2
    for i, ch in enumerate(s):
        _render_digit(cv, start + i * (gw + gap), cy, int(ch), fill, outline, scale)

def _parse_action(line: str) -> tuple[str, list[object]] | None:
    p = line.find("(")
    if p == -1:
        return None
    name = line[:p].strip()
    try:
        args = list(ast.literal_eval(f"({line[p + 1 : line.rfind(')')]},)"))
    except (ValueError, SyntaxError):
        return None
    return name, args

def _norm(v: int, extent: int) -> int:
    v = 0 if v < 0 else 1000 if v > 1000 else v
    return int((v / 1000.0) * extent)

def _bmp_write_black(path: Path, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    size_image = stride * h
    file_size = 54 + size_image
    fh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    ih = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, size_image, 2835, 2835, 0, 0)
    pad = b"\x00" * (stride - w * 3)
    row = b"\x00" * (w * 3) + pad
    data = fh + ih + row * h
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

def _bmp_load_rgba(path: Path, w: int, h: int) -> bytearray:
    try:
        data = path.read_bytes()
        if len(data) < 54 or data[0:2] != b"BM":
            return bytearray()
        off = struct.unpack_from("<I", data, 10)[0]
        hs = struct.unpack_from("<I", data, 14)[0]
        if hs < 40:
            return bytearray()
        bw, bh = struct.unpack_from("<ii", data, 18)
        planes, bpp = struct.unpack_from("<HH", data, 26)
        comp = struct.unpack_from("<I", data, 30)[0]
        if planes != 1 or comp != 0 or bpp not in (24, 32):
            return bytearray()
        ah = -bh if bh < 0 else bh
        if bw != w or ah != h:
            return bytearray()
        bytespp = bpp // 8
        stride = ((w * bytespp + 3) // 4) * 4
        need = off + stride * h
        if len(data) < need:
            return bytearray()
        out = bytearray(w * h * 4)
        top_down = bh < 0
        for y in range(h):
            sy = y if top_down else (h - 1 - y)
            row = data[off + sy * stride : off + (sy + 1) * stride]
            di = y * w * 4
            if bpp == 24:
                for x in range(w):
                    i = x * 3
                    out[di + (x * 4)] = row[i + 2]
                    out[di + (x * 4) + 1] = row[i + 1]
                    out[di + (x * 4) + 2] = row[i]
                    out[di + (x * 4) + 3] = 255
            else:
                for x in range(w):
                    i = x * 4
                    out[di + (x * 4)] = row[i + 2]
                    out[di + (x * 4) + 1] = row[i + 1]
                    out[di + (x * 4) + 2] = row[i]
                    out[di + (x * 4) + 3] = 255
        return out
    except Exception:
        return bytearray()

def _bmp_save_rgba(path: Path, buf: bytes, w: int, h: int) -> None:
    stride = ((w * 3 + 3) // 4) * 4
    size_image = stride * h
    file_size = 54 + size_image
    fh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    ih = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, size_image, 2835, 2835, 0, 0)
    pad = b"\x00" * (stride - w * 3)
    out = bytearray()
    out.extend(fh)
    out.extend(ih)
    for y in range(h - 1, -1, -1):
        row = buf[y * w * 4 : (y + 1) * w * 4]
        for x in range(w):
            i = x * 4
            out.append(row[i + 2])
            out.append(row[i + 1])
            out.append(row[i])
        out.extend(pad)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(bytes(out))
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

def _sandbox_load(w: int, h: int, reset: bool) -> bytearray:
    if reset:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
    if not SANDBOX_CANVAS.is_file():
        _bmp_write_black(SANDBOX_CANVAS, w, h)
    buf = _bmp_load_rgba(SANDBOX_CANVAS, w, h)
    if not buf:
        _bmp_write_black(SANDBOX_CANVAS, w, h)
        return bytearray(b"\x00\x00\x00\xff" * (w * h))
    return buf

def _sandbox_save(buf: bytearray, w: int, h: int) -> None:
    _bmp_save_rgba(SANDBOX_CANVAS, bytes(buf), w, h)

def _sandbox_apply(buf: bytearray, w: int, h: int, actions: list[str]) -> bool:
    cv = Canvas(buf, w, h)
    dirty = False
    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args = parsed
        if name != "drag" or len(args) < 4:
            continue
        x1 = _norm(int(args[0]), w)
        y1 = _norm(int(args[1]), h)
        x2 = _norm(int(args[2]), w)
        y2 = _norm(int(args[3]), h)
        cv.line_opaque(x1, y1, x2, y2, SANDBOX_WHITE, 4)
        dirty = True
    return dirty

def _apply_marks(buf: bytearray, w: int, h: int, actions: list[str]) -> None:
    cv = Canvas(buf, w, h)
    px: int | None = None
    py: int | None = None
    n = 1
    for line in actions:
        parsed = _parse_action(line)
        if parsed is None:
            continue
        name, args = parsed
        match name:
            case "left_click" if len(args) >= 2:
                x, y = _norm(int(args[0]), w), _norm(int(args[1]), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "right_click" if len(args) >= 2:
                x, y = _norm(int(args[0]), w), _norm(int(args[1]), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                cv.rect(x + 20, y - 36, 16, 16, MARK_TEXT, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "double_left_click" if len(args) >= 2:
                x, y = _norm(int(args[0]), w), _norm(int(args[1]), h)
                if px is not None and py is not None and (abs(x - px) + abs(y - py) > 30):
                    cv.line(px, py, x, y, TRAIL_COLOR, 4)
                cv.circle(x, y, 32, MARK_OUTLINE, True, 3)
                cv.circle(x, y, 28, MARK_FILL, True, 3)
                cv.circle(x, y, 42, MARK_OUTLINE, False, 3)
                _render_number(cv, x, y, n, MARK_TEXT, BLACK, 4)
                px, py = x, y
                n += 1
            case "drag" if len(args) >= 4:
                x1, y1 = _norm(int(args[0]), w), _norm(int(args[1]), h)
                x2, y2 = _norm(int(args[2]), w), _norm(int(args[3]), h)
                if px is not None and py is not None and (abs(x1 - px) + abs(y1 - py) > 30):
                    cv.line(px, py, x1, y1, TRAIL_COLOR, 4)
                cv.circle(x1, y1, 20, MARK_OUTLINE, True, 3)
                cv.circle(x1, y1, 16, MARK_FILL, True, 3)
                _render_number(cv, x1, y1, n, MARK_TEXT, BLACK, 3)
                cv.arrow(x1, y1, x2, y2, MARK_FILL, 6)
                cv.circle(x2, y2, 20, MARK_OUTLINE, False, 4)
                cv.circle(x2, y2, 16, MARK_FILL, False, 3)
                px, py = x2, y2
                n += 1
            case "type":
                if px is None or py is None:
                    continue
                pad = 30
                cv.rect(px - pad, py - pad // 2, pad * 2, pad, MARK_FILL, 4)
                cv.rect(px - pad - 2, py - pad // 2 - 2, pad * 2 + 4, pad + 4, MARK_OUTLINE, 2)
                _render_number(cv, px, py, n, MARK_TEXT, BLACK, 3)
                n += 1
            case _:
                continue

def capture(actions: list[str], width: int, height: int, marks: bool, sandbox: bool, sandbox_reset: bool) -> str:
    sw, sh = _screen_w, _screen_h
    if sandbox:
        base = _sandbox_load(sw, sh, sandbox_reset)
        dirty = _sandbox_apply(base, sw, sh, actions)
        if dirty:
            _sandbox_save(base, sw, sh)
        rgba = bytearray(base)
    else:
        rgba = _bgra_to_rgba(_capture_bgra(sw, sh))

    if marks and actions:
        _apply_marks(rgba, sw, sh, actions)

    dw = sw if width <= 0 else width
    dh = sh if height <= 0 else height
    if (dw, dh) != (sw, sh):
        bgra = _rgba_to_bgra(bytes(rgba))
        bgra2 = _resize_bgra(bgra, sw, sh, dw, dh)
        rgba = _bgra_to_rgba(bgra2)
        sw, sh = dw, dh

    png = _encode_png(bytes(rgba), sw, sh)
    return base64.b64encode(png).decode("ascii")

def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    actions = req.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    actions = [a for a in actions if isinstance(a, str) and not a.lstrip().startswith("focus(")]
    w = int(req.get("width", 0))
    h = int(req.get("height", 0))
    marks = bool(req.get("marks", True))
    sandbox = bool(req.get("sandbox", SANDBOX_DEFAULT))
    sandbox_reset = bool(req.get("sandbox_reset", SANDBOX_RESET_DEFAULT))
    sys.stdout.write(capture(actions, w, h, marks, sandbox, sandbox_reset))
    sys.stdout.flush()

if __name__ == "__main__":
    main()
