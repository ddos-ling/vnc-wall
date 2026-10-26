"""RFB 3.8 客户端

一个实例对应一格画面,内部单线程完成「连接 -> 认证 -> 解码 -> 断线重连」全流程
对外只暴露线程安全的方法:取帧、发键鼠、发剪贴板、停止

稳定性设计:
- socket 只在本线程内读,任何协议异常都走统一断线路径,不会留下半死连接
- 心跳探针(1x1 非增量请求)区分「画面静止」与「链路已死」,避免误判离线
- 指数退避重连,认证失败不重试(避免把对面账号锁死)
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import zlib
from typing import Callable

import numpy as np

from . import codec, const
from .codec import Framebuffer, ProtocolError
from .des import vnc_response

log = logging.getLogger(__name__)

# 心跳:静默超过该秒数就发探针,超过该秒数仍无任何回包判定离线
HEARTBEAT_IDLE = 5.0
HEARTBEAT_TIMEOUT = 14.0
# socket 单次等待上限,决定 stop() 的响应速度
RECV_TIMEOUT = 1.0
CONNECT_TIMEOUT = 6.0
# 帧间隔下限,防止高帧率画面把 CPU 吃满
MIN_FRAME_INTERVAL = 0.008
# 一次最多合并的脏矩形数量,超过则整帧拷贝
DIRTY_MERGE_LIMIT = 240

BACKOFF_STEPS = (1, 2, 3, 5, 8, 10)


class ClientState:
    CONNECTING = "connecting"
    ONLINE = "online"
    OFFLINE = "offline"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


class AuthFailed(Exception):
    """认证失败,不重试"""


class Disconnected(Exception):
    """链路断开,触发重连"""


class _Stop(Exception):
    """用户主动停止"""


class VncClient:
    def __init__(
        self,
        host: str,
        port: int = 5900,
        password: str = "",
        on_state: Callable[[str, dict], None] | None = None,
        on_cursor: Callable[[np.ndarray | None, int, int], None] | None = None,
        on_clipboard: Callable[[str], None] | None = None,
        on_bell: Callable[[], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.server_name = ""
        self._on_state = on_state
        self._on_cursor = on_cursor
        self._on_clipboard = on_clipboard
        self._on_bell = on_bell

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._wlock = threading.Lock()
        self._lock = threading.Lock()

        self._fb = Framebuffer(0, 0)
        self._dirty: list[tuple[int, int, int, int]] = []

        self._rbuf = bytearray()
        self._rpos = 0
        self._last_rx = 0.0
        self._hb_sent = False
        self._zdec = zlib.decompressobj()
        self._state = ClientState.CONNECTING
        self._retry_in = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"rfb-{self.host}:{self.port}", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        """只发停止信号不等待,便于批量关闭时并行收敛"""
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self, timeout: float = 5.0) -> None:
        self.request_stop()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._set_state(ClientState.STOPPED)

    @property
    def state(self) -> str:
        return self._state

    @property
    def retry_in(self) -> int:
        return self._retry_in

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ #
    # 帧数据
    # ------------------------------------------------------------------ #

    def frame_geometry(self) -> tuple[int, int, int]:
        """返回 (宽, 高, 尺寸代次),宽度为 0 表示尚未收到画面"""
        with self._lock:
            return self._fb.width, self._fb.height, self._fb.generation

    def grab_full(self, target: np.ndarray) -> None:
        """把整帧拷入目标数组(尺寸变化后调用)"""
        with self._lock:
            h = min(target.shape[0], self._fb.height)
            w = min(target.shape[1], self._fb.width)
            if h > 0 and w > 0:
                target[:h, :w] = self._fb.data[:h, :w]
            self._dirty.clear()

    def grab(self, target: np.ndarray) -> bool:
        """把脏区拷入目标数组,返回是否有新内容"""
        with self._lock:
            if not self._dirty:
                return False
            rects = self._dirty
            self._dirty = []
            fh, fw = self._fb.height, self._fb.width
            th, tw = target.shape[0], target.shape[1]
            if len(rects) > DIRTY_MERGE_LIMIT:
                h = min(fh, th)
                w = min(fw, tw)
                if h > 0 and w > 0:
                    target[:h, :w] = self._fb.data[:h, :w]
                return True
            for x, y, w, h in rects:
                x2 = min(x + w, fw, tw)
                y2 = min(y + h, fh, th)
                if x < 0 or y < 0 or x2 <= x or y2 <= y:
                    continue
                target[y:y2, x:x2] = self._fb.data[y:y2, x:x2]
            return True

    # ------------------------------------------------------------------ #
    # 输入事件
    # ------------------------------------------------------------------ #

    def send_pointer(self, mask: int, x: int, y: int) -> None:
        self._send(bytes((const.MSG_POINTER_EVENT, mask & 0xFF)) + _u16(x) + _u16(y))

    def send_key(self, keysym: int, down: bool) -> None:
        self._send(bytes((const.MSG_KEY_EVENT, 1 if down else 0, 0, 0)) + _u32(keysym))

    def send_clipboard(self, text: str) -> None:
        raw = text.encode("latin-1", "replace")
        if len(raw) > 1 << 20:
            raw = raw[: 1 << 20]
        self._send(bytes((const.MSG_CLIENT_CUT_TEXT, 0, 0, 0)) + _u32(len(raw)) + raw)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    def _set_state(self, state: str, **info) -> None:
        self._state = state
        self._retry_in = int(info.get("retry_in", 0))
        if self._on_state:
            try:
                self._on_state(state, info)
            except Exception:
                log.exception("状态回调异常")

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._session()
                if self._stop.is_set():
                    break
                raise Disconnected("服务器结束会话")
            except _Stop:
                break
            except AuthFailed as exc:
                log.warning("%s:%s 认证失败: %s", self.host, self.port, exc)
                self._set_state(ClientState.AUTH_FAILED, message=str(exc))
                self._close()
                return
            except Disconnected as exc:
                reason = str(exc)
                log.info("%s:%s 断开: %s", self.host, self.port, reason)
            except Exception as exc:  # 任何意外都不许让线程静默退出
                reason = f"{type(exc).__name__}: {exc}"
                log.exception("%s:%s 会话异常", self.host, self.port)
            finally:
                self._close()

            if self._stop.is_set():
                break
            attempt += 1
            delay = BACKOFF_STEPS[min(attempt - 1, len(BACKOFF_STEPS) - 1)]
            self._set_state(ClientState.OFFLINE, retry_in=delay, message=reason, attempt=attempt)
            if self._stop.wait(delay):
                break

        self._set_state(ClientState.STOPPED)

    def _session(self) -> None:
        if self._stop.is_set():
            raise _Stop()
        self._set_state(ClientState.CONNECTING)
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            raise Disconnected(f"连接失败: {exc}") from exc
        self._sock = sock
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        sock.settimeout(RECV_TIMEOUT)

        self._rbuf = bytearray()
        self._rpos = 0
        self._zdec = zlib.decompressobj()
        self._last_rx = time.monotonic()
        self._hb_sent = False

        self._handshake()
        self._set_state(ClientState.ONLINE, name=self.server_name)
        self._loop()

    def _close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._on_cursor:
            try:
                self._on_cursor(None, 0, 0)
            except Exception:
                log.exception("光标回调异常")

    # ------------------------------------------------------------------ #
    # 握手
    # ------------------------------------------------------------------ #

    def _handshake(self) -> None:
        version = self._take(12)
        if not version.startswith(b"RFB ") or not version.endswith(b"\n"):
            raise Disconnected(f"不是 RFB 服务:{version!r}")
        try:
            major = int(version[4:7])
            minor = int(version[8:11])
        except ValueError as exc:
            raise Disconnected(f"协议版本异常:{version!r}") from exc
        if major != 3 or minor < 3:
            raise Disconnected(f"不支持的 RFB 版本 {major}.{minor}")

        if minor >= 8:
            self._send(b"RFB 003.008\n")
            self._security_types_38()
        elif minor == 7:
            self._send(b"RFB 003.007\n")
            self._security_types_37()
        else:
            self._send(b"RFB 003.003\n")
            self._security_types_33()

        self._send(bytes((1,)))  # ClientInit,shared=1 表示不独占会话
        width = int.from_bytes(self._take(2), "big")
        height = int.from_bytes(self._take(2), "big")
        self._take(16)  # 服务器原生像素格式,我们随后强制改为 32bpp
        name_len = int.from_bytes(self._take(4), "big")
        if name_len:
            self.server_name = self._take(name_len).decode("latin-1", "replace")

        with self._lock:
            self._fb = Framebuffer(width, height)
            self._fb.generation += 1
            self._dirty.clear()

        self._send(bytes((const.MSG_SET_PIXEL_FORMAT, 0, 0, 0)) + const.PIXEL_FORMAT)
        encodings = const.SUPPORTED_ENCODINGS
        payload = bytearray((const.MSG_SET_ENCODINGS, 0))
        payload += len(encodings).to_bytes(2, "big")
        for enc in encodings:
            payload += int(enc).to_bytes(4, "big", signed=True)
        self._send(bytes(payload))

    def _security_types_38(self) -> None:
        count = self._take(1)[0]
        if count == 0:
            self._raise_security_reason(has_reason=True)
        types = list(self._take(count))
        self._choose_security(types, has_reason=True)

    def _security_types_37(self) -> None:
        count = self._take(1)[0]
        if count == 0:
            raise AuthFailed("服务器拒绝连接(未给出原因)")
        types = list(self._take(count))
        self._choose_security(types, has_reason=False)

    def _security_types_33(self) -> None:
        sec = int.from_bytes(self._take(4), "big")
        if sec == const.SEC_INVALID:
            self._raise_security_reason(has_reason=True)
        if sec == const.SEC_NONE:
            # RFB 3.3 使用 None 认证时服务器不再发送 SecurityResult
            return
        if sec == const.SEC_VNC_AUTH:
            self._vnc_auth(has_reason=False)
            return
        raise AuthFailed(f"服务器仅支持 {const.SEC_NAMES.get(sec, sec)} 认证")

    def _choose_security(self, types: list[int], has_reason: bool) -> None:
        if const.SEC_VNC_AUTH in types:
            chosen = const.SEC_VNC_AUTH
        elif const.SEC_NONE in types:
            chosen = const.SEC_NONE
        else:
            names = ", ".join(str(const.SEC_NAMES.get(t, t)) for t in types)
            raise AuthFailed(f"服务器要求不支持的认证方式:{names}")
        self._send(bytes((chosen,)))
        if chosen == const.SEC_NONE:
            self._security_result(has_reason)
        else:
            self._vnc_auth(has_reason)

    def _vnc_auth(self, has_reason: bool) -> None:
        challenge = self._take(16)
        self._send(vnc_response(self.password or "", challenge))
        self._security_result(has_reason)

    def _security_result(self, has_reason: bool) -> None:
        result = int.from_bytes(self._take(4), "big")
        if result == 0:
            return
        if result == 1:
            raise AuthFailed(self._read_reason() if has_reason else "密码错误")
        if result == 2:
            raise AuthFailed("服务器限制登录(连接次数过多)")
        raise AuthFailed(f"认证失败,返回码 {result}")

    def _raise_security_reason(self, has_reason: bool) -> None:
        raise AuthFailed(self._read_reason() if has_reason else "服务器拒绝连接")

    def _read_reason(self) -> str:
        try:
            length = int.from_bytes(self._take(4), "big")
            if 0 < length <= 4096:
                return self._take(length).decode("latin-1", "replace") or "认证失败"
        except (Disconnected, _Stop):
            pass
        return "认证失败"

    # ------------------------------------------------------------------ #
    # 消息循环
    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        self._request_update(incremental=False, x=0, y=0, w=self._fb.width, h=self._fb.height)
        while True:
            if self._stop.is_set():
                raise _Stop()
            msg = self._take(1)[0]
            if msg == const.MSG_FB_UPDATE:
                self._handle_framebuffer_update()
            elif msg == const.MSG_SET_COLOUR_MAP:
                self._handle_colour_map()
            elif msg == const.MSG_BELL:
                if self._on_bell:
                    self._bell_safe()
            elif msg == const.MSG_SERVER_CUT_TEXT:
                self._handle_cut_text()
            else:
                raise ProtocolError(f"未知的服务器消息类型 {msg}")

    def _bell_safe(self) -> None:
        try:
            self._on_bell()
        except Exception:
            log.exception("响铃回调异常")

    def _handle_colour_map(self) -> None:
        self._take(1)
        int.from_bytes(self._take(2), "big")
        count = int.from_bytes(self._take(2), "big")
        self._take(count * 6)

    def _handle_cut_text(self) -> None:
        self._take(3)
        length = int.from_bytes(self._take(4), "big")
        if length > (8 << 20):
            raise ProtocolError(f"剪贴板数据过大: {length}")
        text = self._take(length).decode("latin-1", "replace")
        if self._on_clipboard:
            try:
                self._on_clipboard(text)
            except Exception:
                log.exception("剪贴板回调异常")

    def _handle_framebuffer_update(self) -> None:
        start = time.monotonic()
        self._take(1)  # padding
        count = int.from_bytes(self._take(2), "big")
        if count > 4096:
            raise ProtocolError(f"矩形数量异常: {count}")

        resized = False
        grew = False
        for _ in range(count):
            x = int.from_bytes(self._take(2), "big")
            y = int.from_bytes(self._take(2), "big")
            w = int.from_bytes(self._take(2), "big")
            h = int.from_bytes(self._take(2), "big")
            enc = int.from_bytes(self._take(4), "big", signed=True)

            if enc == const.ENC_DESKTOP_SIZE:
                with self._lock:
                    if w != self._fb.width or h != self._fb.height:
                        self._fb.resize(w, h)
                        resized = True
                        grew = True
                        self._dirty.clear()
                continue
            if enc == const.ENC_CURSOR:
                self._handle_cursor(x, y, w, h)
                continue

            try:
                if enc == const.ENC_RAW:
                    codec.decode_raw(self, self._fb, x, y, w, h)
                elif enc == const.ENC_COPY_RECT:
                    codec.decode_copyrect(self, self._fb, x, y, w, h)
                elif enc == const.ENC_HEXTILE:
                    codec.decode_hextile(self, self._fb, x, y, w, h)
                elif enc == const.ENC_ZRLE:
                    codec.decode_zrle(self, self._fb, x, y, w, h, self._zdec)
                else:
                    raise ProtocolError(f"服务器使用了未声明的编码 {enc}")
            except ProtocolError:
                raise
            except Exception as exc:
                raise ProtocolError(f"解码失败(编码 {enc}): {exc}") from exc

            if w > 0 and h > 0:
                with self._lock:
                    self._dirty.append((x, y, w, h))

        if resized:
            self._set_state(ClientState.ONLINE, name=self.server_name, resized=True)

        if grew:
            self._request_update(False, 0, 0, self._fb.width, self._fb.height)
        else:
            self._request_update(True, 0, 0, self._fb.width, self._fb.height)

        # 限制帧率,避免高动态画面把 CPU 占满
        elapsed = time.monotonic() - start
        if elapsed < MIN_FRAME_INTERVAL:
            self._stop.wait(MIN_FRAME_INTERVAL - elapsed)

    def _handle_cursor(self, x: int, y: int, w: int, h: int) -> None:
        if w == 0 or h == 0:
            if self._on_cursor:
                self._on_cursor(None, 0, 0)
            return
        if w > 512 or h > 512:
            raise ProtocolError(f"光标尺寸异常 {w}x{h}")
        pixels = self._take(w * h * 4)
        mask = self._take(((w + 7) // 8) * h)
        rgba = codec.decode_cursor_pixels(pixels + mask, w, h)
        if self._on_cursor:
            try:
                self._on_cursor(rgba, x, y)
            except Exception:
                log.exception("光标回调异常")

    def _request_update(self, incremental: bool, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            # 服务器报告了 0 尺寸时退化成一个极小区域,避免发起非法请求
            w, h = 1, 1
        self._send(bytes((const.MSG_FB_UPDATE_REQUEST, 1 if incremental else 0))
                   + _u16(x) + _u16(y) + _u16(min(w, 0xFFFF)) + _u16(min(h, 0xFFFF)))

    # ------------------------------------------------------------------ #
    # 底层收发
    # ------------------------------------------------------------------ #

    def read(self, n: int) -> bytes:
        """供解码器使用的定长读取"""
        return self._take(n)

    def _take(self, n: int) -> bytes:
        if n == 0:
            return b""
        buf = self._rbuf
        if len(buf) - self._rpos < n:
            self._fill(n)
        end = self._rpos + n
        out = bytes(buf[self._rpos:end])
        self._rpos = end
        return out

    def _fill(self, need: int) -> None:
        sock = self._sock
        if sock is None:
            raise Disconnected("连接已关闭")
        while len(self._rbuf) - self._rpos < need:
            if self._stop.is_set():
                raise _Stop()
            try:
                chunk = sock.recv(262144)
            except socket.timeout:
                self._check_heartbeat()
                continue
            except OSError as exc:
                raise Disconnected(f"读取失败: {exc}") from exc
            if not chunk:
                raise Disconnected("服务器关闭了连接")
            if self._rpos:
                del self._rbuf[:self._rpos]
                self._rpos = 0
            self._rbuf += chunk
            self._last_rx = time.monotonic()
            self._hb_sent = False

    def _check_heartbeat(self) -> None:
        now = time.monotonic()
        idle = now - self._last_rx
        if idle > HEARTBEAT_TIMEOUT:
            raise Disconnected(f"心跳超时({idle:.0f}s 无响应)")
        if idle > HEARTBEAT_IDLE and not self._hb_sent:
            # 画面静止时服务器不会主动发包,用 1x1 非增量请求探测链路
            self._hb_sent = True
            try:
                self._request_update(False, 0, 0, 1, 1)
            except Exception:
                pass

    def _send(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        with self._wlock:
            try:
                sock.sendall(data)
            except OSError:
                # 断线由读线程统一处理,这里不能阻塞 UI
                pass


def _u16(v: int) -> bytes:
    return max(0, min(0xFFFF, int(v))).to_bytes(2, "big")


def _u32(v: int) -> bytes:
    return max(0, min(0xFFFFFFFF, int(v))).to_bytes(4, "big")
