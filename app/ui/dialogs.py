"""配置对话框:设备列表、布局、批量导入"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import CellConfig, Profile, parse_endpoint

INPUT_WIDTH = 26


class DeviceDialog(QDialog):
    """按格子顺序编辑地址与密码"""

    def __init__(self, profile: Profile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设备配置")
        self.setMinimumWidth(660)
        self.profile = profile

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        tip = QLabel(
            f"当前布局 {profile.rows} 行 × {profile.cols} 列,共 {len(profile.cells)} 格。"
            "地址格式 192.168.1.1:5900,端口可省略(默认 5900)。"
        )
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        layout.addWidget(tip)

        self.table = QTableWidget(len(profile.cells), 4, self)
        self.table.setHorizontalHeaderLabels(["格位", "地址", "密码", "记住"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(3, 62)
        self.table.setMinimumHeight(320)

        self._addr_edits: list[QLineEdit] = []
        self._pwd_edits: list[QLineEdit] = []
        self._remember_boxes: list[QCheckBox] = []

        for row, cell in enumerate(profile.cells):
            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignCenter)
            index.setForeground(Qt.gray)
            self.table.setItem(row, 0, index)

            addr = QLineEdit(cell.address, self.table)
            addr.setPlaceholderText("192.168.1.1:5900")
            self.table.setCellWidget(row, 1, _wrap(addr))
            self._addr_edits.append(addr)

            pwd = QLineEdit(cell.password, self.table)
            pwd.setEchoMode(QLineEdit.Password)
            pwd.setPlaceholderText("远程密码")
            self.table.setCellWidget(row, 2, _wrap(pwd))
            self._pwd_edits.append(pwd)

            box = QCheckBox()
            box.setChecked(cell.remember)
            box.setToolTip("勾选后使用 Windows DPAPI 加密保存在本机")
            holder = QWidget(self.table)
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setAlignment(Qt.AlignCenter)
            hl.addWidget(box)
            self.table.setCellWidget(row, 3, holder)
            self._remember_boxes.append(box)

        layout.addWidget(self.table)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        btn_import = QPushButton("批量导入…", self)
        btn_clear = QPushButton("全部清空", self)
        btn_import.clicked.connect(self._batch_import)
        btn_clear.clicked.connect(self._clear_all)
        tools.addWidget(btn_import)
        tools.addWidget(btn_clear)
        tools.addStretch(1)
        layout.addLayout(tools)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._apply_style()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog { background: #10141C; }
            QLabel[role="hint"] { color: #8A93A5; font-size: 12px; }
            QTableWidget { background: #161B25; alternate-background-color: #141922;
                           color: #E6EAF2; gridline-color: #232A36; border: 1px solid #232A36;
                           border-radius: 6px; }
            QHeaderView::section { background: #1B2130; color: #9AA4B6; border: none;
                                   padding: 6px; font-weight: 600; }
            QLineEdit { background: #0E1219; color: #E6EAF2; border: 1px solid #2A3240;
                        border-radius: 4px; padding: 4px 6px; }
            QLineEdit:focus { border: 1px solid #1C4D9F; }
            QPushButton { background: #1B2130; color: #DCE3EE; border: 1px solid #2A3240;
                          border-radius: 4px; padding: 6px 14px; }
            QPushButton:hover { background: #232B3C; }
            QPushButton:default { background: #1C4D9F; border-color: #1C4D9F; color: #fff; }
            """
        )

    def _batch_import(self) -> None:
        dialog = BatchImportDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        entries = dialog.entries
        if not entries:
            return
        filled = 0
        for i, (addr, pwd) in enumerate(entries):
            if i >= len(self._addr_edits):
                break
            self._addr_edits[i].setText(addr)
            if pwd:
                self._pwd_edits[i].setText(pwd)
            filled += 1
        if filled < len(entries):
            QMessageBox.information(self, "提示", f"当前只有 {len(self._addr_edits)} 格,已导入前 {filled} 条。")

    def _clear_all(self) -> None:
        if QMessageBox.question(self, "确认", "清空所有格的地址与密码?") != QMessageBox.Yes:
            return
        for addr, pwd in zip(self._addr_edits, self._pwd_edits):
            addr.clear()
            pwd.clear()

    def _on_accept(self) -> None:
        for row, edit in enumerate(self._addr_edits):
            text = edit.text().strip()
            if not text:
                continue
            host, port = parse_endpoint(text)
            if not host:
                QMessageBox.warning(self, "地址无效", f"第 {row + 1} 格地址无法解析:{text}")
                return
            if not (1 <= port <= 65535):
                QMessageBox.warning(self, "端口无效", f"第 {row + 1} 格端口超出范围:{port}")
                return
        for row, cell in enumerate(self.profile.cells):
            host, port = parse_endpoint(self._addr_edits[row].text().strip())
            cell.host = host
            cell.port = port
            cell.password = self._pwd_edits[row].text()
            cell.remember = self._remember_boxes[row].isChecked()
        self.accept()


class BatchImportDialog(QDialog):
    """一次性粘贴多个地址"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量导入")
        self.setMinimumSize(480, 360)
        self.entries: list[tuple[str, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        label = QLabel("每行一条,格式:地址 [密码]\n例如:\n192.168.1.1 admin123\n192.168.1.1:5900 admin123")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.text = QPlainTextEdit(self)
        self.text.setPlaceholderText("192.168.1.1 admin123")
        layout.addWidget(self.text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("导入")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet(
            """
            QDialog { background: #10141C; }
            QLabel { color: #9AA4B6; }
            QPlainTextEdit { background: #0E1219; color: #E6EAF2; border: 1px solid #2A3240;
                             border-radius: 6px; padding: 6px; }
            QPushButton { background: #1B2130; color: #DCE3EE; border: 1px solid #2A3240;
                          border-radius: 4px; padding: 6px 14px; }
            QPushButton:default { background: #1C4D9F; border-color: #1C4D9F; color: #fff; }
            """
        )

    def _on_accept(self) -> None:
        entries: list[tuple[str, str]] = []
        for line in self.text.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            host, port = parse_endpoint(parts[0])
            if not host:
                continue
            pwd = parts[1] if len(parts) > 1 else ""
            entries.append((host if port == 5900 else f"{host}:{port}", pwd))
        if not entries:
            QMessageBox.warning(self, "无有效数据", "没有解析到有效地址")
            return
        self.entries = entries
        self.accept()


class LayoutDialog(QDialog):
    """自定义行列"""

    def __init__(self, rows: int, cols: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自定义布局")
        self.setFixedWidth(300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        row_box = QHBoxLayout()
        row_box.addWidget(QLabel("行数"))
        self.rows = QSpinBox(self)
        self.rows.setRange(1, 8)
        self.rows.setValue(rows)
        row_box.addWidget(self.rows)
        row_box.addStretch(1)
        layout.addLayout(row_box)

        col_box = QHBoxLayout()
        col_box.addWidget(QLabel("列数"))
        self.cols = QSpinBox(self)
        self.cols.setRange(1, 8)
        self.cols.setValue(cols)
        col_box.addWidget(self.cols)
        col_box.addStretch(1)
        layout.addLayout(col_box)

        hint = QLabel("最多 8×8,格子越多单路画面越小")
        hint.setStyleSheet("color:#8A93A5;font-size:12px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("应用")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet(
            """
            QDialog { background: #10141C; }
            QLabel { color: #DCE3EE; }
            QSpinBox { background: #0E1219; color: #E6EAF2; border: 1px solid #2A3240;
                       border-radius: 4px; padding: 4px 8px; min-width: 90px; }
            QPushButton { background: #1B2130; color: #DCE3EE; border: 1px solid #2A3240;
                          border-radius: 4px; padding: 6px 14px; }
            QPushButton:default { background: #1C4D9F; border-color: #1C4D9F; color: #fff; }
            """
        )

    @property
    def value(self) -> tuple[int, int]:
        return self.rows.value(), self.cols.value()


def _wrap(widget: QWidget) -> QWidget:
    holder = QWidget()
    hl = QHBoxLayout(holder)
    hl.setContentsMargins(4, 2, 4, 2)
    hl.addWidget(widget)
    return holder
