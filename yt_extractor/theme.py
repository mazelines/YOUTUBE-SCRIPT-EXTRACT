"""Shared visual theme for production, screenshots and UI checks."""
from pathlib import Path

ICON_DIR = Path(__file__).parent / "img"

# Design tokens. Status tones are (text, tint) pairs used for pills and bars.
ACCENT = "#2e5bdb"
INK = "#0e1726"
TEXT = "#2a3547"
MUTED = "#5f6b7c"
CANVAS = "#f2f4f7"
BORDER = "#e4e8ee"
# Machine-made values (URLs, paths, versions, percentages) use a fixed-width face.
MONO = "'Cascadia Mono', 'Consolas', monospace"
TONES = {
    "neutral": ("#4f5b6c", "#eef1f5"),
    "info": ("#1f45b0", "#ebf0fd"),
    "success": ("#12704b", "#e2f4ec"),
    "warning": ("#8a5600", "#fcf0d6"),
    "danger": ("#b42a36", "#fce7e9"),
}

STYLESHEET = """
QWidget { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 12px;
          color: #2a3547; }
QMainWindow, QDialog { background: #f2f4f7; }
QWidget#workspace { background: #f2f4f7; }
QFrame#appBar { background: #ffffff; border: none; border-bottom: 1px solid #e4e8ee; }
QLabel#appTitle { font-size: 15px; font-weight: 700; color: #0e1726; }
QLabel#version { font-family: @MONO@; font-size: 11px; color: #5f6b7c; background: #f2f4f7;
                 border: 1px solid #e4e8ee; border-radius: 6px; padding: 1px 6px; }
QFrame#card, QWidget#chatPanel { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 12px; }
QFrame#divider { background: #eef1f5; border: none; max-height: 1px; min-height: 1px; }
QLabel { background: transparent; }
QLabel#sectionTitle { font-size: 15px; font-weight: 700; color: #0e1726; }
QLabel#stepChip { background: #ebf0fd; color: #2e5bdb; border-radius: 6px;
                  padding: 2px 7px; font-family: @MONO@; font-size: 11px; font-weight: 600; }
QLabel#fieldLabel { color: #5f6b7c; font-weight: 600; min-width: 64px; }
QLabel#muted, QLabel#hint { color: #5f6b7c; }
QLabel#settingsSummary { color: #4f5b6c; }
QLabel#hint { font-size: 11px; }
QLabel#hint[tone="warning"] { color: #8a5600; }
QLabel#hint[tone="info"] { color: #1f45b0; }
QLabel#infoText { color: #1f45b0; font-size: 11px; }
QLabel#contextLabel { color: #5f6b7c; background: #f8f9fb; border: 1px solid #e4e8ee;
                      border-radius: 8px; padding: 7px 10px; }
QLabel#contextLabel[active="true"] { color: #12704b; background: #f2f8f5; border-color: #cde8db; }
QLabel#badge { background: #eef1f5; color: #4f5b6c; border-radius: 9px; padding: 1px 8px;
               font-family: @MONO@; font-size: 11px; font-weight: 600; }
QLabel#pill { border-radius: 10px; padding: 2px 10px; font-size: 11px; font-weight: 700;
              background: #eef1f5; color: #4f5b6c; }
QLabel#pill[tone="info"] { background: #ebf0fd; color: #1f45b0; }
QLabel#pill[tone="success"] { background: #e2f4ec; color: #12704b; }
QLabel#pill[tone="warning"] { background: #fcf0d6; color: #8a5600; }
QLabel#pill[tone="danger"] { background: #fce7e9; color: #b42a36; }
QLabel#emptyTitle { font-size: 19px; font-weight: 700; color: #0e1726; }
QLabel#emptyStep { background: #fbfcfd; border: 1px solid #e4e8ee; border-radius: 10px;
                   padding: 10px 14px; color: #4f5b6c; }
QLabel#overallText { color: #4f5b6c; font-size: 11px; font-weight: 600; }
QPushButton { background: #ffffff; color: #3a4659; border: 1px solid #d2d9e3;
              border-radius: 8px; padding: 7px 14px; font-weight: 600; min-height: 18px; }
QPushButton:hover { background: #f5f7fb; border-color: #b7c4d8; }
QPushButton:pressed { background: #e9eef6; }
QPushButton:checked { background: #ebf0fd; border-color: #2e5bdb; color: #1f45b0; }
QPushButton#primary, QPushButton#start_btn { background: #2e5bdb; color: white; border-color: #2e5bdb; }
QPushButton#primary:hover, QPushButton#start_btn:hover { background: #244bbd; border-color: #244bbd; }
QPushButton#primary:pressed, QPushButton#start_btn:pressed { background: #1f42a6; }
QPushButton#danger { color: #b42a36; border-color: #edb9bf; }
QPushButton#danger:hover { background: #fdf0f1; border-color: #e39ba3; }
QPushButton#subtle, QPushButton#barButton { background: transparent; border-color: transparent; color: #4f5b6c; }
QPushButton#subtle:hover, QPushButton#barButton:hover { background: #eef1f5; color: #0e1726; }
QPushButton#barButton:checked { background: #ebf0fd; border-color: #bfcdf5; color: #1f45b0; }
QPushButton#link { background: transparent; border: none; color: #2e5bdb; padding: 4px 6px; }
QPushButton#link:hover { color: #1f45b0; text-decoration: underline; }
QFrame#segmented { background: #f2f4f7; border-radius: 9px; }
QPushButton#segment { background: transparent; border: 1px solid transparent; border-radius: 7px;
                      padding: 5px 14px; color: #4f5b6c; font-weight: 500; }
QPushButton#segment:hover { color: #0e1726; }
QPushButton#segment:checked { background: #ffffff; border-color: #e4e8ee; color: #0e1726; font-weight: 700; }
QPushButton:disabled, QPushButton#primary:disabled, QPushButton#start_btn:disabled,
QPushButton#danger:disabled { background: #e9edf3; color: #6b7686; border-color: #e9edf3; }
QPushButton#subtle:disabled, QPushButton#segment:disabled { background: transparent;
    border-color: transparent; color: #a3adbb; }
QPushButton:focus { border: 1px solid #2e5bdb; }
QPushButton#primary:focus, QPushButton#start_btn:focus { border: 2px solid #9db8f5; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus,
QTextBrowser:focus { border: 1px solid #2e5bdb; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox {
    background: #ffffff; border: 1px solid #d2d9e3; border-radius: 8px;
    padding: 6px 9px; selection-background-color: #dce7ff; selection-color: #0e1726; }
QPlainTextEdit#urlInput { background: #f8f9fb; border: 1px dashed #c3cddb; border-radius: 10px;
                          padding: 8px 10px; }
QPlainTextEdit#urlInput:focus { background: #ffffff; border: 1px solid #2e5bdb; }
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #b7c4d8; }
QLineEdit[readOnly="true"] { background: #f8f9fb; color: #2a3547; border-color: #e4e8ee;
                             font-family: @MONO@; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled {
    color: #96a1b1; background: #f1f4f8; }
QTextBrowser#chatView { background: #f8f9fb; color: #2a3547; border-color: #e4e8ee; }
QComboBox { padding-right: 24px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow, QSpinBox::down-arrow { image: url(@ICONS@/chevron-down.svg); width: 12px; height: 12px; }
QSpinBox::up-arrow { image: url(@ICONS@/chevron-up.svg); width: 12px; height: 12px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; border: none; background: #f2f4f7; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #e4e9f1; }
QComboBox QAbstractItemView { background: white; border: 1px solid #e4e8ee; padding: 4px;
    selection-background-color: #ebf0fd; selection-color: #0e1726; outline: none; }
QCheckBox { spacing: 8px; padding: 3px 0; }
QCheckBox:disabled { color: #96a1b1; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #b8c4d6;
                       border-radius: 4px; background: #ffffff; }
QCheckBox::indicator:hover { border-color: #2e5bdb; }
QCheckBox::indicator:focus { border: 2px solid #9db8f5; }
QCheckBox::indicator:checked { background: #2e5bdb; border-color: #2e5bdb;
                               image: url(@ICONS@/check.svg); }
QCheckBox::indicator:disabled { background: #f1f4f8; border-color: #dbe2ec; }
QCheckBox::indicator:checked:disabled { background: #b9c7e4; border-color: #b9c7e4; }
QCheckBox#chip { background: #ffffff; border: 1px solid #d2d9e3; border-radius: 8px;
                 padding: 6px 12px 6px 10px; font-weight: 600; color: #3a4659; }
QCheckBox#chip:hover { border-color: #b7c4d8; }
QCheckBox#chip:checked { background: #ebf0fd; border-color: #2e5bdb; color: #1f45b0; }
QCheckBox#chip:disabled { background: #f1f4f8; border-color: #e4e8ee; }
QCheckBox#chip::indicator { width: 14px; height: 14px; }
QWidget#settingsBody { background: #ffffff; }
QWidget#settingsBody QCheckBox { spacing: 10px; padding: 5px 0; font-weight: 600; color: #0e1726; }
QWidget#settingsBody QCheckBox:disabled { color: #96a1b1; }
QWidget#settingsBody QCheckBox::indicator { width: 36px; height: 20px; border: none; border-radius: 10px;
                                            background: transparent; image: url(@ICONS@/switch-off.svg); }
QWidget#settingsBody QCheckBox::indicator:checked { image: url(@ICONS@/switch-on.svg); }
QWidget#settingsBody QCheckBox::indicator:disabled { image: url(@ICONS@/switch-off-disabled.svg); }
QWidget#settingsBody QCheckBox::indicator:checked:disabled { image: url(@ICONS@/switch-on-disabled.svg); }
QWidget#settingsBody QLabel#groupTitle { color: #5f6b7c; font-size: 11px; font-weight: 700; }
QDialog#settings { background: #ffffff; }
QFrame#dialogFooter { background: #f8f9fb; border: none; border-top: 1px solid #eef1f5; }
QGroupBox { border: 1px solid #e4e8ee; border-radius: 10px; background: white;
            margin-top: 14px; padding: 18px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #3a4659; font-weight: 700; }
QTableWidget { background: white; alternate-background-color: #ffffff; border: none;
               border-top: 1px solid #eef1f5; gridline-color: #f0f2f6;
               selection-background-color: #f3f6fe; selection-color: #0e1726; outline: none; }
QTableWidget::item { padding: 7px 12px; border-bottom: 1px solid #f0f2f6; }
QTableWidget::item:selected { background: #f3f6fe; color: #0e1726; }
QTableWidget::item:hover { background: #f8f9fb; }
QHeaderView::section { background: #f8f9fb; color: #5f6b7c; border: none;
                       border-bottom: 1px solid #eef1f5; padding: 9px 12px; font-size: 11px; font-weight: 600; }
QProgressBar { background: #eef1f5; border: none; border-radius: 3px;
               min-height: 18px; max-height: 18px; color: #2a3547; text-align: center; font-size: 10px; }
QProgressBar::chunk { background: #9cb6f3; border-radius: 3px; }
QProgressBar#rowProgress { margin: 16px 12px 16px 0; min-height: 0; max-height: 16777215; }
QProgressBar#rowProgress::chunk { background: #2e5bdb; }
QProgressBar#rowProgress[state="done"]::chunk { background: #2fa36f; }
QProgressBar#overallProgress, QProgressBar#slimProgress { min-height: 6px; max-height: 6px; border-radius: 3px; }
QProgressBar#overallProgress::chunk, QProgressBar#slimProgress::chunk { background: #2e5bdb; border-radius: 3px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; color: #5f6b7c; padding: 9px 18px;
                border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:selected { color: #2e5bdb; border-bottom-color: #2e5bdb; }
QTabBar::tab:hover { background: #f2f4f7; }
QDockWidget { font-weight: 700; color: #0e1726;
              titlebar-close-icon: url(@ICONS@/close.svg);
              titlebar-normal-icon: url(@ICONS@/undock.svg); }
QDockWidget::title { background: #f2f4f7; padding: 10px 12px; text-align: left; }
QDockWidget::close-button, QDockWidget::float-button { border: none; background: transparent;
                                                       padding: 0; border-radius: 4px; icon-size: 16px; }
QDockWidget::close-button:hover, QDockWidget::float-button:hover { background: #e4e9f1; }
QMenu { background: #ffffff; border: 1px solid #e4e8ee; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 7px 26px 7px 12px; border-radius: 5px; color: #2a3547; }
QMenu::item:selected { background: #f2f4f7; color: #0e1726; }
QMenu::item:disabled { color: #a3adbb; }
QMenu::separator { height: 1px; background: #eef1f5; margin: 4px 6px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #cdd5e1; border-radius: 3px; min-height: 32px; }
QScrollBar::handle:horizontal { background: #cdd5e1; border-radius: 3px; min-width: 32px; }
QScrollBar::handle:hover { background: #a9b5c7; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QStatusBar { background: #ffffff; color: #5f6b7c; font-size: 11px; border-top: 1px solid #e4e8ee; }
QStatusBar::item { border: none; }
QToolTip { background: #0e1726; color: white; border: none; padding: 6px 8px; border-radius: 6px; }
QMessageBox QLabel { color: #2a3547; }
"""


def repolish(widget):
    """Re-apply the stylesheet after a dynamic property (tone/state) changes."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def app_icon():
    from PySide6.QtGui import QIcon
    return QIcon(str(ICON_DIR / "app-icon.svg"))


def apply_theme(app):
    app.setStyle("Fusion")
    # Fusion's native checkbox ticks and spin arrows remain legible in either
    # Windows appearance mode; the application itself uses a light palette.
    from PySide6.QtGui import QColor, QPalette
    palette = app.style().standardPalette()
    palette.setColor(QPalette.Window, QColor(CANVAS))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.Text, QColor(TEXT))
    palette.setColor(QPalette.WindowText, QColor(TEXT))
    palette.setColor(QPalette.Highlight, QColor("#dce7ff"))
    palette.setColor(QPalette.HighlightedText, QColor(INK))
    palette.setColor(QPalette.PlaceholderText, QColor("#8a94a3"))
    app.setPalette(palette)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(STYLESHEET.replace("@ICONS@", ICON_DIR.as_posix())
                      .replace("@MONO@", MONO))
