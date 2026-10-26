"""统一字体:按平台回退,避免缺字体时中文变方块"""

from __future__ import annotations

from PySide6.QtGui import QFont

# 优先 Windows 中文界面字体,再退到跨平台开源中文字体
FAMILIES = [
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Source Han Sans SC",
    "Noto Sans CJK SC",
    "Segoe UI",
    "sans-serif",
]


def ui_font(size: int = 9, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(FAMILIES)
    font.setPointSize(size)
    font.setBold(bold)
    return font


def mono_font(size: int = 9) -> QFont:
    font = QFont()
    font.setFamilies(["JetBrains Mono", "Cascadia Mono", "Consolas", "monospace"])
    font.setPointSize(size)
    return font
