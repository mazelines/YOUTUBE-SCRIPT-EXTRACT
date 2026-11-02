"""High-DPI startup screen matching the extraction workspace."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QSplashScreen

from . import __version__

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
    p.setPen(QColor("#dfe5ee"))
    p.setBrush(QColor("#ffffff"))
    p.drawRoundedRect(QRectF(0.5, 0.5, _W - 1, _H - 1), 14, 14)

    # Vector video/text mark remains crisp at every display scale.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#3264da"))
    p.drawRoundedRect(QRectF(32, 28, 40, 40), 10, 10)
    p.setBrush(QColor("#ffffff"))
    p.drawPolygon(QPolygonF([QPointF(45, 38), QPointF(45, 50), QPointF(56, 44)]))
    p.drawRoundedRect(QRectF(42, 55, 20, 2), 1, 1)
    _font(p, 11, bold=True)
    p.setPen(QColor("#3264da"))
    p.drawText(QRectF(84, 28, 300, 40), Qt.AlignVCenter, "MAZELINE  /  VIDEO TO TEXT")
    _font(p, 12)
    p.setPen(QColor("#64748b"))
    p.drawText(QRectF(430, 28, 98, 40), Qt.AlignRight | Qt.AlignVCenter,
               f"v{__version__}")

    _font(p, 28, bold=True)
    p.setPen(QColor("#17243a"))
    p.drawText(QRectF(32, 96, 496, 44), Qt.AlignVCenter, "YouTube 자막 추출기")
    _font(p, 13)
    p.setPen(QColor("#64748b"))
    p.drawText(QRectF(32, 145, 496, 28), Qt.AlignVCenter,
               "영상에서 필요한 내용을, 하나의 작업 공간에서.")

    for x, width, text in ((32, 104, "MD · SRT 자막"), (144, 94, "MP3 · MP4"),
                           (246, 106, "AI 요약 · 번역")):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#eef3fc"))
        p.drawRoundedRect(QRectF(x, 192, width, 28), 6, 6)
        _font(p, 11)
        p.setPen(QColor("#345aac"))
        p.drawText(QRectF(x, 192, width, 28), Qt.AlignCenter, text)

    p.setPen(QColor("#e2e8f0"))
    p.drawLine(32, 242, 528, 242)
    _font(p, 12)
    p.setPen(QColor("#64748b"))
    p.drawText(QRectF(32, 254, 496, 26), Qt.AlignVCenter,
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
        track = QRectF(32, 294, 496, 4)
        painter.setBrush(QColor("#edf1f7"))
        painter.drawRoundedRect(track, 2, 2)
        painter.setClipRect(track)
        painter.setBrush(QColor("#3264da"))
        x = 32 - 120 + self._phase / 100 * (496 + 120)
        painter.drawRoundedRect(QRectF(x, 294, 120, 4), 2, 2)
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
