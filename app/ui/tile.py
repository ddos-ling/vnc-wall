"""单格画面控件:显示远端画面、状态角标、键鼠转发"""

from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ..config import CellConfig
from ..rfb.client import ClientState, VncClient
from . import keysyms
from .fonts import ui_font

log = logging.getLogger(__name__)

BG = QColor("#080A0E")
PANEL = QColor(10, 14, 20, 190)
TEXT = QColor("#E6EAF2")
TEXT_DIM = QColor("#8A93A5")
BRAND = QColor("#1C4D9F")
OK = QColor("#93C93E")
WARN = QColor("#F2A93B")
BAD = QColor("#E34D59")

class VncTile(QWidget):
    soloRequested = Signal(object)
    exitSoloRequested = Signal()
    configRequested = Signal(object)
    stateChanged = Signal(object)

    def __init__(self, index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.index = index
        self.cell = CellConfig()
        self.client: VncClient | None = None

        self._state = "idle"
        self._retry_in = 0
        self._attempt = 0
        self._message = ""
        self._solo = False

        self._buffer: np.ndarray | None = None
        self._image: QImage | None = None
        self._image_buf: np.ndarray | None = None
        self._mouse_inside = False
        self._mouse_pos = QPoint(0, 0)
        self._cursor_pixmap: QPixmap | None = None
        self._hotspot = QPoint(0, 0)
        self._pressed_keys: set[int] = set()
        self._active_key: tuple[str, int, str] | None = None
        self._closing = False

        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(180, 130)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        self._font = ui_font(9)
        self._font_small = ui_font(8)

        self.sigState.connect(self._on_state_ui)
        self.sigCursor.connect(self._on_cursor_ui)

    sigState = Signal(str, dict)
    sigCursor = Signal(object, int, int)

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #

    def apply_cell(self, cell: CellConfig, autoconnect: bool = True) -> None:
        self.cell = cell
        # 地址变了就丢弃旧画面,避免瞬间显示别的机器内容
        self._buffer = None
        self._image = None
        self._image_buf = None
        if autoconnect and cell.configured:
            self.reconnect()
        else:
            self.disconnect()
            self._state = "idle"
            self.update()

    def reconnect(self) -> None:
        self.disconnect(silent=True)
        cell = self.cell
        if not cell.configured:
            self._state = "idle"
            self.update()
            return
        self._cursor_pixmap = None
        self.setCursor(Qt.ArrowCursor)
        self._state = "connecting"
        self._message = ""
        self._attempt = 0
        self.update()
        client = VncClient(
            cell.host,
            cell.port,
            cell.password,
            on_state=self._emit_state,
            on_cursor=self._emit_cursor,
        )
        self.client = client
        self._active_key = (cell.host, cell.port, cell.password)
        client.start()
        self._notify()

    @property
    def active_key(self) -> tuple[str, int, str] | None:
        """当前正在使用的连接参数快照,用于判断是否需要重连"""
        return self._active_key

    def disconnect(self, silent: bool = False) -> None:
        client = self.client
        self.client = None
        if client is not None:
            client.stop(timeout=1.5)
        self._pressed_keys.clear()
        if not silent:
            self._state = "idle" if not self.cell.configured else "stopped"
            self._message = ""
            self.update()
            self._notify()

    def shutdown(self) -> None:
        """窗口关闭时的快速退出:先全部发停止信号,再由主窗口统一等待"""
        if self.client is not None:
            self.client.request_stop()

    def finish_shutdown(self) -> None:
        if self.client is not None:
            self.client.stop(timeout=1.0)
            self.client = None
        # 控件即将销毁,让后台线程剩余的回调不再往界面发信号
        self._closing = True

    @property
    def state(self) -> str:
        return self._state

    @property
    def online(self) -> bool:
        return self._state == "online"

    # ------------------------------------------------------------------ #
    # 线程回调
    # ------------------------------------------------------------------ #

    def _emit_state(self, state: str, info: dict) -> None:
        if self._closing:
            return
        self.sigState.emit(state, info)

    def _emit_cursor(self, rgba, hx: int, hy: int) -> None:
        if self._closing:
            return
        self.sigCursor.emit(rgba, hx, hy)

    def _on_state_ui(self, state: str, info: dict) -> None:
        self._state = state
        self._retry_in = int(info.get("retry_in", 0) or 0)
        self._attempt = int(info.get("attempt", self._attempt) or 0)
        self._message = str(info.get("message", "") or "")
        if state != ClientState.ONLINE:
            self._cursor_pixmap = None
            self.setCursor(Qt.ArrowCursor)
        self.update()
        self._notify()

    def _on_cursor_ui(self, rgba, hx: int, hy: int) -> None:
        if rgba is None:
            self._cursor_pixmap = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            return
        try:
            h, w = rgba.shape[:2]
            img = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()
            self._cursor_pixmap = QPixmap.fromImage(img)
            self._hotspot = QPoint(hx, hy)
            self.setCursor(Qt.BlankCursor)
            self.update()
        except Exception:
            log.exception("光标绘制失败")

    def _notify(self) -> None:
        self.stateChanged.emit(self.index)

    # ------------------------------------------------------------------ #
    # 帧刷新
    # ------------------------------------------------------------------ #

    def tick(self) -> None:
        """拉取最新画面,由主窗口统一驱动,避免每格一个定时器"""
        client = self.client
        if client is None:
            return
        width, height, _gen = client.frame_geometry()
        if width <= 0 or height <= 0:
            return
        buf = self._buffer
        if buf is None or buf.shape[0] != height or buf.shape[1] != width:
            self._buffer = np.zeros((height, width, 4), dtype=np.uint8)
            client.grab_full(self._buffer)
            self._make_image()
            self.update()
            return
        if client.grab(buf):
            self._make_image()
            self.update()
        elif self._image is None:
            self._make_image()
            self.update()

    def _make_image(self) -> None:
        buf = self._buffer
        if buf is None:
            self._image = None
            self._image_buf = None
            return
        h, w = buf.shape[0], buf.shape[1]
        try:
            image = QImage(buf.data, w, h, w * 4, QImage.Format_RGB32)
        except TypeError:
            image = QImage(buf.tobytes(), w, h, w * 4, QImage.Format_RGB32)
        # QImage 不持有 numpy 内存的所有权,必须自己保活,否则可能读到已释放内存
        self._image = image
        self._image_buf = buf

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #

    def _image_rect(self) -> QRectF:
        if self._image is None or self._image.width() <= 0:
            return QRectF()
        iw, ih = self._image.width(), self._image.height()
        aw, ah = self.width(), self.height()
        scale = min(aw / iw, ah / ih)
        return QRectF((aw - iw * scale) / 2, (ah - ih * scale) / 2, iw * scale, ih * scale)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        area = self._image_rect()
        if self._image is not None and not area.isEmpty():
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(area, self._image)
            self._paint_remote_cursor(p, area)
        else:
            # 无画面时的占位;连接状态文字统一由 _paint_status 负责,避免重复
            if not self.cell.configured:
                p.setPen(TEXT_DIM)
                p.setFont(self._font)
                p.drawText(self.rect(), Qt.AlignCenter, "双击配置地址")
        self._paint_status(p)

    def _paint_remote_cursor(self, p: QPainter, area: QRectF) -> None:
        pix = self._cursor_pixmap
        if pix is None or not self._mouse_inside or self._image is None:
            return
        scale = area.width() / self._image.width()
        # _mouse_pos 本身就是控件坐标,不能再叠加 area 偏移,否则光标会整体偏移
        x = self._mouse_pos.x() - self._hotspot.x() * scale
        y = self._mouse_pos.y() - self._hotspot.y() * scale
        if scale != 1.0:
            size = QSize(max(1, int(round(pix.width() * scale))), max(1, int(round(pix.height() * scale))))
            p.drawPixmap(QRect(int(round(x)), int(round(y)), size.width(), size.height()), pix)
        else:
            p.drawPixmap(int(round(x)), int(round(y)), pix)

    def _paint_status(self, p: QPainter) -> None:
        state = self._state
        rect = self.rect()
        fm = QFontMetrics(self._font_small)
        text = self.cell.address or "未配置"
        if self.cell.label:
            text = f"{self.cell.label}  {text}"

        if state == ClientState.ONLINE:
            text = fm.elidedText(text, Qt.ElideMiddle, max(40, rect.width() - 52))
            bar_w = min(rect.width() - 8, fm.horizontalAdvance(text) + 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(6, 9, 14, 175))
            p.drawRoundedRect(QRectF(4, 4, max(44, bar_w), 20), 4, 4)
            p.setBrush(OK)
            p.drawEllipse(QPoint(15, 14), 3, 3)
            p.setBrush(Qt.NoBrush)
            p.setFont(self._font_small)
            p.setPen(TEXT)
            p.drawText(QRect(22, 4, max(20, bar_w - 24), 20), Qt.AlignVCenter | Qt.AlignLeft, text)
            return

        # 非在线:整条状态色 + 粗边框 + 中央大字,一眼能看出这格没画面
        if state in (ClientState.OFFLINE, ClientState.AUTH_FAILED):
            color, badge, title = BAD, "离线", "离线"
            sub = f"{self._retry_in} 秒后自动重连" if self._retry_in > 0 else "正在重连…"
            if self._attempt > 1:
                sub = f"第 {self._attempt} 次重连 · {sub}"
            if state == ClientState.AUTH_FAILED:
                badge, title, sub = "密码错误", "密码错误", "右键 → 设置此格,修正密码"
        elif state == ClientState.CONNECTING:
            color, badge, title, sub = WARN, "连接中", "连接中…", self.cell.address
        elif state == "stopped":
            color, badge, title, sub = WARN, "已断开", "已断开", "右键 → 重新连接"
        else:
            color, badge, title, sub = TEXT_DIM, "未配置", "未配置", "双击配置地址"

        bar_h = 24 if rect.height() >= 120 else 20
        p.fillRect(QRect(0, 0, rect.width(), bar_h), color)
        p.setFont(self._font_small)
        p.setPen(QColor("#FFFFFF"))
        left = fm.elidedText(text, Qt.ElideMiddle, max(20, rect.width() - 88))
        p.drawText(QRect(9, 0, max(20, rect.width() - 96), bar_h), Qt.AlignVCenter | Qt.AlignLeft, left)
        p.drawText(QRect(rect.width() - 84, 0, 76, bar_h), Qt.AlignVCenter | Qt.AlignRight, badge)

        p.setPen(QPen(color, 3))
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect.adjusted(2, 2, -2, -2))

        shade = 90 if state == ClientState.CONNECTING else 165
        p.fillRect(rect.adjusted(3, bar_h, -3, -3), QColor(0, 0, 0, shade))

        mid = rect.height() // 2
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPoint(rect.width() // 2, mid - 30), 5, 5)
        p.setBrush(Qt.NoBrush)

        size = 14 if rect.height() >= 240 else (12 if rect.height() >= 150 else 9)
        p.setFont(ui_font(size, True))
        p.setPen(color)
        p.drawText(QRect(0, mid - 18, rect.width(), size + 14), Qt.AlignCenter, title)

        p.setFont(self._font_small)
        p.setPen(QColor("#D6DEEA"))
        p.drawText(QRect(6, mid + 12, rect.width() - 12, 18), Qt.AlignCenter, sub)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def _to_remote(self, pos: QPoint) -> tuple[int, int]:
        buf = self._buffer
        area = self._image_rect()
        if buf is None or area.isEmpty():
            return 0, 0
        ih, iw = buf.shape[0], buf.shape[1]
        rx = int((pos.x() - area.x()) / area.width() * iw)
        ry = int((pos.y() - area.y()) / area.height() * ih)
        return max(0, min(iw - 1, rx)), max(0, min(ih - 1, ry))

    def _button_mask(self, buttons) -> int:
        mask = 0
        if buttons & Qt.LeftButton:
            mask |= 1
        if buttons & Qt.MiddleButton:
            mask |= 2
        if buttons & Qt.RightButton:
            mask |= 4
        return mask

    def _send_pointer(self, mask: int) -> None:
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            return
        x, y = self._to_remote(self._mouse_pos)
        client.send_pointer(mask, x, y)

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.MouseFocusReason)
        self._mouse_pos = event.position().toPoint()
        self._send_pointer(self._button_mask(event.buttons()))
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._mouse_pos = event.position().toPoint()
        self._send_pointer(self._button_mask(event.buttons()))
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self._mouse_pos = event.position().toPoint()
        self._send_pointer(self._button_mask(event.buttons()))
        if self._cursor_pixmap is not None:
            self.update()
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if not self.cell.configured:
            self.configRequested.emit(self)
            event.accept()
            return
        if self._solo:
            self.exitSoloRequested.emit()
        else:
            self.soloRequested.emit(self)
        event.accept()

    def wheelEvent(self, event) -> None:
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        button = 8 if delta > 0 else 16
        x, y = self._to_remote(event.position().toPoint())
        # 滚轮是一次性按键,按下后必须补一个空掩码,否则远端会一直滚
        client.send_pointer(button, x, y)
        client.send_pointer(0, x, y)
        event.accept()

    def enterEvent(self, event) -> None:
        self._mouse_inside = True
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._mouse_inside = False
        self.update()
        super().leaveEvent(event)

    def focusOutEvent(self, event) -> None:
        self._release_all_keys()
        super().focusOutEvent(event)

    def _release_all_keys(self) -> None:
        client = self.client
        if client is None:
            self._pressed_keys.clear()
            return
        for ks in list(self._pressed_keys):
            client.send_key(ks, False)
        self._pressed_keys.clear()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape and self._solo:
            self.exitSoloRequested.emit()
            event.accept()
            return
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            super().keyPressEvent(event)
            return
        ks = keysyms.keysym_for(event)
        if not ks:
            return
        if event.isAutoRepeat() and ks in self._pressed_keys:
            return
        client.send_key(ks, True)
        self._pressed_keys.add(ks)
        event.accept()

    def keyReleaseEvent(self, event) -> None:
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            super().keyReleaseEvent(event)
            return
        ks = keysyms.keysym_for(event)
        if not ks:
            return
        if event.isAutoRepeat():
            return
        client.send_key(ks, False)
        self._pressed_keys.discard(ks)
        event.accept()

    def set_solo(self, solo: bool) -> None:
        self._solo = solo

    def send_ctrl_alt_del(self) -> None:
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            return
        for ks in (keysyms.KEYSYM_CONTROL_L, keysyms.KEYSYM_ALT_L):
            client.send_key(ks, True)
        client.send_key(keysyms.KEYSYM_DELETE, True)
        client.send_key(keysyms.KEYSYM_DELETE, False)
        for ks in (keysyms.KEYSYM_ALT_L, keysyms.KEYSYM_CONTROL_L):
            client.send_key(ks, False)

    def paste_local_clipboard(self) -> None:
        """把本机剪贴板文本送到远端(中文输入法只能这样进远端)"""
        client = self.client
        if client is None or self._state != ClientState.ONLINE:
            return
        text = QApplication.clipboard().text()
        if text:
            client.send_clipboard(text)

    def _show_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        act_reconnect = menu.addAction("重新连接")
        act_disconnect = menu.addAction("断开")
        menu.addSeparator()
        act_solo = menu.addAction("退出单屏" if self._solo else "单屏显示")
        act_cad = menu.addAction("发送 Ctrl+Alt+Del")
        act_paste = menu.addAction("粘贴本机剪贴板")
        menu.addSeparator()
        act_config = menu.addAction("设置此格…")

        online = self._state == ClientState.ONLINE
        act_disconnect.setEnabled(online)
        act_cad.setEnabled(online)
        act_paste.setEnabled(online)

        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act_reconnect:
            self.reconnect()
        elif chosen is act_disconnect:
            self.disconnect()
        elif chosen is act_solo:
            if self._solo:
                self.exitSoloRequested.emit()
            else:
                self.soloRequested.emit(self)
        elif chosen is act_cad:
            self.send_ctrl_alt_del()
        elif chosen is act_paste:
            self.paste_local_clipboard()
        elif chosen is act_config:
            self.configRequested.emit(self)
