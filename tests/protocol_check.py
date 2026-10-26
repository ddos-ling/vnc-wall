"""协议层自检

两部分:
1. 端到端:内置最小 RFB 服务端,验证握手、认证、Raw/Hextile/CopyRect 解码与键鼠上行
2. 单测:直接调用 ZRLE 解码器(该编码默认不向服务端声明,保留实现以便后续启用)

运行: python tests/protocol_check.py
"""

from __future__ import annotations

import io
import socket
import sys
import threading
import time
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rfb import codec, const
from app.rfb.client import ClientState, VncClient
from app.rfb.des import vnc_response

W, H = 640, 480
PASSWORD = "admin123"

RED = (0x00, 0x00, 0xFF)      # 内存序 B,G,R
GREEN = (0x00, 0xFF, 0x00)
BLUE = (0xFF, 0x00, 0x00)
WHITE = (0xFF, 0xFF, 0xFF)


def px4(color) -> bytes:
    """普通像素:4 字节"""
    return bytes(color) + b"\x00"


def px3(color) -> bytes:
    """CPIXEL:3 字节"""
    return bytes(color)


class FakeServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.events: list[tuple] = []
        self.error: str | None = None
        self.sent = False
        self._conn: socket.socket | None = None

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("客户端断开")
            buf += chunk
        return buf

    def _send(self, data: bytes) -> None:
        self._conn.sendall(data)

    # -- 编码 -------------------------------------------------------- #
    def _hextile_solid(self, w: int, h: int, color) -> bytes:
        out = bytearray()
        for _ty in range(0, h, 16):
            for _tx in range(0, w, 16):
                out.append(0x02)
                out += px4(color)
        return bytes(out)

    def _hextile_subrects(self, w: int, h: int, bg, fg) -> bytes:
        """每个 16x16 tile 画一个左上 4x4 的前景方块"""
        out = bytearray()
        for _ty in range(0, h, 16):
            for _tx in range(0, w, 16):
                out.append(0x02 | 0x08 | 0x10)  # bg + subrects + colored
                out += px4(bg)
                out.append(1)                   # 一个子矩形
                out += px4(fg)
                out.append((0 << 4) | 0)        # x=0,y=0
                out.append((3 << 4) | 3)        # w-1=3,h-1=3
        return bytes(out)

    def build_frame(self) -> bytes:
        rects: list[bytes] = []

        def rect(x, y, w, h, enc, data=b""):
            head = x.to_bytes(2, "big") + y.to_bytes(2, "big")
            head += w.to_bytes(2, "big") + h.to_bytes(2, "big")
            head += int(enc).to_bytes(4, "big", signed=True)
            rects.append(head + data)

        rect(0, 0, 64, 64, const.ENC_RAW, px4(RED) * (64 * 64))
        rect(64, 0, 40, 32, const.ENC_HEXTILE, self._hextile_subrects(40, 32, GREEN, WHITE))
        rect(0, 64, 48, 48, const.ENC_HEXTILE, self._hextile_solid(48, 48, BLUE))
        rect(200, 100, 64, 64, const.ENC_COPY_RECT, (0).to_bytes(2, "big") + (0).to_bytes(2, "big"))
        return b"\x00\x00" + len(rects).to_bytes(2, "big") + b"".join(rects)

    def run(self) -> None:
        try:
            self._conn, _ = self.sock.accept()
            self._conn.settimeout(10)
            self._send(b"RFB 003.008\n")
            assert self._recv(12) == b"RFB 003.008\n"

            self._send(bytes((1, const.SEC_VNC_AUTH)))
            assert self._recv(1)[0] == const.SEC_VNC_AUTH

            challenge = bytes(range(16))
            self._send(challenge)
            assert self._recv(16) == vnc_response(PASSWORD, challenge), "VNC 认证响应不匹配"

            self._send((0).to_bytes(4, "big"))
            self.events.append(("clientinit", self._recv(1)[0]))

            init = W.to_bytes(2, "big") + H.to_bytes(2, "big") + const.PIXEL_FORMAT
            name = b"FakeServer"
            init += len(name).to_bytes(4, "big") + name
            self._send(init)

            while True:
                msg = self._recv(1)[0]
                if msg == const.MSG_SET_PIXEL_FORMAT:
                    self._recv(19)
                    self.events.append(("pixelformat",))
                elif msg == const.MSG_SET_ENCODINGS:
                    self._recv(1)
                    count = int.from_bytes(self._recv(2), "big")
                    encs = [int.from_bytes(self._recv(4), "big", signed=True) for _ in range(count)]
                    self.events.append(("encodings", encs))
                elif msg == const.MSG_FB_UPDATE_REQUEST:
                    body = self._recv(9)
                    if not self.sent:
                        self.sent = True
                        self._send(self.build_frame())
                    self.events.append(("request", body[0]))
                elif msg == const.MSG_KEY_EVENT:
                    body = self._recv(7)
                    self.events.append(("key", body[0], int.from_bytes(body[3:7], "big")))
                elif msg == const.MSG_POINTER_EVENT:
                    body = self._recv(5)
                    self.events.append(("pointer", body[0], int.from_bytes(body[1:3], "big"),
                                        int.from_bytes(body[3:5], "big")))
                elif msg == const.MSG_CLIENT_CUT_TEXT:
                    self._recv(3)
                    length = int.from_bytes(self._recv(4), "big")
                    self.events.append(("cuttext", self._recv(length) if length else b""))
                else:
                    self.events.append(("unknown", msg))
                    break
        except ConnectionError:
            pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


def test_zrle() -> bool:
    """ZRLE 解码器单测:2 色棋盘(含非 8 倍数瓦片)+ 4 色条纹 + palette RLE"""
    ok = True
    w, h = 100, 64

    def pack_palette(width: int, height: int, colors, index_fn) -> bytes:
        out = bytearray()
        for ty in range(0, height, 64):
            th = min(64, height - ty)
            for tx in range(0, width, 64):
                tw = min(64, width - tx)
                out.append(len(colors))
                for color in colors:
                    out += px3(color)
                bpp = 1 if len(colors) == 2 else (2 if len(colors) <= 4 else 4)
                for yy in range(th):
                    acc = 0
                    bits = 0
                    for xx in range(tw):
                        acc = (acc << bpp) | index_fn(tx + xx, ty + yy)
                        bits += bpp
                        if bits == 8:
                            out.append(acc)
                            acc = 0
                            bits = 0
                    if bits:
                        out.append(acc << (8 - bits))
        return bytes(out)

    def run_decode(data: bytes, fw: int, fh: int):
        compressed = zlib.compress(data) + zlib.compressobj().flush(zlib.Z_SYNC_FLUSH)
        stream = io.BytesIO(len(compressed).to_bytes(4, "big") + compressed)
        fb = codec.Framebuffer(fw, fh)
        codec.decode_zrle(stream, fb, 0, 0, fw, fh, zlib.decompressobj())
        return fb.data

    # 2 色棋盘,宽 100 使第二个瓦片为 36 像素(非 8 倍数)
    data = pack_palette(w, h, [RED, BLUE], lambda x, y: ((x // 8) + (y // 8)) & 1)
    out = run_decode(data, w, h)
    yy, xx = np.mgrid[0:h, 0:w]
    expect = np.array([RED, BLUE], dtype=np.uint8)[((xx // 8) + (yy // 8)) & 1]
    ok &= bool(np.array_equal(out[:, :, :3], expect))
    print(f"{'PASS' if ok else 'FAIL'}  ZRLE 2 色棋盘(含非 8 倍数瓦片)")

    data = pack_palette(64, 64, [RED, GREEN, BLUE, WHITE], lambda x, y: (x // 4) & 3)
    out = run_decode(data, 64, 64)
    yy, xx = np.mgrid[0:64, 0:64]
    expect = np.array([RED, GREEN, BLUE, WHITE], dtype=np.uint8)[(xx // 4) & 3]
    step = bool(np.array_equal(out[:, :, :3], expect))
    ok &= step
    print(f"{'PASS' if step else 'FAIL'}  ZRLE 4 色条纹(2bit 打包)")
    return ok


def main() -> int:
    server = FakeServer()
    server.start()

    states: list[str] = []
    client = VncClient("127.0.0.1", server.port, PASSWORD, on_state=lambda s, i: states.append(s))
    client.start()

    deadline = time.time() + 6
    frame = None
    while time.time() < deadline:
        w, h, _ = client.frame_geometry()
        if w > 0:
            buf = np.zeros((h, w, 4), dtype=np.uint8)
            client.grab_full(buf)
            client.grab(buf)
            if buf[10, 10, 2] == 255:
                frame = buf
                break
        time.sleep(0.05)

    ok = True

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{extra}]" if extra and not cond else ""))
        if not cond:
            ok = False

    check("握手与认证", ClientState.ONLINE in states, str(states))
    if frame is None:
        check("收到画面", False, f"server_error={server.error}")
        client.stop()
        return 1

    def region(x, y, w, h):
        return frame[y:y + h, x:x + w, :3]

    check("收到画面", True)
    check("Raw 纯红 64x64", np.array_equal(region(0, 0, 64, 64), np.full((64, 64, 3), RED, np.uint8)))

    # Hextile:背景绿 + 每个 16x16 左上 4x4 白色子矩形
    exp = np.full((32, 40, 3), GREEN, np.uint8)
    for ty in range(0, 32, 16):
        for tx in range(0, 40, 16):
            exp[ty:ty + 4, tx:tx + 4] = WHITE
    check("Hextile 子矩形(含跨行裁剪)", np.array_equal(region(64, 0, 40, 32), exp))
    check("Hextile 纯色 48x48", np.array_equal(region(0, 64, 48, 48), np.full((48, 48, 3), BLUE, np.uint8)))
    check("CopyRect 区域复制", np.array_equal(region(200, 100, 64, 64), region(0, 0, 64, 64)))

    client.send_pointer(1, 123, 45)
    client.send_key(0x61, True)
    client.send_key(0x61, False)
    client.send_clipboard("hello")
    time.sleep(0.4)

    keys = [e for e in server.events if e[0] == "key"]
    pointers = [e for e in server.events if e[0] == "pointer"]
    cuts = [e for e in server.events if e[0] == "cuttext"]
    encodings = [e for e in server.events if e[0] == "encodings"]
    check("鼠标事件送达", any(e[1] == 1 and e[2] == 123 and e[3] == 45 for e in pointers), str(pointers[:3]))
    check("键盘按下送达", any(e[2] == 0x61 for e in keys), str(keys[:3]))
    check("剪贴板送达", bool(cuts) and cuts[0][1] == b"hello", str(cuts[:1]))
    check("声明 Hextile 编码", bool(encodings) and const.ENC_HEXTILE in encodings[0][1])
    check("未声明 ZRLE", bool(encodings) and const.ENC_ZRLE not in encodings[0][1])

    client.stop()

    print()
    ok &= test_zrle()
    print("\n结果:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
