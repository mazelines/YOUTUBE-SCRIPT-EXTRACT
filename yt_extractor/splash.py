"""High-DPI startup screen matching the extraction workspace."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QSplashScreen

from . import __version__
from .theme import ICON_DIR

_W, _H = 560, 330


def _font(painter: QPainter, size: int, *, bold: bool = False):
    font = QFont("Malgun Gothic")
    font.setPixelSize(size)
    font.setBold(bold)
    painter.setFont(font)


def _canvas(scale: float) -> QPixmap:
    pix = QPixmap(round(_W * scale), round(_H * scale))
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    # A dark ink card sets the startup screen apart from the light workspace.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0e1726"))
    p.drawRoundedRect(QRectF(0, 0, _W, _H), 14, 14)

    # The vector app icon remains crisp at every display scale.
    QSvgRenderer(str(ICON_DIR / "app-icon.svg")).render(p, QRectF(36, 32, 40, 40))
    _font(p, 11, bold=True)
    p.setPen(QColor("#8fa8f0"))
    p.drawText(QRectF(88, 32, 300, 40), Qt.AlignVCenter, "MAZELINE  /  VIDEO TO TEXT")
    _font(p, 12)
    p.setPen(QColor("#9aa8bd"))
    p.drawText(QRectF(424, 32, 100, 40), Qt.AlignRight | Qt.AlignVCenter,
               f"v{__version__}")

    _font(p, 30, bold=True)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(36, 108, 488, 44), Qt.AlignVCenter, "YouTube 자막 추출기")
    _font(p, 14)
    p.setPen(QColor("#b4c0d3"))
    p.drawText(QRectF(36, 154, 488, 28), Qt.AlignVCenter,
               "영상에서 필요한 내용을, 하나의 작업 공간에서.")

    x = 36
    _font(p, 11)
    for text in ("MD · SRT 자막", "MP3 · MP4", "AI 요약 · 번역"):
        width = p.fontMetrics().horizontalAdvance(text) + 24
        p.setPen(QColor("#2c3b55"))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(x + 0.5, 200.5, width - 1, 25), 12.5, 12.5)
        p.setPen(QColor("#d5deec"))
        p.drawText(QRectF(x, 200, width, 26), Qt.AlignCenter, text)
        x += width + 8

    _font(p, 12)
    p.setPen(QColor("#9aa8bd"))
    p.drawText(QRectF(36, 284, 488, 24), Qt.AlignVCenter,
               "작업 공간을 준비하고 있습니다…")
    p.end()
    return pix


class StartupSplash(QSplashScreen):
    """An indeterminate activity bar, with no invented loading percentage."""

    def __init__(self, scale: float):
        super().__init__(_canvas(scale), Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAccessibleName("YouTube 자막 추출기 시작 중")
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._advance)

    def _advance(self):
        self._phase = (self._phase + 1) % 100
        self.update()

    def drawContents(self, painter: QPainter):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        # Activity strip along the card's bottom edge, inset past the corners.
        track = QRectF(14, 326, _W - 28, 3)
        painter.setBrush(QColor("#1c2a40"))
        painter.drawRect(track)
        painter.setClipRect(track)
        painter.setBrush(QColor("#5b82f0"))
        x = track.x() - 140 + self._phase / 100 * (track.width() + 140)
        painter.drawRoundedRect(QRectF(x, track.y(), 140, 3), 1.5, 1.5)
        painter.restore()

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)


def make_splash() -> QSplashScreen:
    screen = QApplication.primaryScreen()
    splash = StartupSplash(screen.devicePixelRatio())
    available = screen.availableGeometry()
    splash.move(available.center() - splash.rect().center())
    return splash
