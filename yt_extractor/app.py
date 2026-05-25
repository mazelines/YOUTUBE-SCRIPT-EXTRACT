"""PySide6 (Qt 6) GUI for batch YouTube transcript extraction.

Concurrency model: each video is one QRunnable submitted to a shared
QThreadPool. Workers never touch widgets directly — they emit signals that
Qt delivers on the GUI thread, which is the only thread-safe way to update
the UI in Qt.
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import (
    Qt, QObject, QRunnable, QThreadPool, Signal, Slot, QUrl,
)
from PySide6.QtGui import QDesktopServices, QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPlainTextEdit, QPushButton, QLabel, QLineEdit, QCheckBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QProgressBar,
    QGroupBox, QAbstractItemView, QMessageBox, QComboBox, QFrame,
)

from .core import (
    extract_video_id,
    extract_to_markdown,
    extract_audio_mp3,
    ExtractionError,
    TRANSCRIPT_TIMESTAMPED,
    TRANSCRIPT_SENTENCES,
    TRANSCRIPT_PARAGRAPHS,
)


# Columns of the job table.
COL_TITLE, COL_STATUS, COL_PROGRESS, COL_DETAIL, COL_FILE = range(5)

# Pulls a percentage out of an MP3 progress message (e.g. "… 45.2% 1.2MiB/s").
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# Status labels.
ST_PENDING = "대기 중"
ST_RUNNING = "진행 중"
ST_DONE = "완료"
ST_PARTIAL = "부분 완료"
ST_ERROR = "실패"

_STATUS_COLORS = {
    ST_PENDING: "#7a7a7a",
    ST_RUNNING: "#1769aa",
    ST_DONE: "#1b7f3b",
    ST_PARTIAL: "#b07000",
    ST_ERROR: "#b00020",
}


def _force_kill_process_tree():
    """Last-resort shutdown: kill this process and all of its children.

    yt-dlp/ffmpeg/aria2c run on QThreadPool threads and spawn child processes;
    none can always be stopped cooperatively. If a worker is stuck in such
    native work at exit, terminating the whole tree guarantees the app (and its
    aria2c/ffmpeg children) never lingers in Task Manager.
    """
    pid = os.getpid()
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                creationflags=0x08000000,  # CREATE_NO_WINDOW: no console flash
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    os._exit(0)


class WorkerSignals(QObject):
    """Signals emitted by an ExtractWorker, marshalled to the GUI thread."""

    status = Signal(int, str)             # row, status label
    detail = Signal(int, str)             # row, progress/error message
    finished = Signal(int, object)        # row, result dict {files, errors}


class ExtractWorker(QRunnable):
    """Extract a single video's transcript and/or MP3 audio.

    `outputs` selects what to produce: any of {"transcript", "audio"}.
    """

    def __init__(self, row: int, url: str, out_dir: str, outputs: set,
                 langs, prefer_manual: bool, transcript_format: str, bitrate: str,
                 should_cancel=None):
        super().__init__()
        self.row = row
        self.url = url
        self.out_dir = out_dir
        self.outputs = outputs
        self.langs = langs
        self.prefer_manual = prefer_manual
        self.transcript_format = transcript_format
        self.bitrate = bitrate
        self.should_cancel = should_cancel
        self.signals = WorkerSignals()

    def _cancelled(self) -> bool:
        return bool(self.should_cancel and self.should_cancel())

    @Slot()
    def run(self):
        row = self.row
        if self._cancelled():
            return
        self.signals.status.emit(row, ST_RUNNING)
        files, errors = [], []

        if "transcript" in self.outputs and not self._cancelled():
            try:
                path = extract_to_markdown(
                    self.url, self.out_dir,
                    preferred_langs=self.langs,
                    prefer_manual=self.prefer_manual,
                    transcript_format=self.transcript_format,
                    progress=lambda m: self.signals.detail.emit(row, f"[자막] {m}"),
                )
                files.append(("자막", str(path)))
            except ExtractionError as e:
                errors.append(("자막", str(e)))
            except Exception as e:
                errors.append(("자막", f"예기치 못한 오류: {e}"))

        if "audio" in self.outputs and not self._cancelled():
            try:
                path = extract_audio_mp3(
                    self.url, self.out_dir, bitrate=self.bitrate,
                    progress=lambda m: self.signals.detail.emit(row, f"[MP3] {m}"),
                    should_cancel=self.should_cancel,
                )
                files.append(("MP3", str(path)))
            except ExtractionError as e:
                errors.append(("MP3", str(e)))
            except Exception as e:
                errors.append(("MP3", f"예기치 못한 오류: {e}"))

        if errors and files:
            self.signals.status.emit(row, ST_PARTIAL)
        elif errors:
            self.signals.status.emit(row, ST_ERROR)
        else:
            self.signals.status.emit(row, ST_DONE)
        self.signals.finished.emit(row, {"files": files, "errors": errors})


BANNER_URL = "https://mazeline.tech/"


def _resource_base():
    """Base directory for bundled resources.

    Under a PyInstaller build, data files live under sys._MEIPASS; otherwise
    they sit next to this module.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "yt_extractor"
    return Path(__file__).resolve().parent


def _banner_image_path():
    """Return the company banner image path, or None if absent.

    Looks in yt_extractor/img/ for the supplied banner (any common format),
    resolving correctly both in source and in a frozen (PyInstaller) build.
    """
    img_dir = _resource_base() / "img"
    for name in ("mazelinebanner.jpg", "mazelinebanner.png", "banner.png",
                 "banner.jpg"):
        p = img_dir / name
        if p.exists():
            return p
    return None


class BannerWidget(QFrame):
    """Clickable company ad banner pinned to the bottom of the window.

    Shows the bundled banner image scaled to the window width (height-capped);
    falls back to a styled text banner if the image is missing. Clicking it
    opens the company website in the default browser.
    """

    def __init__(self, url: str = BANNER_URL, image_path=None,
                 max_height: int = 110):
        super().__init__()
        self.url = url
        self._pix = None
        self._max_h = max_height
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{url} 바로가기")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(0)
        self._label = QLabel(alignment=Qt.AlignCenter)
        self._label.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self._label)

        if image_path and Path(image_path).exists():
            pix = QPixmap(str(image_path))
            if not pix.isNull():
                self._pix = pix

        if self._pix is None:
            self._setup_text_fallback()
        else:
            self._rescale()

    def _setup_text_fallback(self):
        self._label.setTextFormat(Qt.RichText)
        self._label.setText(
            '<div style="background:#0d1b3e;color:#ffffff;padding:14px 18px;">'
            '<span style="font-size:17px;font-weight:bold;">MazeLine</span>'
            '&nbsp;&nbsp;&nbsp;게임 개발의 새로운 기준&nbsp;&nbsp;&nbsp;'
            f'<span style="color:#9ad0ff;">{self.url}</span></div>'
        )

    def _rescale(self):
        if self._pix is None:
            return
        w = max(self.width(), 1)
        scaled = self._pix.scaledToWidth(w, Qt.SmoothTransformation)
        if scaled.height() > self._max_h:
            scaled = self._pix.scaledToHeight(self._max_h, Qt.SmoothTransformation)
        self._label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(self.url))
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube 자막 추출기")
        self.resize(960, 640)

        self.pool = QThreadPool.globalInstance()
        self.out_dir = str(Path.cwd() / "transcripts")
        self._total = 0
        self._completed = 0
        self._running = False
        # Set on close to tell in-flight workers to abort promptly.
        self._shutdown = threading.Event()

        self._build_ui()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- Input group ---
        in_group = QGroupBox("영상 URL (한 줄에 하나씩 입력)")
        in_layout = QVBoxLayout(in_group)
        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText(
            "https://www.youtube.com/watch?v=...\n"
            "https://youtu.be/...\n"
            "여러 개를 줄바꿈으로 구분해 붙여넣으세요."
        )
        self.url_input.setMaximumHeight(120)
        in_layout.addWidget(self.url_input)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("목록에 추가")
        self.add_btn.clicked.connect(self.on_add_urls)
        btn_row.addWidget(self.add_btn)
        btn_row.addStretch(1)
        in_layout.addLayout(btn_row)
        root.addWidget(in_group)

        # --- Options group ---
        opt_group = QGroupBox("옵션")
        opt = QGridLayout(opt_group)

        opt.addWidget(QLabel("저장 폴더:"), 0, 0)
        self.dir_label = QLineEdit(self.out_dir)
        self.dir_label.setReadOnly(True)
        opt.addWidget(self.dir_label, 0, 1, 1, 2)
        self.dir_btn = QPushButton("변경…")
        self.dir_btn.clicked.connect(self.on_choose_dir)
        opt.addWidget(self.dir_btn, 0, 3)

        opt.addWidget(QLabel("선호 언어:"), 1, 0)
        self.lang_input = QLineEdit("ko, en")
        self.lang_input.setToolTip(
            "쉼표로 구분한 언어 코드 우선순위 (예: ko, en, ja)"
        )
        opt.addWidget(self.lang_input, 1, 1)

        opt.addWidget(QLabel("동시 작업 수:"), 1, 2)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 16)
        self.concurrency.setValue(4)
        opt.addWidget(self.concurrency, 1, 3)

        self.manual_cb = QCheckBox("수동 자막 우선")
        self.manual_cb.setChecked(True)
        self.manual_cb.setToolTip("끄면 자동 생성 자막도 동일하게 취급합니다.")
        opt.addWidget(self.manual_cb, 2, 0, 1, 2)

        opt.addWidget(QLabel("자막 형식:"), 2, 2)
        self.format_combo = QComboBox()
        self.format_combo.addItem("문장 단위 (번역·노션용)", TRANSCRIPT_SENTENCES)
        self.format_combo.addItem("단락 단위", TRANSCRIPT_PARAGRAPHS)
        self.format_combo.addItem("타임스탬프", TRANSCRIPT_TIMESTAMPED)
        self.format_combo.setToolTip(
            "문장/단락: 타임스탬프 없이 읽기 쉬운 본문. 타임스탬프: 자막 큐 단위."
        )
        opt.addWidget(self.format_combo, 2, 3)

        # Output-type selection.
        opt.addWidget(QLabel("추출 항목:"), 3, 0)
        out_row = QHBoxLayout()
        self.transcript_cb = QCheckBox("자막 (.md)")
        self.transcript_cb.setChecked(True)
        self.audio_cb = QCheckBox("MP3 음원")
        out_row.addWidget(self.transcript_cb)
        out_row.addWidget(self.audio_cb)
        out_row.addStretch(1)
        out_widget = QWidget()
        out_widget.setLayout(out_row)
        opt.addWidget(out_widget, 3, 1)

        opt.addWidget(QLabel("MP3 음질:"), 3, 2)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["128 kbps", "192 kbps", "320 kbps"])
        self.bitrate_combo.setCurrentIndex(1)  # 192
        opt.addWidget(self.bitrate_combo, 3, 3)

        # Enable/disable dependent controls with their output type.
        self.transcript_cb.toggled.connect(self._sync_output_controls)
        self.audio_cb.toggled.connect(self._sync_output_controls)
        self._sync_output_controls()

        root.addWidget(opt_group)

        # --- Job table ---
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["영상 / URL", "상태", "진행률", "상세", "저장 파일"]
        )
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.table.setColumnWidth(COL_PROGRESS, 100)
        hdr.setSectionResizeMode(COL_DETAIL, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(self.on_row_double_clicked)
        root.addWidget(self.table, stretch=1)

        # --- Action row ---
        action = QHBoxLayout()
        self.start_btn = QPushButton("추출 시작")
        self.start_btn.setStyleSheet("font-weight: bold; padding: 6px 16px;")
        self.start_btn.clicked.connect(self.on_start)
        action.addWidget(self.start_btn)

        self.clear_done_btn = QPushButton("완료 항목 지우기")
        self.clear_done_btn.clicked.connect(self.on_clear_done)
        action.addWidget(self.clear_done_btn)

        self.clear_all_btn = QPushButton("전체 지우기")
        self.clear_all_btn.clicked.connect(self.on_clear_all)
        action.addWidget(self.clear_all_btn)

        self.open_dir_btn = QPushButton("저장 폴더 열기")
        self.open_dir_btn.clicked.connect(self.on_open_dir)
        action.addWidget(self.open_dir_btn)

        action.addStretch(1)
        root.addLayout(action)

        # --- Progress + status bar ---
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)

        # --- Company ad banner (clickable) ---
        self.banner = BannerWidget(image_path=_banner_image_path())
        root.addWidget(self.banner)

        self.statusBar().showMessage("준비됨")

        self._paths_by_row: dict[int, str] = {}

    # ------------------------------------------------------------- helpers --
    def _sync_output_controls(self):
        want_t = self.transcript_cb.isChecked()
        want_a = self.audio_cb.isChecked()
        self.lang_input.setEnabled(want_t)
        self.manual_cb.setEnabled(want_t)
        self.format_combo.setEnabled(want_t)
        self.bitrate_combo.setEnabled(want_a)

    def _set_status_item(self, row: int, status: str):
        item = QTableWidgetItem(status)
        color = _STATUS_COLORS.get(status)
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, COL_STATUS, item)

    def _make_progress_bar(self) -> QProgressBar:
        """A compact per-row bar tracking that row's MP3 download/convert."""
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat("%p%")
        bar.setAlignment(Qt.AlignCenter)
        bar.setFixedHeight(16)
        return bar

    def _add_job_row(self, url: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, COL_TITLE, QTableWidgetItem(url))
        self._set_status_item(row, ST_PENDING)
        self.table.setCellWidget(row, COL_PROGRESS, self._make_progress_bar())
        self.table.setItem(row, COL_DETAIL, QTableWidgetItem(""))
        self.table.setItem(row, COL_FILE, QTableWidgetItem(""))

    def _row_status(self, row: int) -> str:
        item = self.table.item(row, COL_STATUS)
        return item.text() if item else ""

    # -------------------------------------------------------------- slots ---
    def on_add_urls(self):
        text = self.url_input.toPlainText()
        added, skipped = 0, 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                extract_video_id(line)  # validate
            except ExtractionError:
                skipped += 1
                continue
            self._add_job_row(line)
            added += 1
        self.url_input.clear()
        msg = f"{added}개 추가됨"
        if skipped:
            msg += f", {skipped}개는 인식 불가로 건너뜀"
        self.statusBar().showMessage(msg)

    def on_choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", self.out_dir)
        if d:
            self.out_dir = d
            self.dir_label.setText(d)

    def on_open_dir(self):
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir))

    def on_clear_done(self):
        for row in range(self.table.rowCount() - 1, -1, -1):
            if self._row_status(row) in (ST_DONE, ST_ERROR):
                self.table.removeRow(row)

    def on_clear_all(self):
        if self._running:
            QMessageBox.information(self, "진행 중", "작업이 끝난 뒤 지워주세요.")
            return
        self.table.setRowCount(0)
        self.progress.setValue(0)
        self.progress.setMaximum(0)

    def on_row_double_clicked(self, row: int, _col: int):
        paths = self._paths_by_row.get(row) or []
        for path in paths:
            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                return

    def _set_controls_enabled(self, enabled: bool):
        for w in (self.start_btn, self.add_btn, self.dir_btn,
                  self.concurrency, self.clear_all_btn,
                  self.transcript_cb, self.audio_cb):
            w.setEnabled(enabled)
        if enabled:
            self._sync_output_controls()  # re-apply per-output enable rules
        else:
            for w in (self.lang_input, self.manual_cb, self.format_combo,
                      self.bitrate_combo):
                w.setEnabled(False)

    def on_start(self):
        if self._running:
            return
        pending = [r for r in range(self.table.rowCount())
                   if self._row_status(r) == ST_PENDING]
        if not pending:
            QMessageBox.information(
                self, "작업 없음", "추출할 대기 항목이 없습니다. URL을 추가하세요."
            )
            return

        outputs = set()
        if self.transcript_cb.isChecked():
            outputs.add("transcript")
        if self.audio_cb.isChecked():
            outputs.add("audio")
        if not outputs:
            QMessageBox.information(
                self, "추출 항목 없음",
                "자막 또는 MP3 음원 중 하나 이상을 선택하세요."
            )
            return

        langs = [s.strip() for s in self.lang_input.text().split(",") if s.strip()]
        if not langs:
            langs = ["en"]
        prefer_manual = self.manual_cb.isChecked()
        transcript_format = self.format_combo.currentData()
        bitrate = self.bitrate_combo.currentText().split()[0]  # "192 kbps" -> "192"
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)

        self.pool.setMaxThreadCount(self.concurrency.value())

        self._running = True
        self._total = len(pending)
        self._completed = 0
        self.progress.setMaximum(self._total)
        self.progress.setValue(0)
        self._set_controls_enabled(False)
        self.statusBar().showMessage(f"{self._total}개 작업 시작…")

        for row in pending:
            url = self.table.item(row, COL_TITLE).text()
            worker = ExtractWorker(
                row, url, self.out_dir, outputs,
                langs, prefer_manual, transcript_format, bitrate,
                should_cancel=self._shutdown.is_set,
            )
            worker.signals.status.connect(self._on_worker_status)
            worker.signals.detail.connect(self._on_worker_detail)
            worker.signals.finished.connect(self._on_worker_finished)
            self.pool.start(worker)

    @Slot(int, str)
    def _on_worker_status(self, row: int, status: str):
        self._set_status_item(row, status)

    @Slot(int, str)
    def _on_worker_detail(self, row: int, msg: str):
        self.table.setItem(row, COL_DETAIL, QTableWidgetItem(msg))
        self._update_mp3_progress(row, msg)

    def _update_mp3_progress(self, row: int, msg: str):
        """Drive a row's MP3 bar from its [MP3] progress message.

        Tracks the download percentage; once downloading finishes ("변환 중")
        the encode step reports no progress, so we peg the bar to 100%.
        """
        if "[MP3]" not in msg:
            return
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is None:
            return
        m = _PCT_RE.search(msg)
        if m:
            bar.setValue(int(float(m.group(1))))
        elif "변환 중" in msg or "저장 완료" in msg:
            bar.setValue(100)

    @Slot(int, object)
    def _on_worker_finished(self, row: int, result: dict):
        files = result.get("files", [])
        errors = result.get("errors", [])

        if files:
            # Remember paths for double-click (open the first produced file).
            self._paths_by_row[row] = [p for _label, p in files]
            names = "  |  ".join(f"{lbl}: {Path(p).name}" for lbl, p in files)
            item = QTableWidgetItem(names)
            item.setToolTip(
                "\n".join(p for _l, p in files) + "\n(더블클릭하여 열기)"
            )
            self.table.setItem(row, COL_FILE, item)

        if errors:
            detail = "  /  ".join(f"[{lbl}] {msg}" for lbl, msg in errors)
        else:
            detail = "저장 완료"
        self.table.setItem(row, COL_DETAIL, QTableWidgetItem(detail))

        self._completed += 1
        self.progress.setValue(self._completed)
        self.statusBar().showMessage(
            f"진행: {self._completed} / {self._total}"
        )

        if self._completed >= self._total:
            self._running = False
            self._set_controls_enabled(True)
            statuses = [self._row_status(r) for r in range(self.table.rowCount())]
            n_done = statuses.count(ST_DONE)
            n_partial = statuses.count(ST_PARTIAL)
            n_error = statuses.count(ST_ERROR)
            self.statusBar().showMessage(
                f"완료: 성공 {n_done}개, 부분 완료 {n_partial}개, 실패 {n_error}개"
            )

    def closeEvent(self, event):
        # Confirm before abandoning in-flight work.
        if self._running:
            resp = QMessageBox.question(
                self, "작업 진행 중",
                "추출 작업이 진행 중입니다. 종료하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                event.ignore()
                return

        # Signal workers to abort, drop anything still queued, and give the
        # running ones a moment to unwind cooperatively.
        self._shutdown.set()
        self.pool.clear()
        event.accept()
        if self.pool.waitForDone(4000):
            return

        # A worker is stuck in uninterruptible native work (yt-dlp/ffmpeg/aria2c
        # can't always be cancelled). Without this the QThreadPool thread would
        # keep the interpreter — and the process — alive after the window closes.
        # Kill the whole process tree so nothing lingers.
        _force_kill_process_tree()


def _run_selftest() -> int:
    """Headless sanity check used to validate a packaged build.

    Verifies that bundled resources and runtime dependencies resolve in the
    (possibly frozen) environment: the banner image, the ffmpeg binary, and
    the yt-dlp import. Returns a process exit code; writes a result line to a
    file when SELFTEST_OUT is set (windowed builds have no stdout).
    """
    import os
    problems = []

    bp = _banner_image_path()
    if not bp or not Path(bp).exists():
        problems.append("banner image missing")

    try:
        from .core import _ffmpeg_location
        loc = _ffmpeg_location()
        exe = Path(loc) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not loc or not exe.exists():
            problems.append("ffmpeg binary not resolvable")
    except Exception as e:
        problems.append(f"ffmpeg setup error: {e}")

    try:
        import yt_dlp  # noqa: F401
    except Exception as e:
        problems.append(f"yt_dlp import failed: {e}")

    # Instantiate the GUI offscreen to confirm widgets/resources load.
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv)
        win = MainWindow()
        win.show()
        app.processEvents()
        if win.banner._pix is None and bp:
            problems.append("banner pixmap failed to load")
    except Exception as e:
        problems.append(f"GUI instantiation failed: {e}")

    result = "SELFTEST OK" if not problems else "SELFTEST FAIL: " + "; ".join(problems)
    out = os.environ.get("SELFTEST_OUT")
    if out:
        try:
            Path(out).write_text(result, encoding="utf-8")
        except OSError:
            pass
    print(result)
    return 0 if not problems else 1


def _run_screenshot(path: str) -> int:
    """Render the window to a PNG using Qt's own painter (correct fonts).

    Avoids OS screen-capture quirks; used to preview the UI. Returns exit code.
    """
    app = QApplication(sys.argv)
    win = MainWindow()
    win.resize(980, 720)
    win.url_input.setPlainText(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
        "https://youtu.be/9bZkp7q19f0"
    )
    win.on_add_urls()
    win.audio_cb.setChecked(True)
    win.show()
    for _ in range(5):
        app.processEvents()
    pix = win.grab()
    ok = pix.save(path)
    print(f"screenshot {'saved' if ok else 'FAILED'}: {path}")
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else "screenshot.png"
        sys.exit(_run_screenshot(out))
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube 자막 추출기")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
