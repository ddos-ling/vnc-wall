"""电视墙主窗口:工具栏、网格布局、单屏模式"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QStackedLayout, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from ..config import AppConfig, Profile
from .dialogs import DeviceDialog, LayoutDialog
from .tile import VncTile

log = logging.getLogger(__name__)

PRESET_LAYOUTS = ((1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 2), (3, 3), (4, 4))

APP_QSS = """
QMainWindow, QWidget#Root, QWidget#Host, QWidget#Overlay { background: #080A0E; }
QToolBar { background: #11161F; border: none; border-bottom: 1px solid #1D2430;
           spacing: 8px; padding: 6px 10px; }
QToolBar QLabel { color: #8A93A5; font-size: 12px; }
QToolButton, QPushButton { background: #1A2130; color: #DCE3EE; border: 1px solid #27303F;
                           border-radius: 5px; padding: 5px 12px; font-size: 12px; }
QToolButton:hover, QPushButton:hover { background: #222B3D; border-color: #35415A; }
QToolButton:pressed, QPushButton:pressed { background: #161C28; }
QToolButton::menu-indicator { image: none; width: 0px; }
QComboBox { background: #1A2130; color: #DCE3EE; border: 1px solid #27303F;
            border-radius: 5px; padding: 4px 10px; min-width: 140px; font-size: 12px; }
QComboBox:hover { border-color: #35415A; }
QComboBox QAbstractItemView { background: #161C28; color: #DCE3EE;
                              selection-background-color: #1C4D9F; border: 1px solid #27303F; }
QMenu { background: #161C28; color: #DCE3EE; border: 1px solid #27303F; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 4px; }
QMenu::item:selected { background: #1C4D9F; }
QMenu::separator { height: 1px; background: #27303F; margin: 4px 8px; }
QStatusBar { background: #11161F; color: #8A93A5; border-top: 1px solid #1D2430; }
QStatusBar QLabel { color: #8A93A5; font-size: 12px; }
"""


class WallWindow(QMainWindow):
    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.tiles: list[VncTile] = []
        self._solo: VncTile | None = None

        self.setWindowTitle("VNC 电视墙")
        self.resize(1360, 860)
        self.setStyleSheet(APP_QSS)

        self._build_ui()
        self._refresh_profile_combo()
        self._rebuild(autoconnect=True)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start()

        # 用一个定时器统一下发帧刷新,避免每格各跑一个定时器
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._tick_tiles)
        self._frame_timer.start()

    def _tick_tiles(self) -> None:
        for tile in self.tiles:
            if tile.isVisible():
                tile.tick()

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.stack = QStackedLayout(root)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setStackingMode(QStackedLayout.StackOne)

        self.host = QWidget()
        self.host.setObjectName("Host")
        self.grid = QGridLayout(self.host)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setSpacing(4)

        self.overlay = QWidget()
        self.overlay.setObjectName("Overlay")
        self.overlay_layout = QVBoxLayout(self.overlay)
        self.overlay_layout.setContentsMargins(4, 4, 4, 4)

        self.stack.addWidget(self.host)
        self.stack.addWidget(self.overlay)
        self.setCentralWidget(root)

        bar = QToolBar()
        bar.setMovable(False)
        self.addToolBar(bar)

        bar.addWidget(QLabel("方案"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(150)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        bar.addWidget(self.profile_combo)

        self.btn_profile = QToolButton()
        self.btn_profile.setText("管理")
        self.btn_profile.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        menu.addAction("新建方案…", self._new_profile)
        menu.addAction("重命名…", self._rename_profile)
        menu.addAction("复制当前方案", self._duplicate_profile)
        menu.addSeparator()
        menu.addAction("删除当前方案", self._delete_profile)
        self.btn_profile.setMenu(menu)
        bar.addWidget(self.btn_profile)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        self.btn_layout = QToolButton()
        self.btn_layout.setText("布局")
        self.btn_layout.setPopupMode(QToolButton.InstantPopup)
        self.btn_layout.setMenu(self._build_layout_menu())
        bar.addWidget(self.btn_layout)

        btn_device = QPushButton("设备配置")
        btn_device.clicked.connect(self._open_devices)
        bar.addWidget(btn_device)

        btn_connect = QPushButton("全部连接")
        btn_connect.clicked.connect(self._connect_all)
        bar.addWidget(btn_connect)

        btn_disconnect = QPushButton("全部断开")
        btn_disconnect.clicked.connect(self._disconnect_all)
        bar.addWidget(btn_disconnect)

        btn_fullscreen = QPushButton("全屏")
        btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        bar.addWidget(btn_fullscreen)

        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)
        self._hint = QLabel("双击画面进入单屏 · Esc 返回 · 右键更多操作")
        self.statusBar().addWidget(self._hint)

    def _build_layout_menu(self) -> QMenu:
        menu = QMenu(self)
        for rows, cols in PRESET_LAYOUTS:
            action = menu.addAction(f"{rows} 行 × {cols} 列")
            action.triggered.connect(lambda _=False, r=rows, c=cols: self._set_layout(r, c))
        menu.addSeparator()
        menu.addAction("自定义…", self._custom_layout)
        return menu

    # ------------------------------------------------------------------ #
    # 网格重建
    # ------------------------------------------------------------------ #

    def _rebuild(self, autoconnect: bool) -> None:
        self._drop_tiles()
        profile = self.config.profile
        for index, cell in enumerate(profile.cells):
            tile = VncTile(index)
            tile.soloRequested.connect(self._enter_solo)
            tile.exitSoloRequested.connect(self._exit_solo)
            tile.configRequested.connect(self._on_tile_config)
            tile.stateChanged.connect(lambda _=0: self._update_status())
            self.grid.addWidget(tile, index // profile.cols, index % profile.cols)
            self.tiles.append(tile)
            tile.apply_cell(cell, autoconnect=autoconnect)
        self._update_status()

    def _drop_tiles(self) -> None:
        if self._solo is not None:
            self._solo.set_solo(False)
            self._solo = None
            self.stack.setCurrentWidget(self.host)
        tiles = self.tiles
        self.tiles = []
        for tile in tiles:
            tile.shutdown()
        for tile in tiles:
            tile.finish_shutdown()
        for tile in tiles:
            self.grid.removeWidget(tile)
            self.overlay_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()

    # ------------------------------------------------------------------ #
    # 方案
    # ------------------------------------------------------------------ #

    def _refresh_profile_combo(self) -> None:
        combo = self.profile_combo
        combo.blockSignals(True)
        combo.clear()
        for profile in self.config.profiles:
            combo.addItem(f"{profile.name}  ({profile.rows}×{profile.cols})")
        combo.setCurrentIndex(self.config.active)
        combo.blockSignals(False)

    def _on_profile_changed(self, index: int) -> None:
        if index < 0 or index == self.config.active:
            return
        self.config.active = index
        self.config.save()
        self._rebuild(autoconnect=True)

    def _new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "新建方案", "方案名称:", text="新方案")
        if not ok or not name.strip():
            return
        current = self.config.profile
        profile = Profile(name=name.strip(), rows=current.rows, cols=current.cols)
        self.config.profiles.append(profile)
        self.config.active = len(self.config.profiles) - 1
        self.config.save()
        self._refresh_profile_combo()
        self._rebuild(autoconnect=True)

    def _rename_profile(self) -> None:
        profile = self.config.profile
        name, ok = QInputDialog.getText(self, "重命名", "方案名称:", text=profile.name)
        if not ok or not name.strip():
            return
        profile.name = name.strip()
        self.config.save()
        self._refresh_profile_combo()

    def _duplicate_profile(self) -> None:
        source = self.config.profile
        clone = Profile.from_json(source.to_json())
        clone.name = f"{source.name} 副本"
        self.config.profiles.append(clone)
        self.config.active = len(self.config.profiles) - 1
        self.config.save()
        self._refresh_profile_combo()
        self._rebuild(autoconnect=True)

    def _delete_profile(self) -> None:
        if len(self.config.profiles) <= 1:
            QMessageBox.information(self, "无法删除", "至少需要保留一个方案")
            return
        profile = self.config.profile
        if QMessageBox.question(self, "确认", f"删除方案「{profile.name}」?") != QMessageBox.Yes:
            return
        del self.config.profiles[self.config.active]
        self.config.active = 0
        self.config.save()
        self._refresh_profile_combo()
        self._rebuild(autoconnect=True)

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #

    def _set_layout(self, rows: int, cols: int) -> None:
        profile = self.config.profile
        if (rows, cols) == (profile.rows, profile.cols):
            return
        configured = sum(1 for cell in profile.cells if cell.configured)
        if rows * cols < configured:
            answer = QMessageBox.question(
                self, "确认调整",
                f"新布局只有 {rows * cols} 格,当前已配置 {configured} 个地址,"
                f"多出的 {configured - rows * cols} 个会被丢弃。继续?",
            )
            if answer != QMessageBox.Yes:
                return
        profile.rows = rows
        profile.cols = cols
        profile.normalize()
        self.config.save()
        self._refresh_profile_combo()
        self._rebuild(autoconnect=True)

    def _custom_layout(self) -> None:
        profile = self.config.profile
        dialog = LayoutDialog(profile.rows, profile.cols, self)
        if dialog.exec() == LayoutDialog.Accepted:
            rows, cols = dialog.value
            self._set_layout(rows, cols)

    # ------------------------------------------------------------------ #
    # 连接控制
    # ------------------------------------------------------------------ #

    def _connect_all(self) -> None:
        for tile in self.tiles:
            if tile.cell.configured:
                tile.reconnect()

    def _disconnect_all(self) -> None:
        for tile in self.tiles:
            tile.disconnect()

    def _open_devices(self, focus_index: int | None = None) -> None:
        profile = self.config.profile
        dialog = DeviceDialog(profile, self)
        if focus_index is not None and 0 <= focus_index < len(dialog._addr_edits):
            dialog._addr_edits[focus_index].setFocus()
            dialog._addr_edits[focus_index].selectAll()
        if dialog.exec() != DeviceDialog.Accepted:
            return
        self.config.save()
        self._apply_cells()

    def _on_tile_config(self, tile: VncTile) -> None:
        self._open_devices(tile.index)

    def _apply_cells(self) -> None:
        """只重连参数真正发生变化的格子,避免整墙闪断"""
        profile = self.config.profile
        for tile, cell in zip(self.tiles, profile.cells):
            key = (cell.host, cell.port, cell.password)
            if tile.active_key == key and tile.client is not None:
                tile.update()
                continue
            tile.apply_cell(cell, autoconnect=True)
        self._update_status()

    def _update_status(self) -> None:
        total = len(self.tiles)
        online = sum(1 for t in self.tiles if t.online)
        offline = sum(1 for t in self.tiles if t.state == "offline")
        auth = sum(1 for t in self.tiles if t.state == "auth_failed")
        parts = [f"在线 {online}/{total}"]
        if offline:
            parts.append(f"离线 {offline}")
        if auth:
            parts.append(f"密码错误 {auth}")
        self.status_label.setText("   ".join(parts))

    # ------------------------------------------------------------------ #
    # 单屏 / 全屏
    # ------------------------------------------------------------------ #

    def _enter_solo(self, tile: VncTile) -> None:
        if self._solo is tile:
            return
        if self._solo is not None:
            self._exit_solo()
        self._solo = tile
        self.grid.removeWidget(tile)
        self.overlay_layout.addWidget(tile)
        tile.set_solo(True)
        self.stack.setCurrentWidget(self.overlay)
        tile.setFocus(Qt.OtherFocusReason)

    def _exit_solo(self) -> None:
        tile = self._solo
        if tile is None:
            return
        self._solo = None
        self.overlay_layout.removeWidget(tile)
        tile.set_solo(False)
        self.stack.setCurrentWidget(self.host)
        profile = self.config.profile
        self.grid.addWidget(tile, tile.index // profile.cols, tile.index % profile.cols)
        tile.setFocus(Qt.OtherFocusReason)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            if self._solo is not None:
                self._exit_solo()
                return
            if self.isFullScreen():
                self.showNormal()
                return
        elif event.key() == Qt.Key_F11:
            self._toggle_fullscreen()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ #
    # 退出
    # ------------------------------------------------------------------ #

    def closeEvent(self, event) -> None:
        for tile in self.tiles:
            tile.shutdown()
        for tile in self.tiles:
            tile.finish_shutdown()
        self.config.save()
        event.accept()


def make_icon() -> QIcon:
    """用品牌色画一个简易图标,免去外部资源依赖"""
    pix = QPixmap(64, 64)
    pix.fill(QColor("#0E1219"))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#1C4D9F"))
    painter.drawRoundedRect(4, 4, 56, 56, 10, 10)
    painter.setBrush(QColor("#93C93E"))
    for row in range(2):
        for col in range(2):
            painter.drawRoundedRect(12 + col * 22, 12 + row * 22, 18, 18, 3, 3)
    painter.end()
    return QIcon(pix)
