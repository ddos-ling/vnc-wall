"""VNC 电视墙启动入口"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication

from app.config import AppConfig, config_dir
from app.ui.fonts import ui_font
from app.ui.wall import WallWindow, make_icon

log = logging.getLogger("vncwall")


def setup_logging() -> None:
    log_dir = config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(log_dir / "vnccwall.log", maxBytes=2 << 20, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream)

    def hook(exc_type, exc_value, exc_tb):
        log.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = hook


def main() -> int:
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("VncWall")
    app.setApplicationDisplayName("VNC 电视墙")
    app.setStyle("Fusion")
    app.setFont(ui_font(9))

    config = AppConfig()
    window = WallWindow(config)
    window.setWindowIcon(make_icon())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
