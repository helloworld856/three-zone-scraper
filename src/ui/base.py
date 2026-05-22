from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.app_logging import get_logger

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str = "text"
    default: str | int = ""
    required: bool = False
    minimum: int = 1
    maximum: int = 999999
    options: tuple[str, ...] = ()
    placeholder: str = ""


class WorkerSignals(QObject):
    log = pyqtSignal(str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class SimpleToolWindow(QWidget):
    def __init__(self, title: str, fields: list[FieldSpec], *, width: int = 720, height: int = 560) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.resize(width, height)
        self.fields = fields
        self.widgets: dict[str, Any] = {}
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.logger = get_logger(self.__class__.__name__)
        self.signals = WorkerSignals()
        self.signals.log.connect(self.append_log)
        self.signals.finished.connect(self._finish_success)
        self.signals.failed.connect(self._finish_error)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        root.addLayout(form)

        for field in self.fields:
            widget = self._create_field_widget(field)
            form.addRow(QLabel(field.label), widget)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.start_button = QPushButton("开始")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        root.addLayout(buttons)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("运行日志")
        root.addWidget(self.log_text, 1)
        self.setStyleSheet(
            """
            QWidget {
                background: #f6f8fb;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {
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
            QPushButton:disabled {
                color: #98a2b3;
                background: #eef2f7;
            }
            """
        )

    def _create_field_widget(self, field: FieldSpec):
        if field.kind == "multiline":
            widget = QPlainTextEdit()
            widget.setPlainText(str(field.default or ""))
            widget.setPlaceholderText(field.placeholder)
            widget.setMinimumHeight(82)
        elif field.kind == "int":
            widget = QSpinBox()
            widget.setRange(field.minimum, field.maximum)
            widget.setValue(int(field.default or field.minimum))
        elif field.kind == "combo":
            widget = QComboBox()
            widget.addItems(field.options)
            if field.default in field.options:
                widget.setCurrentText(str(field.default))
        elif field.kind in {"file", "folder"}:
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(str(field.default or ""))
            edit.setPlaceholderText(field.placeholder)
            button = QPushButton("选择")
            button.clicked.connect(lambda _=False, f=field, e=edit: self._select_path(f, e))
            layout.addWidget(edit, 1)
            layout.addWidget(button)
            widget = container
            widget.path_edit = edit
        else:
            widget = QLineEdit(str(field.default or ""))
            widget.setPlaceholderText(field.placeholder)
        self.widgets[field.name] = widget
        return widget

    def _select_path(self, field: FieldSpec, edit: QLineEdit) -> None:
        if field.kind == "folder":
            path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择文件", str(Path.cwd()), "Text Files (*.txt);;Excel Files (*.xlsx);;All Files (*.*)")
        if path:
            edit.setText(path)
            self.raise_()
            self.activateWindow()

    def collect_values(self) -> dict[str, Any] | None:
        values: dict[str, Any] = {}
        for field in self.fields:
            widget = self.widgets[field.name]
            if field.kind == "multiline":
                value = widget.toPlainText().strip()
            elif field.kind == "int":
                value = widget.value()
            elif field.kind == "combo":
                value = widget.currentText().strip()
            elif field.kind in {"file", "folder"}:
                value = widget.path_edit.text().strip()
            else:
                value = widget.text().strip()
            if field.required and not value and widget.isVisible():
                QMessageBox.warning(self, "提示", f"请填写：{field.label}")
                return None
            values[field.name] = value
        return values

    def set_field_visible(self, field_name: str, visible: bool) -> None:
        widget = self.widgets.get(field_name)
        if not widget:
            return
        widget.setVisible(visible)
        form = self.layout().itemAt(0).layout()
        label = form.labelForField(widget)
        if label:
            label.setVisible(visible)

    def bind_field_visibility(self, trigger_field: str, trigger_value: str, target_fields: list[str]) -> None:
        combo = self.widgets.get(trigger_field)
        if not isinstance(combo, QComboBox):
            self.logger.warning("bind_field_visibility expects a QComboBox for %s", trigger_field)
            return

        def on_changed(text: str):
            visible = (text == trigger_value)
            for target in target_fields:
                self.set_field_visible(target, visible)

        combo.currentTextChanged.connect(on_changed)
        on_changed(combo.currentText())

    def start(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            QMessageBox.information(self, "提示", "该工具正在运行。")
            return
        values = self.collect_values()
        if values is None:
            return
        try:
            self.validate_values(values)
        except Exception as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return
        self.log_text.clear()
        self.stop_event.clear()
        self._set_running(True)
        self.logger.info("Task starting: %s", self.windowTitle())
        self.worker_thread = threading.Thread(target=self._run_worker, args=(values,), daemon=True)
        self.worker_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.logger.info("Stop requested: %s", self.windowTitle())
        self.append_log("正在停止，请稍候...")

    def validate_values(self, values: dict[str, Any]) -> None:
        return None

    def run_task(self, values: dict[str, Any], log_callback, finish_callback, stop_event) -> Any:
        raise NotImplementedError

    def _run_worker(self, values: dict[str, Any]) -> None:
        result = {"path": None}

        def log_callback(message: str) -> None:
            self.signals.log.emit(str(message))

        def finish_callback(path=None) -> None:
            result["path"] = path

        try:
            returned = self.run_task(values, log_callback, finish_callback, self.stop_event)
            if returned is not None:
                result["path"] = returned
            self.logger.info("Task finished: %s output=%s", self.windowTitle(), result["path"] or "")
            self.signals.finished.emit(result["path"])
        except Exception as exc:
            self.logger.error("Task failed: %s\n%s", self.windowTitle(), traceback.format_exc())
            self.signals.failed.emit(str(exc))

    def append_log(self, message: str) -> None:
        self.log_text.append(str(message))
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.start_button.setText("运行中..." if running else "开始")

    def _finish_success(self, output_path) -> None:
        self._set_running(False)
        if self.stop_event.is_set():
            self.append_log("任务已停止。")
            return
        if output_path:
            QMessageBox.information(self, "完成", f"结果已保存到：\n{output_path}")

    def _finish_error(self, message: str) -> None:
        self._set_running(False)
        self.append_log(f"运行失败：{message}")
        QMessageBox.critical(self, "运行失败", message)

    def closeEvent(self, event) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            reply = QMessageBox.question(
                self,
                "确认关闭",
                "关闭该工具窗口会停止当前任务，确定关闭吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.stop_event.set()
        else:
            reply = QMessageBox.question(
                self,
                "确认关闭",
                "确定关闭该工具窗口吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()
