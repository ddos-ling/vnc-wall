"""RFB 帧缓冲解码:Raw / CopyRect / Hextile / ZRLE / Cursor

所有解码结果写入 Framebuffer.data —— 一个 (height, width, 4) 的 uint8 数组,
内存布局为 B,G,R,X(对应客户端协商的 32bpp little-endian 真彩色格式)

坐标越界一律裁剪,协议异常抛 ProtocolError 由上层断线重连,不做静默容错
"""

from __future__ import annotations

import zlib

import numpy as np

from .const import CPIXEL_BYTES, PIXEL_BYTES

# Hextile 每块
_HEXTILE_TILE = 16
# ZRLE 每块
_ZRLE_TILE = 64


class ProtocolError(Exception):
    """协议数据非法"""


class Framebuffer:
    """客户端侧帧缓冲"""

    __slots__ = ("data", "width", "height", "generation")

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.generation = 0
        self.data = np.zeros((height, width, 4), dtype=np.uint8)

    def resize(self, width: int, height: int) -> None:
        """调整尺寸,按规范保留左上角原有内容"""
        if width == self.width and height == self.height:
            return
        new = np.zeros((height, width, 4), dtype=np.uint8)
        cw = min(width, self.width)
        ch = min(height, self.height)
        if cw > 0 and ch > 0:
            new[:ch, :cw] = self.data[:ch, :cw]
        self.data = new
        self.width = width
        self.height = height
        self.generation += 1


class Reader:
    """带边界的只读字节游标,越界抛 ProtocolError"""

    __slots__ = ("_buf", "_pos", "_len")

    def __init__(self, buf: bytes) -> None:
        self._buf = buf
        self._pos = 0
        self._len = len(buf)

    def read(self, n: int) -> bytes:
        end = self._pos + n
        if end > self._len:
            raise ProtocolError(f"数据不足,需要 {end} 字节,实际 {self._len} 字节")
        chunk = self._buf[self._pos:end]
        self._pos = end
        return chunk

    def u8(self) -> int:
        end = self._pos + 1
        if end > self._len:
            raise ProtocolError("数据不足")
        v = self._buf[self._pos]
        self._pos = end
        return v

    def __len__(self) -> int:
        return self._len - self._pos


def _cpx(buf: bytes) -> np.ndarray:
    """把 CPIXEL 字节串转为 (n, 3) 数组,ZRLE 专用"""
    return np.frombuffer(buf, dtype=np.uint8).reshape(-1, CPIXEL_BYTES)


def _px(buf: bytes) -> np.ndarray:
    """把普通 PIXEL 字节串转为 (n, 4) 数组,Raw / Hextile / Cursor 使用"""
    return np.frombuffer(buf, dtype=np.uint8).reshape(-1, PIXEL_BYTES)


def _clip(fb: Framebuffer, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """把矩形裁剪到帧缓冲范围内,返回 (x, y, w, h),无效返回 0 尺寸"""
    if x < 0:
        w += x
        x = 0
    if y < 0:
        h += y
        y = 0
    if x + w > fb.width:
        w = fb.width - x
    if y + h > fb.height:
        h = fb.height - y
    return x, y, max(0, w), max(0, h)


# --------------------------------------------------------------------------- #
# 基础编码
# --------------------------------------------------------------------------- #

def decode_raw(stream, fb: Framebuffer, x: int, y: int, w: int, h: int) -> None:
    """Raw 编码:直接读取 w*h 个 PIXEL(4 字节,非 CPIXEL)"""
    buf = stream.read(w * h * PIXEL_BYTES)
    cx, cy, cw, ch = _clip(fb, x, y, w, h)
    if cw == 0 or ch == 0:
        return
    if (cw, ch) != (w, h):
        # 越界时按行裁剪已读数据
        src = _px(buf).reshape(h, w, PIXEL_BYTES)
        fb.data[cy:cy + ch, cx:cx + cw] = src[:ch, :cw]
        return
    fb.data[y:y + h, x:x + w] = _px(buf).reshape(h, w, PIXEL_BYTES)


def decode_copyrect(stream, fb: Framebuffer, x: int, y: int, w: int, h: int) -> None:
    """CopyRect 编码:从帧缓冲内另一位置复制"""
    buf = stream.read(4)
    sx = int.from_bytes(buf[0:2], "big")
    sy = int.from_bytes(buf[2:4], "big")
    cx, cy, cw, ch = _clip(fb, x, y, w, h)
    if cw == 0 or ch == 0:
        return
    # 源区域同样裁剪,保证来源与目标形状一致
    sx = max(0, min(sx, fb.width - 1))
    sy = max(0, min(sy, fb.height - 1))
    cw = min(cw, fb.width - sx)
    ch = min(ch, fb.height - sy)
    if cw <= 0 or ch <= 0:
        return
    src = fb.data[sy:sy + ch, sx:sx + cw].copy()
    fb.data[cy:cy + ch, cx:cx + cw] = src


# --------------------------------------------------------------------------- #
# Hextile
# --------------------------------------------------------------------------- #

def decode_hextile(stream, fb: Framebuffer, x: int, y: int, w: int, h: int) -> None:
    data = fb.data
    cx, cy, cw, ch = _clip(fb, x, y, w, h)
    if cw == 0 or ch == 0:
        # 仍需把整块数据读掉,否则流会错位
        _skip_hextile(stream, w, h)
        return
    # 越界情况极少,走通用慢路径
    if (cw, ch) != (w, h):
        tmp = Framebuffer(w, h)
        _decode_hextile_into(stream, tmp, w, h)
        data[cy:cy + ch, cx:cx + cw] = tmp.data[:ch, :cw]
        return
    _decode_hextile_into(stream, fb, x, y, w, h)


def _skip_hextile(stream, w: int, h: int) -> None:
    """只消费数据不绘制(矩形完全在帧缓冲外)"""
    _decode_hextile_into(stream, Framebuffer(w, h), 0, 0, w, h)


def _decode_hextile_into(stream, fb: Framebuffer, ox: int, oy: int, w: int, h: int) -> None:
    # Hextile 全程使用 PIXEL(4 字节),不使用 CPIXEL
    data = fb.data
    bg: bytes | None = None
    fg: bytes | None = None
    for ty in range(0, h, _HEXTILE_TILE):
        th = min(_HEXTILE_TILE, h - ty)
        for tx in range(0, w, _HEXTILE_TILE):
            tw = min(_HEXTILE_TILE, w - tx)
            sub = stream.read(1)[0]
            if sub & 1:  # Raw
                buf = stream.read(tw * th * PIXEL_BYTES)
                data[oy + ty:oy + ty + th, ox + tx:ox + tx + tw] = _px(buf).reshape(th, tw, PIXEL_BYTES)
                continue
            if sub & 2:  # BackgroundSpecified
                bg = stream.read(PIXEL_BYTES)
            if sub & 4:  # ForegroundSpecified
                fg = stream.read(PIXEL_BYTES)
            if bg is None:
                bg = b"\x00" * PIXEL_BYTES
            tile = data[oy + ty:oy + ty + th, ox + tx:ox + tx + tw]
            tile[:] = _px(bg)
            if not sub & 8:  # AnySubrects 未置位,整块纯背景
                continue
            count = stream.read(1)[0]
            if count == 0:
                continue
            colored = bool(sub & 16)
            if colored:
                raw = stream.read(count * (PIXEL_BYTES + 2))
                pos = 0
                for _ in range(count):
                    color = _px(raw[pos:pos + PIXEL_BYTES])
                    pos += PIXEL_BYTES
                    xy = raw[pos]
                    whs = raw[pos + 1]
                    pos += 2
                    sx, sy = xy >> 4, xy & 0x0F
                    sw = (whs >> 4) + 1
                    sh = (whs & 0x0F) + 1
                    if sx >= tw or sy >= th:
                        continue
                    sw = min(sw, tw - sx)
                    sh = min(sh, th - sy)
                    tile[sy:sy + sh, sx:sx + sw] = color
            else:
                if fg is None:
                    fg = bg
                color = _px(fg)
                raw = stream.read(count * 2)
                pos = 0
                for _ in range(count):
                    xy = raw[pos]
                    whs = raw[pos + 1]
                    pos += 2
                    sx, sy = xy >> 4, xy & 0x0F
                    sw = (whs >> 4) + 1
                    sh = (whs & 0x0F) + 1
                    if sx >= tw or sy >= th:
                        continue
                    sw = min(sw, tw - sx)
                    sh = min(sh, th - sy)
                    tile[sy:sy + sh, sx:sx + sw] = color


# --------------------------------------------------------------------------- #
# ZRLE
# --------------------------------------------------------------------------- #

def decode_zrle(stream, fb: Framebuffer, x: int, y: int, w: int, h: int, zdec: zlib._Decompress) -> None:
    """ZRLE:一个 4 字节长度 + zlib 压缩块,zlib 流在整个连接内复用"""
    length = int.from_bytes(stream.read(4), "big")
    if length > (1 << 28):
        raise ProtocolError(f"ZRLE 数据块长度异常: {length}")
    payload = stream.read(length)
    try:
        raw = zdec.decompress(payload)
    except zlib.error as exc:
        raise ProtocolError(f"ZRLE 解压失败: {exc}") from exc
    if not raw:
        return
    cx, cy, cw, ch = _clip(fb, x, y, w, h)
    if cw == 0 or ch == 0:
        return
    if (cw, ch) != (w, h):
        tmp = Framebuffer(w, h)
        _decode_zrle_into(Reader(raw), tmp, 0, 0, w, h)
        fb.data[cy:cy + ch, cx:cx + cw] = tmp.data[:ch, :cw]
        return
    _decode_zrle_into(Reader(raw), fb, x, y, w, h)


def _decode_zrle_into(r: Reader, fb: Framebuffer, x: int, y: int, w: int, h: int) -> None:
    for ty in range(0, h, _ZRLE_TILE):
        th = min(_ZRLE_TILE, h - ty)
        for tx in range(0, w, _ZRLE_TILE):
            tw = min(_ZRLE_TILE, w - tx)
            _zrle_tile(r, fb.data, x + tx, y + ty, tw, th)


def _zrle_tile(r: Reader, data: np.ndarray, ox: int, oy: int, tw: int, th: int) -> None:
    sub = r.u8()
    total = tw * th
    if sub == 0:  # Raw
        buf = r.read(total * CPIXEL_BYTES)
        data[oy:oy + th, ox:ox + tw, :CPIXEL_BYTES] = _cpx(buf).reshape(th, tw, CPIXEL_BYTES)
        return
    if sub == 1:  # Solid
        data[oy:oy + th, ox:ox + tw, :CPIXEL_BYTES] = _cpx(r.read(CPIXEL_BYTES))
        return
    if 2 <= sub <= 16:  # Packed palette,每行按字节对齐
        palette = _cpx(r.read(sub * CPIXEL_BYTES))
        bpp = 1 if sub == 2 else (2 if sub <= 4 else 4)
        row_bytes = (tw * bpp + 7) // 8
        packed = np.frombuffer(r.read(row_bytes * th), dtype=np.uint8).reshape(th, row_bytes)
        bits = np.unpackbits(packed, axis=1)
        if bpp == 1:
            idx = bits[:, :tw]
        elif bpp == 2:
            idx = (bits[:, 0::2][:, :tw] << 1) | bits[:, 1::2][:, :tw]
        else:
            idx = ((bits[:, 0::4][:, :tw] << 3) | (bits[:, 1::4][:, :tw] << 2)
                   | (bits[:, 2::4][:, :tw] << 1) | bits[:, 3::4][:, :tw])
        np.clip(idx, 0, len(palette) - 1, out=idx)
        data[oy:oy + th, ox:ox + tw, :CPIXEL_BYTES] = palette[idx]
        return
    if sub == 128:  # Plain RLE
        pixels = np.empty((total, CPIXEL_BYTES), dtype=np.uint8)
        i = 0
        while i < total:
            color = _cpx(r.read(CPIXEL_BYTES))
            run = 0
            while True:
                b = r.u8()
                run += b
                if b != 255:
                    break
            run += 1
            if i + run > total:
                run = total - i
            pixels[i:i + run] = color
            i += run
        data[oy:oy + th, ox:ox + tw, :CPIXEL_BYTES] = pixels.reshape(th, tw, CPIXEL_BYTES)
        return
    if 130 <= sub <= 255:  # Palette RLE
        palette = _cpx(r.read((sub - 128) * CPIXEL_BYTES))
        idx = np.empty(total, dtype=np.uint8)
        i = 0
        last = len(palette) - 1
        while i < total:
            b = r.u8()
            if b & 0x80:
                pi = min(b & 0x7F, last)
                run = 0
                while True:
                    c = r.u8()
                    run += c
                    if c != 255:
                        break
                run += 1
            else:
                pi = min(b, last)
                run = 1
            if i + run > total:
                run = total - i
            idx[i:i + run] = pi
            i += run
        data[oy:oy + th, ox:ox + tw, :CPIXEL_BYTES] = palette[idx].reshape(th, tw, CPIXEL_BYTES)
        return
    raise ProtocolError(f"未知的 ZRLE 子编码: {sub}")


# --------------------------------------------------------------------------- #
# 光标
# --------------------------------------------------------------------------- #

def decode_cursor_pixels(buf: bytes, w: int, h: int) -> np.ndarray:
    """把 Cursor 伪编码的像素 + 位图合成 RGBA 数组"""
    pixels = np.frombuffer(buf, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
    mask_row = (w + 7) // 8
    mask_raw = np.frombuffer(buf, dtype=np.uint8, offset=w * h * 4, count=mask_row * h).reshape(h, mask_row)
    mask = np.unpackbits(mask_raw, axis=1)[:, :w]
    rgba = np.empty((h, w, 4), dtype=np.uint8)
    rgba[:, :, 0] = pixels[:, :, 2]
    rgba[:, :, 1] = pixels[:, :, 1]
    rgba[:, :, 2] = pixels[:, :, 0]
    rgba[:, :, 3] = np.where(mask > 0, 255, 0)
    return rgba
