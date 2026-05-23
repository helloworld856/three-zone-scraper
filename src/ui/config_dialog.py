from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


@dataclass
class ConfigParam:
    key: str
    label: str
    kind: str = "int"          # int | float | combo | bool
    default: Any = 0
    minimum: float = 0
    maximum: float = 999999
    step: float = 1
    decimals: int = 1          # for float
    options: tuple[str, ...] = ()
    tooltip: str = ""


class ConfigDialog(QDialog):
    def __init__(
        self,
        title: str,
        params: list[ConfigParam],
        current_values: dict[str, Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{title} — 参数配置")
        self.resize(560, 420)
        self.params = params
        self._current = dict(current_values or {})
        self._widgets: dict[str, Any] = {}
        self._build_ui()
        self._apply_current_or_defaults()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QLabel("调整爬取行为参数。留空或关闭窗口将使用默认值。")
        header.setWordWrap(True)
        header.setStyleSheet("color: #667085; font-size: 9pt;")
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        root.addWidget(scroll, 1)

        form_widget = QWidget()
        scroll.setWidget(form_widget)
        form = QFormLayout(form_widget)
        form.setLabelAlignment(Qt.AlignRight)
        form.setVerticalSpacing(8)
        form.setHorizontalSpacing(10)

        for param in self.params:
            widget = self._create_widget(param)
            label = QLabel(param.label)
            if param.tooltip:
                label.setToolTip(param.tooltip)
            form.addRow(label, widget)
            self._widgets[param.key] = widget

        buttons = QHBoxLayout()
        restore_btn = QPushButton("恢复默认值")
        restore_btn.clicked.connect(self._apply_defaults)
        buttons.addWidget(restore_btn)
        buttons.addStretch(1)
        save_btn = QPushButton("保存")
        save_btn.setObjectName("primaryButton")
        save_btn.clicked.connect(self.accept)
        buttons.addWidget(save_btn)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        root.addLayout(buttons)

        self.setStyleSheet("""
            QDialog {
                background: #f6f8fb;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 6px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #cfd8e6;
                border-radius: 6px;
                padding: 7px 16px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #edf4ff;
            }
            #primaryButton {
                background: #2563eb;
                border-color: #2563eb;
                color: white;
            }
            #primaryButton:hover {
                background: #1d4ed8;
            }
            QScrollArea {
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                background: #ffffff;
            }
        """)

    def _create_widget(self, param: ConfigParam):
        if param.kind == "int":
            widget = QSpinBox()
            widget.setRange(int(param.minimum), int(param.maximum))
            widget.setValue(int(param.default))
            widget.setSingleStep(max(1, int(param.step)))
        elif param.kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(float(param.minimum), float(param.maximum))
            widget.setValue(float(param.default))
            widget.setSingleStep(max(0.1, float(param.step)))
            widget.setDecimals(max(1, int(param.decimals)))
        elif param.kind == "combo":
            widget = QComboBox()
            widget.addItems(param.options)
            if str(param.default) in param.options:
                widget.setCurrentText(str(param.default))
        elif param.kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(param.default))
        else:
            widget = QSpinBox()
            widget.setRange(int(param.minimum), int(param.maximum))
            widget.setValue(int(param.default))
        widget.installEventFilter(self)
        if param.tooltip:
            widget.setToolTip(param.tooltip)
        return widget

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            return True
        return super().eventFilter(obj, event)

    def _apply_current_or_defaults(self) -> None:
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            value = self._current.get(param.key, param.default)
            if param.kind == "int":
                widget.setValue(int(value))
            elif param.kind == "float":
                widget.setValue(float(value))
            elif param.kind == "combo" and str(value) in param.options:
                widget.setCurrentText(str(value))
            elif param.kind == "bool":
                widget.setChecked(bool(value))

    def _apply_defaults(self) -> None:
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            if param.kind == "int":
                widget.setValue(int(param.default))
            elif param.kind == "float":
                widget.setValue(float(param.default))
            elif param.kind == "combo" and str(param.default) in param.options:
                widget.setCurrentText(str(param.default))
            elif param.kind == "bool":
                widget.setChecked(bool(param.default))

    def get_values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for param in self.params:
            widget = self._widgets.get(param.key)
            if widget is None:
                continue
            if param.kind == "int":
                result[param.key] = widget.value()
            elif param.kind == "float":
                result[param.key] = widget.value()
            elif param.kind == "combo":
                result[param.key] = widget.currentText()
            elif param.kind == "bool":
                result[param.key] = widget.isChecked()
        return result
