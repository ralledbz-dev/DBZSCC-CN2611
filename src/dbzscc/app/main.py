"""Desktop application entry point."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from dbzscc.ui.main_window import MainWindow


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("DBZSCC")
    app.setApplicationName("DBZSCC2611")
    app.setApplicationDisplayName("DBZSCC 2611")
    window = MainWindow()
    window.show()
    return app.exec()
