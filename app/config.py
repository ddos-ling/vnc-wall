"""配置持久化与密码加密存储

密码使用 Windows DPAPI(CryptProtectData)加密,密钥绑定当前登录用户,
配置文件被拷到别的机器或别的账户下无法解密
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "VncWall"
DEFAULT_PORT = 5900
MAX_CELLS = 64

_ENTROPY = b"vnc-wall.config.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _load_crypt32():
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


_CRYPTO = _load_crypt32()


def encrypt_secret(plain: str) -> str:
    """加密密码,返回可写入 JSON 的字符串"""
    if not plain:
        return ""
    raw = plain.encode("utf-8")
    if _CRYPTO is None:
        log.warning("当前平台不支持 DPAPI,密码仅做 Base64 编码")
        return "b64:" + base64.b64encode(raw).decode("ascii")
    crypt32, kernel32 = _CRYPTO
    in_blob, _in_buf = _make_blob(raw)
    ent_blob, _ent_buf = _make_blob(_ENTROPY)
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return "dpapi:" + base64.b64encode(ctypes.string_at(out_blob.pbData, out_blob.cbData)).decode("ascii")
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def decrypt_secret(token: str) -> str:
    """解密密码,失败返回空串"""
    if not token:
        return ""
    try:
        if token.startswith("b64:"):
            return base64.b64decode(token[4:]).decode("utf-8")
        if not token.startswith("dpapi:"):
            return ""
        if _CRYPTO is None:
            return ""
        crypt32, kernel32 = _CRYPTO
        raw = base64.b64decode(token[6:])
        in_blob, _in_buf = _make_blob(raw)
        ent_blob, _ent_buf = _make_blob(_ENTROPY)
        out_blob = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
            None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
        )
        if not ok:
            log.warning("密码解密失败(可能换了机器或账户)")
            return ""
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
    except Exception:
        log.exception("密码解密异常")
        return ""


def parse_endpoint(text: str) -> tuple[str, int]:
    """解析 "192.168.1.1:5900" / "192.168.1.1" / "[::1]:5900" """
    text = (text or "").strip()
    if not text:
        return "", DEFAULT_PORT
    if text.startswith("["):
        host, _, rest = text.partition("]")
        host = host[1:]
        rest = rest.lstrip(":")
        port = int(rest) if rest.isdigit() else DEFAULT_PORT
        return host, port
    if text.count(":") == 1:
        host, _, port_text = text.partition(":")
        port = int(port_text) if port_text.isdigit() else DEFAULT_PORT
        return host.strip(), port
    return text, DEFAULT_PORT


def format_endpoint(host: str, port: int) -> str:
    if not host:
        return ""
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}" if port != DEFAULT_PORT else host


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class CellConfig:
    host: str = ""
    port: int = DEFAULT_PORT
    password: str = ""
    remember: bool = True
    label: str = ""

    @property
    def address(self) -> str:
        return format_endpoint(self.host, self.port)

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def to_json(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "password": encrypt_secret(self.password) if (self.remember and self.password) else "",
            "remember": self.remember,
            "label": self.label,
        }

    @classmethod
    def from_json(cls, data: dict) -> "CellConfig":
        remember = bool(data.get("remember", True))
        return cls(
            host=str(data.get("host", "") or ""),
            port=int(data.get("port", DEFAULT_PORT) or DEFAULT_PORT),
            password=decrypt_secret(data.get("password", "") or "") if remember else "",
            remember=remember,
            label=str(data.get("label", "") or ""),
        )


@dataclass
class Profile:
    name: str = "默认方案"
    rows: int = 2
    cols: int = 2
    cells: list[CellConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.normalize()

    def normalize(self) -> None:
        self.rows = max(1, min(8, int(self.rows)))
        self.cols = max(1, min(8, int(self.cols)))
        want = min(MAX_CELLS, self.rows * self.cols)
        if len(self.cells) < want:
            self.cells.extend(CellConfig() for _ in range(want - len(self.cells)))
        elif len(self.cells) > want:
            del self.cells[want:]

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "rows": self.rows,
            "cols": self.cols,
            "cells": [c.to_json() for c in self.cells],
        }

    @classmethod
    def from_json(cls, data: dict) -> "Profile":
        profile = cls(
            name=str(data.get("name", "方案") or "方案"),
            rows=int(data.get("rows", 2) or 2),
            cols=int(data.get("cols", 2) or 2),
            cells=[CellConfig.from_json(c) for c in data.get("cells", [])],
        )
        return profile


class AppConfig:
    def __init__(self) -> None:
        self.profiles: list[Profile] = []
        self.active: int = 0
        self.load()

    @property
    def profile(self) -> Profile:
        if not self.profiles:
            self.profiles = [self._default_profile()]
        self.active = max(0, min(self.active, len(self.profiles) - 1))
        return self.profiles[self.active]

    @staticmethod
    def _default_profile() -> Profile:
        return Profile(name="默认方案", rows=2, cols=2)

    def load(self) -> None:
        path = config_path()
        if not path.exists():
            self.profiles = [self._default_profile()]
            self.active = 0
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            profiles = [Profile.from_json(p) for p in data.get("profiles", [])]
            self.profiles = profiles or [self._default_profile()]
            self.active = int(data.get("active", 0) or 0)
            self.active = max(0, min(self.active, len(self.profiles) - 1))
        except Exception:
            log.exception("配置文件读取失败,已回落默认配置")
            self.profiles = [self._default_profile()]
            self.active = 0

    def save(self) -> None:
        data = {
            "version": 1,
            "active": self.active,
            "profiles": [p.to_json() for p in self.profiles],
        }
        path = config_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            log.exception("配置文件写入失败")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
