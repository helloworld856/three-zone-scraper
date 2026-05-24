from __future__ import annotations

import threading
import time
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
    def __init__(self, title: str, fields: list[FieldSpec], *, width: int = 720, height: int = 680) -> None:
        super().__init__()
        self.setWindowTitle(title)
        self.resize(width, height)
        self.fields = fields
        self.widgets: dict[str, Any] = {}
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.logger = get_logger(self.__class__.__name__)
        self.signals = WorkerSignals(self)
        self.signals.log.connect(self.append_log)
        self.signals.finished.connect(self._finish_success)
        self.signals.failed.connect(self._finish_error)
        self.form_layout: QFormLayout | None = None
        self.config_values: dict[str, Any] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(6)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(5)
        root.addLayout(form)
        self.form_layout = form

        for field in self.fields:
            widget = self._create_field_widget(field)
            form.addRow(QLabel(field.label), widget)

        buttons = QHBoxLayout()
        self.config_button = QPushButton("参数配置")
        self.config_button.clicked.connect(self._open_config)
        buttons.addWidget(self.config_button)
        buttons.addStretch(1)
        self.action_button = QPushButton("开始")
        self.action_button.clicked.connect(self._on_action_button)
        buttons.addWidget(self.action_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        root.addLayout(buttons)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlaceholderText("运行日志")
        self.log_text.document().setMaximumBlockCount(5000)
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
            widget.setMinimumHeight(64)
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
        elif field.kind == "text_or_file":
            widget = QWidget()
            vbox = QVBoxLayout(widget)
            vbox.setContentsMargins(0, 0, 0, 0)
            vbox.setSpacing(4)
            mode_combo = QComboBox()
            mode_combo.addItems(["直接输入", "TXT 文件"])
            vbox.addWidget(mode_combo)
            text_edit = QPlainTextEdit()
            text_edit.setPlaceholderText(field.placeholder or "每行一条")
            text_edit.setMinimumHeight(48)
            vbox.addWidget(text_edit)
            file_row = QWidget()
            fl = QHBoxLayout(file_row)
            fl.setContentsMargins(0, 0, 0, 0)
            file_edit = QLineEdit()
            file_edit.setPlaceholderText("选择 TXT 文件...")
            file_btn = QPushButton("选择")
            file_btn.clicked.connect(lambda _=False, e=file_edit: self._select_text_file(e))
            fl.addWidget(file_edit, 1)
            fl.addWidget(file_btn)
            vbox.addWidget(file_row)
            file_row.hide()
            mode_combo.currentTextChanged.connect(lambda t: (text_edit.show(), file_row.hide()) if t == "直接输入" else (text_edit.hide(), file_row.show()))
            widget.mode_combo = mode_combo
            widget.text_edit = text_edit
            widget.file_edit = file_edit
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

    def _select_text_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 TXT 文件", str(Path.cwd()), "Text Files (*.txt);;All Files (*.*)")
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
            elif field.kind == "text_or_file":
                if widget.mode_combo.currentText() == "TXT 文件":
                    file_path = widget.file_edit.text().strip()
                    if not file_path:
                        QMessageBox.warning(self, "提示", f"请选择或输入：{field.label}")
                        return None
                    try:
                        value = Path(file_path).read_text(encoding="utf-8").strip()
                    except Exception as exc:
                        QMessageBox.warning(self, "提示", f"无法读取文件：{exc}")
                        return None
                else:
                    value = widget.text_edit.toPlainText().strip()
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
        form = self.form_layout
        if form is None:
            return
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

    def _on_action_button(self) -> None:
        text = self.action_button.text()
        if text == "开始":
            self._do_start()
        elif text == "暂停":
            self._toggle_pause()
        elif text == "继续":
            self._toggle_pause()

    def _do_start(self) -> None:
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
        self.pause_event.clear()
        self._set_state("running")
        self.logger.info("Task starting: %s", self.windowTitle())
        self.worker_thread = threading.Thread(target=self._run_worker, args=(values,), daemon=False)
        self.worker_thread.start()

    def _toggle_pause(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self._set_state("running")
            self.append_log("继续运行...")
        else:
            self.pause_event.set()
            self._set_state("paused")
            self.append_log("已暂停，点击「继续」恢复运行。")

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.logger.info("Stop requested: %s", self.windowTitle())
        self.append_log("正在停止，请稍候...")

    def _text_to_tempfile(self, text: str, prefix: str = "input") -> str:
        from src.core import build_output_path

        path = build_output_path("temp", f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        return path

    def validate_values(self, values: dict[str, Any]) -> None:
        return None

    def tool_config_params(self) -> list[Any]:
        return []

    def _open_config(self) -> None:
        from src.ui.config_dialog import ConfigDialog

        params = self.tool_config_params()
        if not params:
            QMessageBox.information(self, "提示", "此工具没有可配置的参数。")
            return
        dialog = ConfigDialog(self.windowTitle(), params, self.config_values, self)
        if dialog.exec_() == ConfigDialog.Accepted:
            self.config_values = dialog.get_values()

    def run_task(self, values: dict[str, Any], log_callback, finish_callback, stop_event, pause_event) -> Any:
        raise NotImplementedError

    def _run_worker(self, values: dict[str, Any]) -> None:
        result = {"path": None}
        for key, val in self.config_values.items():
            values[key] = val

        def log_callback(message: str) -> None:
            self.signals.log.emit(str(message))

        def finish_callback(path=None) -> None:
            result["path"] = path

        try:
            returned = self.run_task(values, log_callback, finish_callback, self.stop_event, self.pause_event)
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

    def _set_state(self, state: str) -> None:
        if state == "running":
            self.action_button.setText("暂停")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        elif state == "paused":
            self.action_button.setText("继续")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(True)
        else:
            self.action_button.setText("开始")
            self.action_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def _finish_success(self, output_path) -> None:
        self._set_state("idle")
        if self.stop_event.is_set():
            self.append_log("任务已停止。")
            return
        if output_path:
            QMessageBox.information(self, "完成", f"结果已保存到：\n{output_path}")
        else:
            self.append_log("任务完成。")

    def _finish_error(self, message: str) -> None:
        self._set_state("idle")
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
            self.pause_event.clear()
            self.worker_thread.join(timeout=5)
            try:
                self.signals.log.disconnect()
                self.signals.finished.disconnect()
                self.signals.failed.disconnect()
            except TypeError:
                pass
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
