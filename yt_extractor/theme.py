"""Shared visual theme for production, screenshots and UI checks."""
from pathlib import Path

STYLESHEET = """
QWidget { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 12px;
          color: #24334b; }
QMainWindow, QDialog { background: #f4f6fa; }
QWidget#workspace { background: #f4f6fa; }
QFrame#card, QWidget#chatPanel { background: #ffffff; border: 1px solid #dfe5ee; border-radius: 10px; }
QLabel { background: transparent; }
QLabel#eyebrow { color: #3264da; font-size: 11px; font-weight: 700; }
QLabel#pageTitle { font-size: 23px; font-weight: 700; color: #17243a; }
QLabel#sectionTitle { font-size: 14px; font-weight: 700; color: #17243a; }
QLabel#muted, QLabel#settingsSummary { color: #64748b; }
QLabel#badge { background: #e8eefb; color: #345aac; border-radius: 5px; padding: 4px 8px; }
QLabel#emptyTitle { font-size: 20px; font-weight: 700; color: #334766; }
QLabel#emptyIcon { font-size: 32px; font-weight: 700; color: #3264da; }
QPushButton { background: #ffffff; color: #34445d; border: 1px solid #d5deeb;
              border-radius: 6px; padding: 7px 12px; font-weight: 600; }
QPushButton:hover { background: #eef3fc; border-color: #a8bfe9; }
QPushButton:pressed { background: #e0e9fa; }
QPushButton:checked { background: #eaf0ff; border-color: #3264da; color: #2455c1; }
QPushButton#primary, QPushButton#start_btn { background: #3264da; color: white; border-color: #3264da; }
QPushButton#primary:hover, QPushButton#start_btn:hover { background: #2854bd; }
QPushButton:disabled, QPushButton#primary:disabled, QPushButton#start_btn:disabled {
    background: #edf0f5; color: #8b97a8; border-color: #e2e7ef; }
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QTextBrowser:focus { border: 1px solid #3264da; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #d5deeb; border-radius: 6px;
    padding: 6px 8px; selection-background-color: #dce7ff; selection-color: #17243a; }
QLineEdit[readOnly="true"] { background: #f8fafc; color: #64748b; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #96a1b1; background: #f1f4f8; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow, QSpinBox::down-arrow { image: url(@ICONS@/chevron-down.svg); width: 12px; height: 12px; }
QSpinBox::up-arrow { image: url(@ICONS@/chevron-up.svg); width: 12px; height: 12px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; border: none; background: #edf1f7; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e7efff; selection-color: #24334b; }
QCheckBox { spacing: 7px; padding: 3px 0; }
QCheckBox:disabled { color: #96a1b1; }
QCheckBox:focus { background: #eaf0ff; border-radius: 3px; }
QGroupBox { border: 1px solid #dfe5ee; border-radius: 8px; background: white;
            margin-top: 14px; padding: 18px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #34445d; font-weight: 700; }
QTableWidget { background: white; alternate-background-color: #fafbfd; border: none;
               gridline-color: #edf1f6; selection-background-color: #eaf0ff; selection-color: #17243a; }
QTableWidget::item { padding: 7px; border-bottom: 1px solid #edf1f6; }
QTableWidget::item:selected { background: #eaf0ff; color: #17243a; }
QHeaderView::section { background: #f8fafc; color: #64748b; border: none;
                       border-bottom: 1px solid #e2e8f0; padding: 9px 7px; font-size: 11px; font-weight: 600; }
QProgressBar { background: #edf1f7; border: none; border-radius: 4px;
               min-height: 18px; max-height: 18px; color: #24334b; text-align: center; font-size: 10px; }
QProgressBar::chunk { background: #9cb9f8; border-radius: 4px; }
QProgressBar#overallProgress { min-height: 5px; max-height: 5px; }
QProgressBar#overallProgress::chunk { background: #3264da; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #64748b; padding: 9px 18px;
                border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:selected { color: #3264da; border-bottom-color: #3264da; }
QTabBar::tab:hover { background: #eef3fc; }
QDockWidget { font-weight: 600; }
QDockWidget::title { background: #e9eef7; padding: 8px; }
QScrollArea { border: none; background: transparent; }
QStatusBar { background: #f4f6fa; color: #64748b; font-size: 11px; }
QToolTip { background: #17243a; color: white; border: none; padding: 6px; }
"""


def apply_theme(app):
    app.setStyle("Fusion")
    # Fusion's native checkbox ticks and spin arrows remain legible in either
    # Windows appearance mode; the application itself uses a light palette.
    from PySide6.QtGui import QColor, QPalette
    palette = app.style().standardPalette()
    palette.setColor(QPalette.Window, QColor("#f4f6fa"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.Text, QColor("#24334b"))
    palette.setColor(QPalette.WindowText, QColor("#24334b"))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET.replace("@ICONS@", (Path(__file__).parent / "img").as_posix()))
