"""Qt 按键 -> X11 keysym 映射

VNC 只传 keysym,修饰键(Shift/Ctrl/Alt)由服务器自行合成,
因此这里必须把修饰键本身也作为独立事件发出去
"""

from __future__ import annotations

from PySide6.QtCore import Qt

# 特殊键 keysym 表
SPECIAL = {
    Qt.Key_Escape: 0xFF1B,
    Qt.Key_Tab: 0xFF09,
    Qt.Key_Backtab: 0xFE20,
    Qt.Key_Backspace: 0xFF08,
    Qt.Key_Return: 0xFF0D,
    Qt.Key_Enter: 0xFF8D,
    Qt.Key_Insert: 0xFF63,
    Qt.Key_Delete: 0xFFFF,
    Qt.Key_Pause: 0xFF13,
    Qt.Key_Print: 0xFF61,
    Qt.Key_Home: 0xFF50,
    Qt.Key_End: 0xFF57,
    Qt.Key_Left: 0xFF51,
    Qt.Key_Up: 0xFF52,
    Qt.Key_Right: 0xFF53,
    Qt.Key_Down: 0xFF54,
    Qt.Key_PageUp: 0xFF55,
    Qt.Key_PageDown: 0xFF56,
    Qt.Key_Shift: 0xFFE1,
    Qt.Key_Control: 0xFFE3,
    Qt.Key_Meta: 0xFFEB,
    Qt.Key_Alt: 0xFFE9,
    Qt.Key_AltGr: 0xFFEA,
    Qt.Key_CapsLock: 0xFFE5,
    Qt.Key_NumLock: 0xFF7F,
    Qt.Key_ScrollLock: 0xFF14,
    Qt.Key_Menu: 0xFF67,
    Qt.Key_Help: 0xFF6A,
    Qt.Key_Clear: 0xFF0B,
    Qt.Key_Space: 0x0020,
}

KEYSYM_SHIFT_L = 0xFFE1
KEYSYM_CONTROL_L = 0xFFE3
KEYSYM_ALT_L = 0xFFE9
KEYSYM_DELETE = 0xFFFF
KEYSYM_SUPER_L = 0xFFEB

# F1..F12
_F1 = 0xFFBE


def keysym_for(event) -> int:
    """把 Qt 按键事件翻成 X11 keysym,无法映射返回 0"""
    key = event.key()
    f_key = _function_key(key)
    if f_key:
        return f_key
    special = SPECIAL.get(key)
    if special is not None:
        return special
    text = event.text()
    if text:
        code = ord(text[0])
        if 0x20 <= code <= 0x7E:
            return code
        if 0xA0 <= code <= 0xFF:
            return code
        # 非 Latin-1 字符用 Unicode keysym,主流服务端可识别
        if code > 0xFF and not 0xD800 <= code <= 0xDFFF:
            return 0x01000000 + code
    if 0x20 <= key <= 0x7E:
        return key
    return 0


def _function_key(key: int) -> int:
    first = Qt.Key_F1
    last = Qt.Key_F35
    if first <= key <= last:
        return _F1 + (key - first)
    return 0
