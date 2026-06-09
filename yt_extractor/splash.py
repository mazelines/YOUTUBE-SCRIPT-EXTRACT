"""Splash screen for YouTube 자막 추출기 — Lime Forward B theme."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from yt_extractor import __version__

_W, _H = 520, 300


def make_splash() -> QSplashScreen:
    pix = QPixmap(_W, _H)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)

    # Cream background
    p.fillRect(0, 0, _W, _H, QColor("#f2ede4"))

    # Lime top accent
    p.fillRect(0, 0, _W, 8, QColor("#cde86a"))

    # Lime bottom accent
    p.fillRect(0, _H - 4, _W, 4, QColor("#cde86a"))

    # App name
    f = QFont("Malgun Gothic", 22, QFont.Bold)
    p.setFont(f)
    p.setPen(QColor("#2a2622"))
    p.drawText(0, 60, _W, 56, Qt.AlignCenter, "YouTube 자막 추출기")

    # Subtitle
    f2 = QFont("Malgun Gothic", 11)
    p.setFont(f2)
    p.setPen(QColor("#575451"))
    p.drawText(0, 126, _W, 32, Qt.AlignCenter, "자막 추출  ·  AI 요약  ·  한국어 번역")

    # Horizontal divider
    p.setPen(QColor("#ddd5c8"))
    p.drawLine(60, 186, _W - 60, 186)

    # Brand + version
    f3 = QFont("Malgun Gothic", 10)
    p.setFont(f3)
    p.setPen(QColor("#8a836f"))
    p.drawText(0, 198, _W, 30, Qt.AlignCenter, f"MazeLine  ·  v{__version__}")

    # Loading hint
    f4 = QFont("Malgun Gothic", 9)
    p.setFont(f4)
    p.setPen(QColor("#b3cf4e"))
    p.drawText(0, 250, _W, 24, Qt.AlignCenter, "로딩 중…")

    p.end()

    splash = QSplashScreen(pix, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)

    # Center on primary monitor
    primary = QApplication.primaryScreen().geometry()
    splash.move(
        primary.x() + (primary.width() - _W) // 2,
        primary.y() + (primary.height() - _H) // 2,
    )
    return splash
