from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import QProcess, Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.app_logging import get_logger, setup_console_logging
from src.studio.registry import TOOLS

logger = get_logger(__name__)


ALL_CATEGORY = "全部"
CATEGORY_ORDER = [ALL_CATEGORY, "YouTube", "TikTok", "X/Twitter", "Instagram", "数据处理"]


class ThreePlatformCrawlerQtApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("多平台数据爬取工具")
        self.resize(1040, 640)
        self.setMinimumSize(860, 560)

        self.tools = list(TOOLS)
        extra_categories = sorted({tool.category for tool in self.tools} - set(CATEGORY_ORDER))
        self.category_order = [*CATEGORY_ORDER, *extra_categories]
        self.filtered_tools = []
        self.processes: dict[str, QProcess] = {}
        self.current_category = ALL_CATEGORY

        self._build_ui()
        self._apply_style()
        self.refresh_tools()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 16, 18, 14)
        root_layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        self.title_label = QLabel("多平台数据爬取工具")
        self.title_label.setObjectName("titleLabel")
        self.subtitle_label = QLabel("集中启动 YouTube、TikTok、X/Twitter、Instagram 采集工具和数据处理工具")
        self.subtitle_label.setObjectName("subtitleLabel")
        title_box.addWidget(self.title_label)
        title_box.addWidget(self.subtitle_label)
        header.addLayout(title_box, 1)

        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("搜索工具、平台或标签")
        self.search_entry.textChanged.connect(self.refresh_tools)
        header.addWidget(self.search_entry, 0)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_tools)
        header.addWidget(refresh_btn)
        root_layout.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        for category in self.category_order:
            item = QListWidgetItem(self._category_label(category))
            item.setData(Qt.UserRole, category)
            self.nav.addItem(item)
        self.nav.currentItemChanged.connect(self._on_category_changed)
        splitter.addWidget(self.nav)

        center = QFrame()
        center.setObjectName("panel")
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(12, 12, 12, 12)
        center_layout.setSpacing(8)

        list_title = QLabel("工具列表")
        list_title.setObjectName("sectionTitle")
        center_layout.addWidget(list_title)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["工具", "分类"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.update_detail)
        self.table.itemDoubleClicked.connect(lambda *_: self.open_selected_tool())
        center_layout.addWidget(self.table, 1)
        splitter.addWidget(center)

        detail = QFrame()
        detail.setObjectName("panel")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(10)

        detail_title = QLabel("工具详情")
        detail_title.setObjectName("sectionTitle")
        detail_layout.addWidget(detail_title)

        self.detail_name = QLabel("未选择工具")
        self.detail_name.setObjectName("detailName")
        self.detail_name.setWordWrap(True)
        detail_layout.addWidget(self.detail_name)

        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("mutedLabel")
        self.detail_meta.setWordWrap(True)
        detail_layout.addWidget(self.detail_meta)

        self.detail_script = QLabel("")
        self.detail_script.setObjectName("scriptLabel")
        self.detail_script.setWordWrap(True)
        detail_layout.addWidget(self.detail_script)

        self.detail_summary = QTextEdit()
        self.detail_summary.setReadOnly(True)
        self.detail_summary.setObjectName("summaryBox")
        detail_layout.addWidget(self.detail_summary, 1)

        self.open_btn = QPushButton("打开工具")
        self.open_btn.setObjectName("primaryButton")
        self.open_btn.clicked.connect(self.open_selected_tool)
        detail_layout.addWidget(self.open_btn)
        splitter.addWidget(detail)

        splitter.setSizes([180, 520, 320])
        self.setCentralWidget(root)
        self.nav.setCurrentRow(0)

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        self.addAction(exit_action)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #eef2f7;
                color: #172033;
                font-family: "Microsoft YaHei UI", "Segoe UI";
                font-size: 9pt;
            }
            #titleLabel {
                font-size: 18pt;
                font-weight: 700;
                color: #111827;
            }
            #subtitleLabel, #mutedLabel {
                color: #667085;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 8px 10px;
                min-width: 260px;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 6px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #edf4ff;
                border-color: #b8cae8;
            }
            #primaryButton {
                background: #2563eb;
                border-color: #2563eb;
                color: white;
                padding: 10px 16px;
            }
            #primaryButton:hover {
                background: #1d4ed8;
            }
            #panel {
                background: #ffffff;
                border: 1px solid #d8e0eb;
                border-radius: 8px;
            }
            #sectionTitle {
                background: transparent;
                font-size: 11pt;
                font-weight: 700;
            }
            #detailName {
                background: transparent;
                font-size: 15pt;
                font-weight: 700;
                color: #111827;
            }
            #scriptLabel {
                color: #475467;
                background: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                padding: 7px;
            }
            #summaryBox {
                background: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                padding: 8px;
            }
            #navList {
                background: #182033;
                color: #cbd5e1;
                border: 0;
                border-radius: 8px;
                padding: 8px;
                font-weight: 600;
            }
            #navList::item {
                border-radius: 6px;
                padding: 10px;
                margin: 2px 0;
            }
            #navList::item:selected {
                background: #2563eb;
                color: white;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8fafc;
                border: 1px solid #e4eaf2;
                border-radius: 6px;
                gridline-color: #eef2f7;
                selection-background-color: #dcecff;
                selection-color: #172033;
            }
            QHeaderView::section {
                background: #f8fafc;
                color: #667085;
                border: 0;
                border-bottom: 1px solid #e4eaf2;
                padding: 8px;
                font-weight: 700;
            }
            """
        )
        self.table.setAlternatingRowColors(True)
        self.table.setFont(QFont("Microsoft YaHei UI", 9))

    def _category_label(self, category: str) -> str:
        if category == ALL_CATEGORY:
            return f"全部  {len(self.tools)}"
        count = sum(1 for tool in self.tools if tool.category == category)
        return f"{category}  {count}"

    def _on_category_changed(self, current: QListWidgetItem | None) -> None:
        self.current_category = current.data(Qt.UserRole) if current else ALL_CATEGORY
        self.refresh_tools()

    def refresh_tools(self) -> None:
        query = self.search_entry.text().strip().lower()
        category = self.current_category

        self.filtered_tools = []
        for tool in self.tools:
            if category != ALL_CATEGORY and tool.category != category:
                continue
            haystack = " ".join([tool.name, tool.category, tool.summary, " ".join(tool.tags)]).lower()
            if query and query not in haystack:
                continue
            self.filtered_tools.append(tool)

        self.table.setRowCount(len(self.filtered_tools))
        for row, tool in enumerate(self.filtered_tools):
            for column, text in enumerate([tool.name, tool.category]):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, tool.tool_id)
                self.table.setItem(row, column, item)

        if self.filtered_tools:
            self.table.selectRow(0)
        else:
            self.clear_detail()

    def selected_tool(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return None
        row = selected_rows[0].row()
        if row < 0 or row >= len(self.filtered_tools):
            return None
        return self.filtered_tools[row]

    def update_detail(self) -> None:
        tool = self.selected_tool()
        if tool is None:
            self.clear_detail()
            return
        self.detail_name.setText(tool.name)
        tags = " / ".join(tool.tags) if tool.tags else "无标签"
        self.detail_meta.setText(f"{tool.category}    {tags}")
        self.detail_script.setText(tool.implementation_path or tool.entrypoint)
        self.detail_summary.setPlainText(tool.summary)
        self.open_btn.setEnabled(True)
        self.open_btn.setText("打开工具")

    def clear_detail(self) -> None:
        self.detail_name.setText("未选择工具")
        self.detail_meta.setText("")
        self.detail_script.setText("")
        self.detail_summary.setPlainText("请选择左侧工具。")
        self.open_btn.setEnabled(False)

    def open_selected_tool(self) -> None:
        tool = self.selected_tool()
        if tool is None:
            return
        if self._is_tool_running(tool.tool_id):
            logger.info("Tool already running: %s (%s)", tool.name, tool.tool_id)
            QMessageBox.information(self, "工具已打开", f"{tool.name} 已经打开。")
            return

        logger.info("Launching tool process: %s (%s)", tool.name, tool.tool_id)
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(["-m", "src.studio.tool_runner", "--tool-id", tool.tool_id])
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.finished.connect(lambda exit_code, exit_status, tool_id=tool.tool_id: self._tool_finished(tool_id, exit_code, exit_status))
        process.errorOccurred.connect(lambda error, tool_id=tool.tool_id: self._tool_error(tool_id, error))
        process.readyReadStandardOutput.connect(lambda tool_id=tool.tool_id: self._read_tool_output(tool_id))
        self.processes[tool.tool_id] = process
        process.start()
        self.refresh_tools()

    def _read_tool_output(self, tool_id: str) -> None:
        process = self.processes.get(tool_id)
        if process is not None:
            text = bytes(process.readAllStandardOutput()).decode(errors="replace")
            if text:
                print(text, end="")
                sys.stdout.flush()

    def _tool_finished(self, tool_id: str, exit_code: int, exit_status) -> None:
        logger.info("Tool process finished: %s exit_code=%s exit_status=%s", tool_id, exit_code, exit_status)
        if tool_id in self.processes:
            self.processes.pop(tool_id, None)
            self.refresh_tools()

    def _tool_error(self, tool_id: str, error) -> None:
        logger.error("Tool process error: %s error=%s", tool_id, error)
        if tool_id in self.processes:
            self.processes.pop(tool_id, None)
            self.refresh_tools()

    def _is_tool_running(self, tool_id: str) -> bool:
        process = self.processes.get(tool_id)
        return bool(process and process.state() != QProcess.NotRunning)

    def closeEvent(self, event) -> None:
        running = [tool_id for tool_id in self.processes if self._is_tool_running(tool_id)]
        if running:
            message = "关闭主窗口会关闭已打开的工具窗口，确定关闭吗？"
        else:
            message = "确定关闭三平台数据爬取工具吗？"
        reply = QMessageBox.question(self, "确认关闭", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            event.ignore()
            return
        for process in list(self.processes.values()):
            if process.state() != QProcess.NotRunning:
                process.terminate()
        for process in list(self.processes.values()):
            if process.state() != QProcess.NotRunning:
                process.waitForFinished(1500)
                if process.state() != QProcess.NotRunning:
                    process.kill()
        event.accept()


def main() -> None:
    setup_console_logging()
    from src.core.config_store import generate_all_defaults
    try:
        generate_all_defaults()
    except OSError:
        pass
    logger.info("Starting main window")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("多平台数据爬取工具")
    window = ThreePlatformCrawlerQtApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
