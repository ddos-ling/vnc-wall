"""RFB 3.8 协议常量定义"""

from __future__ import annotations

# 客户端 -> 服务器消息类型
MSG_SET_PIXEL_FORMAT = 0
MSG_SET_ENCODINGS = 2
MSG_FB_UPDATE_REQUEST = 3
MSG_KEY_EVENT = 4
MSG_POINTER_EVENT = 5
MSG_CLIENT_CUT_TEXT = 6

# 服务器 -> 客户端消息类型
MSG_FB_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_SERVER_CUT_TEXT = 3

# 安全类型
SEC_INVALID = 0
SEC_NONE = 1
SEC_VNC_AUTH = 2

# 安全类型名称(用于错误提示)
SEC_NAMES = {
    0: "Invalid",
    1: "None",
    2: "VNC Authentication",
    16: "Tight",
    18: "TLS(VeNCrypt)",
    19: "VeNCrypt",
    20: "SASL",
    21: "MD5 hash",
    22: "XVP",
    30: "RA2",
    35: "RA2ne",
    36: "Tight Unix",
    113: "MSLogonII",
}

# 矩形编码
ENC_RAW = 0
ENC_COPY_RECT = 1
ENC_HEXTILE = 5
ENC_ZRLE = 6
ENC_TRLE = 15
ENC_TIGHT = 7

# 伪编码
ENC_DESKTOP_SIZE = -223
ENC_LAST_RECT = -224
ENC_CURSOR = -239
ENC_CURSOR_ALPHA = -240

# 鼠标按键掩码
BUTTON_LEFT = 1
BUTTON_MIDDLE = 2
BUTTON_RIGHT = 4
BUTTON_WHEEL_UP = 8
BUTTON_WHEEL_DOWN = 16

# 协商给服务器的像素格式:32bpp / depth 24 / little-endian true color
# 红绿蓝分别位于 bit16 / bit8 / bit0,因此内存布局为 B,G,R,X 四字节
PIXEL_FORMAT = bytes((
    32,      # bits-per-pixel
    24,      # depth
    0,       # big-endian-flag
    1,       # true-colour-flag
    0, 255,  # red-max
    0, 255,  # green-max
    0, 255,  # blue-max
    16,      # red-shift
    8,       # green-shift
    0,       # blue-shift
    0, 0, 0,  # padding
))

# 我们协商的像素格式下,RGB 全部落在最低 3 字节,CPIXEL 为 3 字节
CPIXEL_BYTES = 3
PIXEL_BYTES = 4

# 客户端声明的编码偏好(靠前的优先级更高)
# 说明:部分服务端(实测 192.168.1.1 上的 Xvnc)的 ZRLE 编码未按 RFC 6143
# 写入 tile 子编码字节,数据无法解析,因此默认只声明 Hextile/Raw,保证画面稳定
SUPPORTED_ENCODINGS = (
    ENC_HEXTILE,
    ENC_COPY_RECT,
    ENC_RAW,
    ENC_DESKTOP_SIZE,
    ENC_CURSOR,
)
