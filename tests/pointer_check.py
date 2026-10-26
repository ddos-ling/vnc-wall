"""坐标映射验证:控件点击位置 -> 发往远端的帧缓冲坐标

模拟 900x600 的格子显示 640x480 的画面(左右各有 50px 黑边),
检查若干位置的映射结果是否精确。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from protocol_check import PASSWORD, FakeServer
from app.config import CellConfig
from app.ui.tile import VncTile

WIDGET_W, WIDGET_H = 900, 600
IMAGE_W, IMAGE_H = 640, 480
SCALE = min(WIDGET_W / IMAGE_W, WIDGET_H / IMAGE_H)       # 1.25
OFF_X = (WIDGET_W - IMAGE_W * SCALE) / 2                 # 50
OFF_Y = (WIDGET_H - IMAGE_H * SCALE) / 2                 # 0


def send_move(tile: VncTile, x: int, y: int) -> None:
    local = QPointF(x, y)
    event = QMouseEvent(QEvent.MouseMove, local, tile.mapToGlobal(local),
                        Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    tile.mouseMoveEvent(event)


def main() -> int:
    app = QApplication([])
    server = FakeServer()
    server.start()

    tile = VncTile(0)
    tile.resize(WIDGET_W, WIDGET_H)
    tile.apply_cell(CellConfig(host="127.0.0.1", port=server.port, password=PASSWORD))

    deadline = time.time() + 8
    while time.time() < deadline and tile._image is None:
        app.processEvents()
        tile.tick()
        time.sleep(0.02)

    if tile._image is None:
        print("FAIL 未取得画面", server.error)
        return 1
    print(f"PASS 画面尺寸 {tile._image.width()}x{tile._image.height()},"
          f"格子 {tile.width()}x{tile.height()},缩放 {SCALE},横向偏移 {OFF_X}")

    cases = [
        (int(OFF_X), 0, 0, 0),                      # 画面左上角
        (int(OFF_X) + 400, 300, 320, 240),          # 画面正中
        (WIDGET_W - 1, WIDGET_H - 1, 639, 479),     # 画面右下角
        (int(OFF_X) + 80, 120, 64, 96),             # 任意点
    ]

    ok = True
    for wx, wy, ex, ey in cases:
        before = len(server.events)
        send_move(tile, wx, wy)
        deadline = time.time() + 1.5
        got = None
        while time.time() < deadline:
            app.processEvents()
            for event in server.events[before:]:
                if event[0] == "pointer":
                    got = (event[2], event[3])
            if got:
                break
            time.sleep(0.01)
        status = "PASS" if got == (ex, ey) else "FAIL"
        if got != (ex, ey):
            ok = False
        print(f"{status} 控件({wx},{wy}) -> 远端 {got},期望 ({ex},{ey})")

    # 光标绘制位置:应严格跟随鼠标,不受画面黑边影响
    from PySide6.QtGui import QPixmap
    tile._cursor_pixmap = QPixmap(16, 16)
    tile._hotspot = type(tile._hotspot)(0, 0)
    tile._mouse_inside = True
    tile._mouse_pos = type(tile._mouse_pos)(450, 300)
    area = tile._image_rect()
    cx = tile._mouse_pos.x() - tile._hotspot.x() * (area.width() / IMAGE_W)
    cy = tile._mouse_pos.y() - tile._hotspot.y() * (area.width() / IMAGE_W)
    cursor_ok = (cx, cy) == (450, 300)
    print(f"{'PASS' if cursor_ok else 'FAIL'} 远程光标绘制位置 ({cx},{cy}),期望跟随鼠标 (450,300)")
    ok &= cursor_ok

    tile.finish_shutdown()
    print("\n结果:", "坐标链路正确" if ok else "存在偏差")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
