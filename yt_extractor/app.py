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
import time
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import (
    Qt, QObject, QRunnable, QThreadPool, Signal, Slot, QUrl, QTimer,
    QStandardPaths,
)
from PySide6.QtGui import QDesktopServices, QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QLabel,
    QTableWidgetItem, QFileDialog, QProgressBar, QMessageBox, QFrame,
    QTextBrowser, QTabWidget,
)

from .core import (
    extract_video_id,
    extract_to_markdown,
    extract_to_markdown_with_fallback,
    extract_audio_mp3,
    extract_video,
    ExtractionError,
    TRANSCRIPT_TIMESTAMPED,
    TRANSCRIPT_SENTENCES,
    TRANSCRIPT_PARAGRAPHS,
)
from .llm import (
    build_messages, LLMError, MAX_CONTEXT_CHARS,
    split_for_translation, transcript_body, TRANSLATE_SYSTEM_PROMPT,
)
from .local_llm import (
    is_model_present, download_model, stream_local_chat, ensure_loaded,
    is_loaded,
)
from .chat_render import render_conversation
from .gpu_runtime import runtime_summary


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
    ST_PENDING: "#64748b",
    ST_RUNNING: "#3264da",
    ST_DONE: "#16805d",
    ST_PARTIAL: "#b07000",
    ST_ERROR: "#c23e48",
}


def _force_kill_process_tree():
    """Last-resort shutdown: kill this process and all of its children.

    yt-dlp/ffmpeg run on QThreadPool threads and spawn child processes;
    none can always be stopped cooperatively. If a worker is stuck in such
    native work at exit, terminating the whole tree guarantees the app (and its
    ffmpeg children) never lingers in Task Manager.
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

    status = Signal(int, str)             # job ID, status label
    detail = Signal(int, str)             # job ID, progress/error message
    finished = Signal(int, object)        # job ID, result dict {files, errors}


class ExtractWorker(QRunnable):
    """Extract a single video's transcript and/or MP3 audio.

    `outputs` selects what to produce: any of {"transcript", "audio", "video"}.
    """

    def __init__(self, job_id: int, url: str, out_dir: str, outputs: set,
                 langs, prefer_manual: bool, transcript_format: str, bitrate: str,
                 translate_to: str | None = None, should_cancel=None,
                 video_height: int = 1080,
                 stt_fallback: bool = False, stt_model_size: str = "small",
                 poll_wait_min: float = 0.0,
                 live_capture_minutes: float = 0.0):
        super().__init__()
        self.job_id = job_id
        self.url = url
        self.out_dir = out_dir
        self.outputs = outputs
        self.langs = langs
        self.prefer_manual = prefer_manual
        self.transcript_format = transcript_format
        self.bitrate = bitrate
        self.video_height = video_height
        self.translate_to = translate_to
        self.should_cancel = should_cancel
        self.stt_fallback = stt_fallback
        self.stt_model_size = stt_model_size
        self.poll_wait_min = poll_wait_min
        self.live_capture_minutes = live_capture_minutes
        self.signals = WorkerSignals()

    def _cancelled(self) -> bool:
        return bool(self.should_cancel and self.should_cancel())

    @Slot()
    def run(self):
        job_id = self.job_id
        if self._cancelled():
            return
        self.signals.status.emit(job_id, ST_RUNNING)
        files, errors = [], []

        if "transcript" in self.outputs and not self._cancelled():
            try:
                # Use the fallback pipeline whenever STT or polling is
                # requested; otherwise fall back to the original one-shot
                # extract_to_markdown to keep behaviour identical for users
                # who don't opt in.
                if self.stt_fallback or self.poll_wait_min > 0:
                    paths = extract_to_markdown_with_fallback(
                        self.url, self.out_dir,
                        preferred_langs=self.langs,
                        prefer_manual=self.prefer_manual,
                        transcript_format=self.transcript_format,
                        translate_to=self.translate_to,
                        stt_fallback=self.stt_fallback,
                        stt_model_size=self.stt_model_size,
                        poll_wait_min=self.poll_wait_min,
                        live_capture_minutes=self.live_capture_minutes,
                        progress=lambda m: self.signals.detail.emit(job_id, m),
                        should_cancel=self.should_cancel,
                    )
                else:
                    paths = extract_to_markdown(
                        self.url, self.out_dir,
                        preferred_langs=self.langs,
                        prefer_manual=self.prefer_manual,
                        transcript_format=self.transcript_format,
                        translate_to=self.translate_to,
                        progress=lambda m: self.signals.detail.emit(job_id, f"[자막] {m}"),
                    )
                for i, p in enumerate(paths):
                    if p.suffix.lower() == ".srt":
                        label = "SRT (STT)" if p.name.endswith(".stt.srt") else "SRT 원본"
                    else:
                        label = "자막" if i == 0 else f"번역({self.translate_to})"
                    files.append((label, str(p)))
            except ExtractionError as e:
                errors.append(("자막", str(e)))
            except Exception as e:
                errors.append(("자막", f"예기치 못한 오류: {e}"))

        if "audio" in self.outputs and not self._cancelled():
            try:
                path = extract_audio_mp3(
                    self.url, self.out_dir, bitrate=self.bitrate,
                    progress=lambda m: self.signals.detail.emit(job_id, f"[MP3] {m}"),
                    should_cancel=self.should_cancel,
                )
                files.append(("MP3", str(path)))
            except ExtractionError as e:
                errors.append(("MP3", str(e)))
            except Exception as e:
                errors.append(("MP3", f"예기치 못한 오류: {e}"))

        if "video" in self.outputs and not self._cancelled():
            try:
                path = extract_video(
                    self.url, self.out_dir, height=self.video_height,
                    progress=lambda m: self.signals.detail.emit(job_id, f"[영상] {m}"),
                    should_cancel=self.should_cancel,
                )
                files.append(("MP4", str(path)))
            except ExtractionError as e:
                errors.append(("MP4", str(e)))
            except Exception as e:
                errors.append(("MP4", f"예기치 못한 오류: {e}"))

        if errors and files:
            self.signals.status.emit(job_id, ST_PARTIAL)
        elif errors:
            self.signals.status.emit(job_id, ST_ERROR)
        else:
            self.signals.status.emit(job_id, ST_DONE)
        self.signals.finished.emit(job_id, {"files": files, "errors": errors})


class BuiltinSignals(QObject):
    """Signals for the bundled CPU model worker (download + inference)."""

    status = Signal(str)          # high-level phase, e.g. "모델 로딩 중…"
    progress = Signal(object, object)   # downloaded, total bytes (first-run download)
    chunk = Signal(int, int)      # current, total chunks (chunked translation)
    token = Signal(str)           # streamed text delta
    done = Signal()
    cancelled = Signal()
    error = Signal(str)


class BuiltinWorker(QRunnable):
    """Run the built-in model off the GUI thread.

    On first use the model isn't on disk, so this downloads it (reporting
    progress) before loading and streaming. Cancellation aborts the download or
    the generation, whichever is in flight.

    Two modes:
      - single request: pass `messages` (the usual chat-completion array).
      - chunked translation: pass `chunks` (a list of transcript pieces); each
        is translated as an independent request under the strict
        TRANSLATE_SYSTEM_PROMPT and the results are streamed back as one
        continuous answer. Used so a long transcript can be fully translated
        within the model's n_ctx instead of being truncated. See
        llm.split_for_translation.
    """

    def __init__(self, messages=None, *, chunks=None):
        super().__init__()
        self.messages = messages
        self.chunks = chunks
        self.signals = BuiltinSignals()
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @Slot()
    def run(self):
        try:
            if not is_model_present():
                self.signals.status.emit("내장 모델 다운로드 중… (최초 1회)")
                download_model(
                    on_progress=lambda d, t: self.signals.progress.emit(d, t),
                    should_cancel=self._cancel.is_set,
                )
            if self._cancel.is_set():
                self.signals.cancelled.emit()
                return
            # The model is normally preloaded at startup, so this is just
            # inference. Only say "로딩 중" if it actually isn't resident yet
            # (e.g. preload hasn't finished) — otherwise it looks like a reload.
            if is_loaded():
                self.signals.status.emit(f"{runtime_summary()} · 처리 중…")
            else:
                self.signals.status.emit("모델 로딩 중… (최초 1회는 시간이 걸립니다)")
            if self.chunks is not None:
                self._run_chunked()
            else:
                stream_local_chat(
                    self.messages,
                    on_token=lambda t: self.signals.token.emit(t),
                    should_cancel=self._cancel.is_set,
                )
        except LLMError as e:
            self.signals.error.emit(str(e))
            return
        except Exception as e:  # pragma: no cover - defensive
            self.signals.error.emit(f"예기치 못한 오류: {e}")
            return
        if self._cancel.is_set():
            self.signals.cancelled.emit()
        else:
            self.signals.done.emit()

    def _run_chunked(self):
        """Translate each chunk as its own request, streamed as one answer.

        A short status note ("번역 중… (i/total)") is emitted at each chunk
        boundary so the user sees progress through a long translation; chunks
        are joined with a blank line so the result reads as one document.
        """
        total = len(self.chunks)
        for i, chunk in enumerate(self.chunks, 1):
            if self._cancel.is_set():
                break
            self.signals.chunk.emit(i, total)
            messages = build_messages(chunk, system_prompt=TRANSLATE_SYSTEM_PROMPT)
            stream_local_chat(
                messages,
                on_token=lambda t: self.signals.token.emit(t),
                should_cancel=self._cancel.is_set,
            )
            if i < total and not self._cancel.is_set():
                self.signals.token.emit("\n\n")


class PreloadWorker(QRunnable):
    """Eagerly download+load the built-in model at startup, off the GUI thread.

    The model then stays resident (cached in local_llm) for the process
    lifetime, so the first summary has no load delay.
    """

    def __init__(self):
        super().__init__()
        self.signals = BuiltinSignals()
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @Slot()
    def run(self):
        try:
            ensure_loaded(
                on_status=lambda m: self.signals.status.emit(m),
                on_progress=lambda d, t: self.signals.progress.emit(d, t),
                should_cancel=self._cancel.is_set,
            )
        except LLMError as e:
            self.signals.error.emit(str(e))
            return
        except Exception as e:  # pragma: no cover - defensive
            self.signals.error.emit(f"내장 모델 준비 실패: {e}")
            return
        self.signals.done.emit()


BANNER_URL = "https://www.mazeline.tech"


def _default_output_dir() -> str:
    """Return a writable default output folder.

    Installed builds run from Program Files, so a cwd-relative default can make
    extraction fail when the app tries to create or write transcripts there.
    Prefer Documents, then AppData, and only fall back to cwd if it is the first
    writable option in the current environment.
    """
    candidates = []
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    if docs:
        candidates.append(Path(docs) / "YouTubeTranscriptExtractor" / "transcripts")

    app_data = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
    if app_data:
        candidates.append(Path(app_data) / "transcripts")

    candidates.extend([
        Path.home() / "YouTubeTranscriptExtractor" / "transcripts",
        Path.cwd() / "transcripts",
    ])

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
            return str(candidate)
        except OSError:
            continue

    return str(candidates[0])


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
    """Compact brand footer with a keyboard-accessible website link."""

    def __init__(self, url: str = BANNER_URL, image_path=None, max_height=32):
        super().__init__()
        self.url = url
        self._pix = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        brand = QLabel("MazeLine  ·  영상에서 지식으로")
        brand.setObjectName("muted")
        lay.addWidget(brand)
        lay.addStretch()
        self.link = QPushButton("www.mazeline.tech ↗")
        self.link.setFlat(True)
        self.link.setCursor(Qt.PointingHandCursor)
        self.link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.url)))
        lay.addWidget(self.link)


SUMMARY_PROMPT = "첨부된 자막을 핵심 위주로 한국어로 요약해 주세요."
TRANSLATE_PROMPT = "첨부된 자막을 자연스러운 한국어로 번역해 주세요."

# Braille spinner frames for the in-progress indicator.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class ChatTab(QWidget):
    """One conversation strand (요약 or 번역) inside a parent ChatPanel.

    Each tab keeps its own history, view, input, and worker so summarizing on
    one tab and translating on the other don't interleave in a single chat
    log. The shared provider config and attached transcript live on the parent
    panel and are pulled in here on send.
    """

    def __init__(self, panel: "ChatPanel", quick_label: str, quick_prompt: str,
                 placeholder: str = ""):
        super().__init__()
        self.panel = panel
        self.quick_prompt = quick_prompt
        self._history: list[dict] = []
        self._worker: BuiltinWorker | None = None
        self._streaming_assistant: str | None = None
        self._last_error: str | None = None
        self._translation_path: str | None = None
        # Coalesce stream-driven re-renders (~80 ms) so a fast model doesn't
        # trigger one setHtml() per token.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._rerender)
        # Spinner + elapsed clock for the in-progress indicator.
        self._spin_i = 0
        self._start_ts = 0.0
        self._status_base = ""
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._tick_progress)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)

        # Agent-style progress: a spinner+status line over a progress bar,
        # pinned above the chat view and shown only while a response is in
        # flight. For chunked translation the bar is determinate (i/total);
        # for single-request work (summary, external LLM) it stays marquee.
        self._prog_label = QLabel("")
        self._prog_label.setStyleSheet("color: #1769aa; font-size: 11px;")
        self._prog_bar = QProgressBar()
        self._prog_bar.setTextVisible(True)
        self._prog_box = QWidget()
        prog_lay = QVBoxLayout(self._prog_box)
        prog_lay.setContentsMargins(0, 0, 0, 4)
        prog_lay.setSpacing(2)
        prog_lay.addWidget(self._prog_label)
        prog_lay.addWidget(self._prog_bar)
        self._prog_box.setVisible(False)
        lay.addWidget(self._prog_box)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        # Force a light canvas regardless of system palette — chat bubbles are
        # tuned for a light page.
        self.view.setStyleSheet(
            "QTextBrowser { background-color: #f8fafc; color: #24334b; }"
        )
        self.view.setPlaceholderText(placeholder)
        lay.addWidget(self.view, stretch=1)

        quick = QHBoxLayout()
        self.quick_btn = QPushButton(quick_label)
        self.quick_btn.clicked.connect(self.run_quick_prompt)
        quick.addWidget(self.quick_btn)
        quick.addStretch(1)
        lay.addLayout(quick)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("결과에 대한 추가 질문 입력 (Ctrl+Enter: 질문하기)")
        self.input.setMaximumHeight(80)
        self.input.installEventFilter(self)
        lay.addWidget(self.input)

        send_row = QHBoxLayout()
        self.send_btn = QPushButton("질문하기")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self.on_send_clicked)
        send_row.addWidget(self.send_btn)
        self.stop_btn = QPushButton("중지")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        send_row.addWidget(self.stop_btn)
        self.reset_btn = QPushButton("대화 초기화")
        self.reset_btn.clicked.connect(self.on_reset)
        send_row.addWidget(self.reset_btn)
        send_row.addStretch(1)
        lay.addLayout(send_row)

    # Ctrl+Enter (or Cmd+Enter) sends; plain Enter inserts a newline.
    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == event.Type.KeyPress:
            if (event.key() in (Qt.Key_Return, Qt.Key_Enter)
                    and event.modifiers() & Qt.ControlModifier):
                self.on_send_clicked()
                return True
        return super().eventFilter(obj, event)

    def run_quick_prompt(self):
        if not self.panel._attached:
            QMessageBox.information(
                self, "자막 없음",
                "먼저 왼쪽 목록에서 자막 항목을 선택하고 'AI 요약' 또는 "
                "'AI 번역'을 눌러 불러오세요.",
            )
            return
        self._send(self.quick_prompt,
                   translate_full=(self is self.panel.translate_tab))

    def on_send_clicked(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        # Follow-up question on the summary/translation result: send as a plain
        # chat turn over history + transcript context. No manual_instruction
        # prepend, no chunking — the model answers the question asked.
        self._send(text)

    def _send(self, text: str, *, translate_full: bool = False):
        if self._worker is not None:
            QMessageBox.information(self, "응답 대기 중",
                                    "현재 답변이 끝난 뒤 보내주세요.")
            return
        panel = self.panel
        self._translation_path = panel._attached_path if translate_full else None
        # Initial translation (quick button or 'AI 번역'): translate the
        # transcript body (header metadata excluded) under the strict
        # translation-only prompt, split into chunks so a long video can't
        # overflow the model's n_ctx and get truncated. Results stream back as
        # one continuous answer. Follow-up questions (질문하기) never take this
        # path — they go through the normal chat-completion array, so the model
        # answers the question instead of re-translating the whole transcript.
        chunks = None
        if translate_full and panel._attached:
            body = transcript_body("\n\n".join(t for _, t in panel._attached if t))
            chunks = split_for_translation(body) or None

        messages = build_messages(text, history=self._history,
                                  transcripts=panel._attached,
                                  max_chars=MAX_CONTEXT_CHARS)
        self._history.append({"role": "user", "content": text})
        # Empty in-flight assistant turn — _rerender() shows it as a bubble
        # with a streaming cursor until tokens fill it in.
        self._streaming_assistant = ""
        self._last_error = None
        self._set_busy(True)
        self._rerender()

        worker = (BuiltinWorker(chunks=chunks) if chunks is not None
                  else BuiltinWorker(messages))
        worker.signals.status.connect(panel._on_builtin_status)
        worker.signals.progress.connect(panel._on_builtin_progress)
        worker.signals.chunk.connect(self._on_chunk)
        worker.signals.token.connect(self._on_token)
        worker.signals.done.connect(self._on_done)
        worker.signals.cancelled.connect(self._on_cancelled)
        worker.signals.error.connect(self._on_error)
        self._worker = worker
        panel.start_chat_worker(worker)

    def on_stop(self):
        if self._worker is not None:
            self._worker.cancel()

    def on_reset(self):
        if self._worker is not None:
            return
        self._history.clear()
        self._streaming_assistant = None
        self._last_error = None
        self._rerender()

    def cancel_worker(self):
        if self._worker is not None:
            self._worker.cancel()

    @Slot(str)
    def _on_token(self, chunk: str):
        self.panel._hide_builtin_ui()  # generation has begun
        if self._streaming_assistant is None:
            self._streaming_assistant = ""
        self._streaming_assistant += chunk
        if not self._render_timer.isActive():
            self._render_timer.start(80)

    @Slot()
    def _on_done(self):
        self.panel._hide_builtin_ui()
        self._render_timer.stop()
        answer = self._streaming_assistant or ""
        if answer:
            self._history.append({"role": "assistant", "content": answer})
        self._streaming_assistant = None
        self._worker = None
        self._set_busy(False)
        self._rerender()
        if self._translation_path:
            self.panel._maybe_auto_save_translation(self._translation_path, answer)
        self._translation_path = None

    @Slot()
    def _on_cancelled(self):
        self._on_error("작업이 취소되었습니다.")

    @Slot(str)
    def _on_error(self, msg: str):
        self._translation_path = None
        self.panel._hide_builtin_ui()
        self._render_timer.stop()
        # Drop the user turn we optimistically recorded so retry isn't doubled.
        if self._history and self._history[-1]["role"] == "user":
            self._history.pop()
        self._streaming_assistant = None
        self._last_error = msg
        self._worker = None
        self._set_busy(False)
        self._rerender()

    def _tick_progress(self):
        self._spin_i = (self._spin_i + 1) % len(_SPINNER)
        el = int(time.monotonic() - self._start_ts)
        self._prog_label.setText(
            f"{_SPINNER[self._spin_i]} {self._status_base}  경과 {el // 60}:{el % 60:02d}"
        )

    @Slot(int, int)
    def _on_chunk(self, i: int, total: int):
        self._status_base = f"청크 {i}/{total} 번역 중…"
        self._prog_bar.setRange(0, total)
        self._prog_bar.setValue(i)
        self._prog_bar.setFormat(f"{i}/{total}")

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.quick_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        if busy:
            self._status_base = "처리 중…"
            self._start_ts = time.monotonic()
            self._spin_i = 0
            self._prog_bar.setRange(0, 0)   # marquee until chunk info arrives
            self._prog_bar.setFormat("")
            self._prog_box.setVisible(True)
            self._tick_progress()
            self._spin_timer.start(120)
        else:
            self._spin_timer.stop()
            self._prog_box.setVisible(False)

    def _rerender(self):
        streaming = self._streaming_assistant is not None
        turns = list(self._history)
        if streaming:
            turns.append({"role": "assistant",
                          "content": self._streaming_assistant or ""})
        html_doc = render_conversation(
            turns, streaming=streaming, error=self._last_error,
        )
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 8
        prev = sb.value()
        self.view.setHtml(html_doc)        # setHtml resets scroll to the top
        # Follow the newest text when pinned to the bottom; otherwise restore
        # the user's place so they can read/scroll up mid-stream (without this,
        # the per-token re-render yanks them back to the top every ~80 ms, which
        # also makes manual scrolling impossible).
        sb.setValue(sb.maximum() if at_bottom else min(prev, sb.maximum()))


class ChatPanel(QWidget):
    """Right-side AI chat: summarize / translate / Q&A over transcripts.

    Hosts two independent conversation tabs (요약 / 번역) that share the
    attached transcript. Each tab keeps its own history so the two workflows
    don't pollute each other. Only the bundled in-process model is supported.
    """

    def __init__(self, main_window: "MainWindow"):
        super().__init__()
        self.mw = main_window
        # Single pool keeps built-in preload/inference serialized so the
        # multi-GB model is never loaded twice concurrently.
        self.model_pool = QThreadPool()
        self.model_pool.setMaxThreadCount(1)
        self._attached: list[tuple] = []       # (name, text) — shared by tabs
        self._attached_path: str | None = None  # full path of the loaded .md
        self._preload_worker: PreloadWorker | None = None
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)

        self.setMinimumWidth(360)
        title = QLabel("자막을 더 깊이 읽는 공간")
        title.setObjectName("sectionTitle")
        lay.addWidget(title)
        info = QLabel("내장 AI로 요약하고, 번역하고, 질문하세요.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        info.setToolTip("첫 사용 시 모델을 다운로드합니다. GPU가 있으면 자동 가속하며, 없으면 CPU를 사용합니다.")
        lay.addWidget(info)

        # --- Context indicator ---
        # Transcripts enter the conversation via the left pane's "AI로 요약" /
        # "AI로 번역" button, which reads the .md the extractor just produced
        # — so there's no manual file-picking here. This label just shows what's
        # loaded; it's shared between tabs since both operate on the same .md.
        self.attach_label = QLabel(
            "작업 결과에서 AI 요약 또는 AI 번역을 선택하세요."
        )
        self.attach_label.setStyleSheet("color: #7a7a7a;")
        self.attach_label.setWordWrap(True)
        self.attach_label.setTextFormat(Qt.PlainText)
        lay.addWidget(self.attach_label)

        # First-run model download / load indicator (hidden until needed) —
        # shared because either tab's send can be the trigger.
        self.builtin_status = QLabel("")
        self.builtin_status.setWordWrap(True)
        self.builtin_status.setStyleSheet("color: #1769aa; font-size: 11px;")
        self.builtin_status.setVisible(False)
        lay.addWidget(self.builtin_status)
        self.dl_bar = QProgressBar()
        self.dl_bar.setTextVisible(True)
        self.dl_bar.setVisible(False)
        lay.addWidget(self.dl_bar)

        # --- Tabs: separate conversations for summary vs translation ---
        self.tabs = QTabWidget()
        self.summary_tab = ChatTab(
            self, "자막 요약 시작", SUMMARY_PROMPT,
            placeholder=(
                "왼쪽에서 자막을 추출해 'AI 요약'을 누르거나, "
                "여기에 질문을 입력하세요."
            ),
        )
        self.translate_tab = ChatTab(
            self, "자막 번역 시작", TRANSLATE_PROMPT,
            placeholder=(
                "왼쪽에서 자막을 추출해 'AI 번역'을 누르세요. "
                "번역 결과에 대한 추가 질문은 여기에 입력하세요."
            ),
        )
        self.tabs.addTab(self.summary_tab, "요약")
        self.tabs.addTab(self.translate_tab, "번역")
        lay.addWidget(self.tabs, stretch=1)

    # ----------------------------------------------------------- context ---
    def load_transcript(self, name: str, text: str, *,
                        summarize: bool = False, translate: bool = False,
                        path: str | None = None):
        """Load a transcript as the chat context and optionally fire a prompt.

        Driven by the left pane's "AI로 요약" / "AI로 번역" buttons; switches to
        the matching tab and kicks off the default prompt for it. The attached
        transcript is shared — both tabs see the same .md.
        """
        target = (self.summary_tab if summarize else
                  self.translate_tab if translate else None)
        changed = self._attached != [(name, text)] or self._attached_path != path
        # Both tabs share context; never replace it while either is running.
        if ((changed and any(t._worker is not None for t in
                            (self.summary_tab, self.translate_tab)))
                or (target is not None and target._worker is not None)):
            QMessageBox.information(
                self, "응답 대기 중", "현재 답변이 끝난 뒤 다시 시도하세요."
            )
            return
        if changed:
            self.summary_tab.on_reset()
            self.translate_tab.on_reset()
        self._attached = [(name, text)]
        self._attached_path = path
        self._refresh_context_label()
        if summarize:
            self.tabs.setCurrentWidget(self.summary_tab)
            self.summary_tab._send(SUMMARY_PROMPT)
        elif translate:
            self.tabs.setCurrentWidget(self.translate_tab)
            self.translate_tab._send(TRANSLATE_PROMPT, translate_full=True)

    def _refresh_context_label(self):
        if not self._attached:
            self.attach_label.setText(
                "작업 결과에서 AI 요약 또는 AI 번역을 선택하세요."
            )
            self.attach_label.setStyleSheet("color: #7a7a7a;")
            return
        names = ", ".join(n for n, _ in self._attached)
        self.attach_label.setText(f"분석 중인 자막: {names}")
        self.attach_label.setStyleSheet("color: #1b7f3b;")

    def _maybe_auto_save_translation(self, source_path: str, text: str):
        """Save only a completed full translation to its original source path."""
        if not self.mw.translate_cb.isChecked() or not text.strip():
            return
        text = text.strip()
        try:
            src = Path(source_path)
            out_path = src.with_suffix(".ko.md")
            out_path.write_text(text, encoding="utf-8")
            self.mw._register_translation_output(source_path, str(out_path))
            self.mw.statusBar().showMessage(f"번역 저장됨: {out_path.name}", 5000)
        except OSError as e:
            self.mw.statusBar().showMessage(f"번역 저장 실패: {e}", 5000)

    # ----------------------------------------------------- builtin model UI --
    @Slot(str)
    def _on_builtin_status(self, msg: str):
        self.builtin_status.setVisible(True)
        self.builtin_status.setText(msg)
        if "로딩" in msg or "워밍업" in msg:
            # Model load / GPU warmup have no measurable progress, so show an
            # indeterminate (marquee) bar.
            self.dl_bar.setRange(0, 0)
            self.dl_bar.setFormat("")
            self.dl_bar.setVisible(True)

    @Slot(object, object)
    def _on_builtin_progress(self, done: int, total: int):
        self.dl_bar.setVisible(True)
        if total > 0:
            self.dl_bar.setRange(0, 100)
            self.dl_bar.setValue(min(100, max(0, done * 100 // total)))
            self.dl_bar.setFormat(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB  (%p%)")
        else:
            self.dl_bar.setRange(0, 0)  # indeterminate (unknown size)

    def _hide_builtin_ui(self):
        self.builtin_status.setVisible(is_loaded())
        self.builtin_status.setText(runtime_summary())
        self.dl_bar.setVisible(False)

    def start_chat_worker(self, worker: BuiltinWorker):
        self.model_pool.start(worker)

    # --------------------------------------------------------- preload ---
    def preload_model(self):
        """Start loading the built-in model now (called once at app startup).

        Downloads it first if needed; the loaded model then stays resident for
        the whole session, so summaries don't pay a load delay later.
        """
        if self._preload_worker is not None:
            return
        w = PreloadWorker()
        w.signals.status.connect(self._on_builtin_status)
        w.signals.progress.connect(self._on_builtin_progress)
        w.signals.done.connect(self._on_preload_done)
        w.signals.error.connect(self._on_preload_error)
        self._preload_worker = w
        self.model_pool.start(w)

    @Slot()
    def _on_preload_done(self):
        self._preload_worker = None
        self.dl_bar.setVisible(False)
        self.builtin_status.setVisible(True)
        self.builtin_status.setText(runtime_summary())

    @Slot(str)
    def _on_preload_error(self, msg: str):
        self._preload_worker = None
        self.dl_bar.setVisible(False)
        self.builtin_status.setVisible(True)
        self.builtin_status.setText(f"내장 모델 미사용: {msg.splitlines()[0]}")

    def shutdown(self, timeout: int = 4000) -> bool:
        """Cancel any in-flight workers (both tabs + preload) and drain the pool.

        Called on app close. Returns True if the pool drained within `timeout`.
        The model-load step is native and uncancellable, so a False here lets
        MainWindow fall back to force-killing the process tree.
        """
        for tab in (self.summary_tab, self.translate_tab):
            tab.cancel_worker()
        if self._preload_worker is not None:
            self._preload_worker.cancel()
        self.model_pool.clear()
        return self.model_pool.waitForDone(timeout)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube 자막 추출기")
        self.resize(1180, 860)
        self.setMinimumSize(680, 440)

        self.pool = QThreadPool.globalInstance()
        self.out_dir = _default_output_dir()
        self._total = 0
        self._completed = 0
        self._running = False
        # Set on close to tell in-flight workers to abort promptly.
        self._shutdown = threading.Event()
        # Whether pywhispercpp imports (probed in a worker thread to avoid
        # blocking startup; defaults to False until the probe finishes).
        self._stt_available = False

        self._build_ui()
        self._probe_stt_async()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_initial_size_applied", False):
            self._initial_size_applied = True
            # Windows can change the scale factor when the native window is
            # first created. Fit logical dimensions after that transition.
            QTimer.singleShot(0, self._fit_initial_window)

    def _fit_initial_window(self):
        available = self.screen().availableGeometry()
        self.resize(min(1180, available.width() - 32),
                    min(860, available.height() - 60))
        self.move(available.center() - self.rect().center())

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        from .ui_layout import build_workspace
        build_workspace(self, ChatPanel, BannerWidget)

    def _show_settings(self):
        screen = self.screen().availableGeometry()
        self.settings_dialog.resize(min(560, screen.width() - 40),
                                    min(680, screen.height() - 60))
        self.settings_dialog.exec()

    def _update_settings_summary(self, *_):
        fmt = ("단락" if self._fmt_paragraph.isChecked() else
               "타임스탬프" if self._fmt_timestamp.isChecked() else "문장")
        parts = [f"동시 {self.concurrency.value()}개"]
        if self.transcript_cb.isChecked():
            parts = [self.lang_input.text().strip() or "en", fmt] + parts
            if self.manual_cb.isChecked():
                parts.append("수동 자막 우선")
            if self.translate_cb.isChecked():
                parts.append("한국어 저장")
            if self.stt_cb.isChecked() and self._stt_available:
                parts.append(f"음성 인식 {self.stt_model_combo.currentText().split()[0]}")
            if self.stt_poll_spin.value():
                parts.append(f"자막 대기 {self.stt_poll_spin.value()}분")
        self.settings_summary.setText(" · ".join(parts))

    def _toggle_ai(self, checked=False):
        self._show_ai(checked)

    def _show_ai(self, visible=True):
        if visible:
            # Keep the extraction workspace usable on a small display.
            if self.width() < 1100 and not self.ai_dock.isFloating():
                self.ai_dock.setFloating(True)
                screen = self.screen().availableGeometry()
                self.ai_dock.resize(min(460, screen.width() - 40),
                                    min(720, screen.height() - 60))
                self.ai_dock.move(screen.center() - self.ai_dock.rect().center())
            self.ai_dock.show()
            self.resizeDocks([self.ai_dock], [410], Qt.Horizontal)
            self.ai_dock.raise_()
        else:
            self.ai_dock.hide()

    def _refresh_queue(self):
        count = self.table.rowCount()
        self.queue_count.setText(f"{count}개")
        self.queue_stack.setCurrentIndex(1 if count else 0)
        self.start_btn.setEnabled(not self._running and any(
            self._row_status(r) == ST_PENDING for r in range(count)))
        self.clear_all_btn.setEnabled(bool(count) and not self._running)
        self.clear_done_btn.setEnabled(any(self._row_status(r) in
            (ST_DONE, ST_PARTIAL, ST_ERROR) for r in range(count)))
        self._refresh_result()

    def _refresh_result(self):
        selected = self.table.selectionModel().selectedRows()
        row = selected[0].row() if selected else None
        paths = []
        if row is None or self.table.item(row, COL_TITLE) is None:
            self.result_title.setText("03   작업 결과")
            self.result_detail.setText("목록에서 작업을 선택하면 진행 상황과 저장 파일을 확인할 수 있습니다.")
        else:
            status = self._row_status(row)
            self.result_title.setText(f"03   작업 결과 · {status}")
            item = self.table.item(row, COL_DETAIL)
            self.result_detail.setText(item.text() if item and item.text() else "추출 시작을 기다리고 있습니다.")
            paths = self._paths_by_job.get(self._job_id(row), [])
        current_paths = [self.file_combo.itemData(i) for i in range(self.file_combo.count())]
        if paths != current_paths:
            previous = self.file_combo.currentData()
            self.file_combo.clear()
            for path in paths:
                self.file_combo.addItem(f"{Path(path).suffix[1:].upper()}  ·  {Path(path).name}", path)
            index = self.file_combo.findData(previous) if previous else -1
            if index >= 0:
                self.file_combo.setCurrentIndex(index)
        self.file_actions.setVisible(bool(paths))
        has_md = any(p.lower().endswith(".md") and Path(p).is_file() for p in paths)
        self.summarize_btn.setEnabled(has_md)
        self.translate_ai_btn.setEnabled(has_md)

    def _open_result_file(self):
        path = self.file_combo.currentData()
        if not path:
            return
        if not Path(path).is_file():
            QMessageBox.information(self, "파일 없음", "파일이 이동되거나 삭제되었습니다. 저장 폴더를 확인하세요.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_result_folder(self):
        path = self.file_combo.currentData()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))

    def _register_translation_output(self, source_path: str, output_path: str):
        for paths in self._paths_by_job.values():
            if source_path in paths and output_path not in paths:
                paths.append(output_path)
        self._refresh_result()

    # ------------------------------------------------------------- helpers --
    def _sync_output_controls(self):
        want_t = self.transcript_cb.isChecked() and not self._running
        want_a = self.audio_cb.isChecked() and not self._running
        self.lang_input.setEnabled(want_t)
        self.manual_cb.setEnabled(want_t)
        for b in (self._fmt_sentence, self._fmt_paragraph, self._fmt_timestamp):
            b.setEnabled(want_t)
        self.translate_cb.setEnabled(want_t)
        self.bitrate_combo.setEnabled(want_a)
        self.quality_combo.setEnabled(self.video_cb.isChecked() and not self._running)
        self.bitrate_combo.setVisible(self.audio_cb.isChecked())
        self.bitrate_label.setVisible(self.audio_cb.isChecked())
        self.quality_combo.setVisible(self.video_cb.isChecked())
        self.quality_label.setVisible(self.video_cb.isChecked())
        self.quality_options.setVisible(self.audio_cb.isChecked() or self.video_cb.isChecked())
        # STT controls only make sense when transcript output is selected.
        stt_enabled = want_t and self._stt_available
        self.stt_cb.setEnabled(stt_enabled)
        stt_active = stt_enabled and self.stt_cb.isChecked()
        # Polling is independent of STT — it waits for YouTube to publish
        # captions. It only needs transcript output on, so it follows want_t
        # rather than stt_active (otherwise turning STT off would lock the
        # polling value too, even though polling doesn't need STT at all).
        self.stt_poll_spin.setEnabled(want_t)
        # Live capture and model size feed STT, so they stay gated on STT on.
        self.stt_live_spin.setEnabled(stt_active)
        self.stt_model_combo.setEnabled(stt_active)
        self._update_settings_summary()

    def _probe_stt_async(self):
        """Detect pywhispercpp off the UI thread so import cost never blocks.

        Importing pywhispercpp loads the whisper.cpp native extension (can be
        slow on a cold HDD). We run it on a worker thread and flip
        ``_stt_available`` + re-sync controls on the GUI thread when done. If
        unavailable, the STT checkbox stays disabled with its tooltip
        explaining how to install it.

        The signal QObject is kept alive on ``self`` (not just on the worker)
        because Qt deletes C++ signal sources when their Python wrapper is
        garbage-collected — a worker-local signal can vanish before the
        cross-thread emit is delivered.
        """
        from PySide6.QtCore import QObject as _QObj

        class _ProbeSignal(_QObj):
            done = Signal(bool)

        class _Probe(QRunnable):
            def __init__(inner, signal):
                super().__init__()
                inner.sig = signal

            @Slot()
            def run(inner):
                try:
                    from .stt import is_stt_available
                    avail = is_stt_available()
                except Exception:
                    avail = False
                try:
                    inner.sig.done.emit(avail)
                except RuntimeError:
                    # Window closed while we probed — the signal's C++ side
                    # is already deleted. Nothing to deliver to; drop it.
                    pass

        # Keep a strong reference on self for the worker's lifetime.
        self._stt_probe_sig = _ProbeSignal()
        self._stt_probe_sig.done.connect(self._on_stt_probe)
        self.pool.start(_Probe(self._stt_probe_sig))

    @Slot(bool)
    def _on_stt_probe(self, available: bool):
        # The probe runs on a background thread; the user may close the window
        # while it's still importing pywhispercpp (slow on a cold HDD). By
        # the time the queued `done` signal is delivered, either this MainWindow
        # or one of the widgets below can already be torn down — accessing them
        # raises RuntimeError ("wrapped C/C++ object of type X has been
        # deleted") or, worse, aborts the process from PySide6 internals.
        # Guard every widget touch so a late probe result never crashes exit.
        try:
            self._stt_available = bool(available)
            self.stt_hint.setText(
                "자막이 없을 때만 음성을 분석합니다. 첫 사용 시 모델을 다운로드합니다."
                if available else "음성 인식 구성요소가 없어 사용할 수 없습니다."
            )
            if not self._stt_available:
                self.stt_cb.setChecked(False)
                self.stt_cb.setToolTip(
                    "STT 구성요소(pywhispercpp)가 설치되어 있지 않아 STT 폴백을 "
                    "사용할 수 없습니다.\n설치: requirements-stt.txt의 안내 참고"
                )
            self._sync_output_controls()
        except RuntimeError:
            # Window/widgets already destroyed by shutdown — drop the late
            # probe result silently.
            pass

    def _set_status_item(self, row: int, status: str):
        item = QTableWidgetItem(status)
        color = _STATUS_COLORS.get(status)
        if color:
            item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, COL_STATUS, item)

    def _make_progress_bar(self) -> QProgressBar:
        """A compact per-row bar tracking that row's MP3 download/convert."""
        bar = QProgressBar()
        bar.setObjectName("rowProgress")
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat("%p%")
        bar.setAlignment(Qt.AlignCenter)
        bar.setFixedHeight(20)
        return bar

    def _add_job_row(self, url: str):
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(url)
        item.setToolTip(url)
        item.setData(Qt.UserRole, self._next_job_id)
        item.setData(Qt.UserRole + 1, url)
        self._next_job_id += 1
        self.table.setItem(row, COL_TITLE, item)
        self._set_status_item(row, ST_PENDING)
        self.table.setCellWidget(row, COL_PROGRESS, self._make_progress_bar())
        self.table.setItem(row, COL_DETAIL, QTableWidgetItem(""))
        self.table.setItem(row, COL_FILE, QTableWidgetItem(""))
        self._refresh_queue()

    def _job_id(self, row: int) -> int:
        return self.table.item(row, COL_TITLE).data(Qt.UserRole)

    def _job_row(self, job_id: int) -> int | None:
        for row in range(self.table.rowCount()):
            if self._job_id(row) == job_id:
                return row
        return None

    def _row_status(self, row: int) -> str:
        item = self.table.item(row, COL_STATUS)
        return item.text() if item else ""

    # -------------------------------------------------------------- slots ---
    def on_add_urls(self):
        if self._running:
            return
        text = self.url_input.toPlainText()
        added, skipped = 0, 0
        invalid = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                extract_video_id(line)  # validate
            except ExtractionError:
                skipped += 1
                invalid.append(line)
                continue
            self._add_job_row(line)
            added += 1
        self.url_input.setPlainText("\n".join(invalid))
        if added:
            self.table.selectRow(self.table.rowCount() - 1)
        msg = f"{added}개 추가됨"
        if skipped:
            msg += f", 인식할 수 없는 {skipped}개 링크는 입력창에서 확인하세요"
        self.statusBar().showMessage(msg)

    def on_choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", self.out_dir)
        if d:
            self.out_dir = d
            self.dir_label.setText(d)
            self.dir_label.setCursorPosition(0)
            self.dir_label.setToolTip(d)

    def on_open_dir(self):
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.out_dir))

    def on_clear_done(self):
        for row in range(self.table.rowCount() - 1, -1, -1):
            if self._row_status(row) in (ST_DONE, ST_PARTIAL, ST_ERROR):
                self._paths_by_job.pop(self._job_id(row), None)
                self.table.removeRow(row)
        self._refresh_queue()

    def on_clear_all(self):
        if self._running:
            QMessageBox.information(self, "진행 중", "작업이 끝난 뒤 지워주세요.")
            return
        self.table.setRowCount(0)
        self._paths_by_job.clear()
        self.progress.setValue(0)
        self.progress.setMaximum(1)
        self._refresh_queue()

    def on_row_double_clicked(self, row: int, _col: int):
        paths = self._paths_by_job.get(self._job_id(row)) or []
        for path in paths:
            if path and Path(path).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                return

    def _selected_transcript_path(self):
        """Use the selected job only; fall back to newest only without a selection."""
        selected = [i.row() for i in self.table.selectionModel().selectedRows()]
        order = selected or list(range(self.table.rowCount() - 1, -1, -1))
        seen = set()
        for row in order:
            if row in seen:
                continue
            seen.add(row)
            for p in self._paths_by_job.get(self._job_id(row), []):
                if p.lower().endswith(".md") and Path(p).exists():
                    return p
        return None

    def on_ai_summarize(self):
        self._ai_dispatch_transcript(summarize=True)

    def on_ai_translate(self):
        self._ai_dispatch_transcript(translate=True)

    def _ai_dispatch_transcript(self, *, summarize: bool = False,
                                translate: bool = False):
        path = self._selected_transcript_path()
        if not path:
            action = "요약" if summarize else "번역"
            QMessageBox.information(
                self, "자막 없음",
                f"{action}할 자막이 없습니다. 먼저 자막(.md)을 추출한 뒤, "
                "표에서 항목을 선택하고 다시 누르세요.",
            )
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "읽기 실패", str(e))
            return
        self._show_ai()
        self.chat.load_transcript(
            Path(path).name, text, summarize=summarize, translate=translate,
            path=path,
        )

    def _set_controls_enabled(self, enabled: bool):
        for w in (self.start_btn, self.add_btn, self.dir_btn,
                  self.concurrency, self.clear_all_btn, self.url_input,
                  self.transcript_cb, self.audio_cb, self.video_cb):
            w.setEnabled(enabled)
        if enabled:
            self._sync_output_controls()  # re-apply per-output enable rules
        else:
            for w in (self.lang_input, self.manual_cb, self._fmt_sentence,
                      self._fmt_paragraph, self._fmt_timestamp,
                      self.translate_cb, self.bitrate_combo, self.quality_combo,
                      self.stt_cb, self.stt_poll_spin,
                      self.stt_live_spin, self.stt_model_combo):
                w.setEnabled(False)
        self._refresh_queue()

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
        if self.video_cb.isChecked():
            outputs.add("video")
        if not outputs:
            QMessageBox.information(
                self, "추출 항목 없음",
                "자막 · MP3 음원 · MP4 영상 중 하나 이상을 선택하세요."
            )
            return

        langs = [s.strip() for s in self.lang_input.text().split(",") if s.strip()]
        if not langs:
            langs = ["en"]
        prefer_manual = self.manual_cb.isChecked()
        if self._fmt_paragraph.isChecked():
            transcript_format = TRANSCRIPT_PARAGRAPHS
        elif self._fmt_timestamp.isChecked():
            transcript_format = TRANSCRIPT_TIMESTAMPED
        else:
            transcript_format = TRANSCRIPT_SENTENCES
        bitrate = self.bitrate_combo.currentText().split()[0]  # "192 kbps" -> "192"
        # "1080p"->1080, "720p"->720, "원본(최고화질)"->0 (no cap)
        q = self.quality_combo.currentText()
        video_height = int(q[:-1]) if q.endswith("p") else 0
        translate_to = "ko" if self.translate_cb.isChecked() else None
        # STT fallback options. Only active when the user opted in *and* the
        # backend is importable — otherwise pass disabled values so the worker
        # takes the legacy one-shot path.
        want_transcript = self.transcript_cb.isChecked()
        stt_fallback = want_transcript and self.stt_cb.isChecked() and self.stt_cb.isEnabled()
        # Map the human-readable combo entry back to a model size token
        # Whisper understands ("small (권장)" -> "small").
        stt_label = self.stt_model_combo.currentText().split(" ")[0]
        if stt_label not in ("tiny", "base", "small", "medium", "large-v3"):
            stt_label = "small"
        # Polling and STT are INDEPENDENT: a user may want one without the
        # other. Polling is only meaningful when transcript output is selected;
        # otherwise it'd wait on captions for nothing. We do NOT tie it to the
        # STT checkbox — that was the old bug, where enabling STT silently
        # forced a 30-minute poll first.
        poll_wait_min = float(self.stt_poll_spin.value()) if want_transcript else 0.0
        # Live capture only makes sense together with STT (it's the audio
        # source for STT on a live stream), so it stays gated on stt_fallback.
        live_capture_minutes = (
            float(self.stt_live_spin.value()) if stt_fallback else 0.0
        )
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
            title_item = self.table.item(row, COL_TITLE)
            url = title_item.data(Qt.UserRole + 1) or title_item.text()
            worker = ExtractWorker(
                self._job_id(row), url, self.out_dir, outputs,
                langs, prefer_manual, transcript_format, bitrate,
                translate_to=translate_to,
                should_cancel=self._shutdown.is_set,
                video_height=video_height,
                stt_fallback=stt_fallback,
                stt_model_size=stt_label,
                poll_wait_min=poll_wait_min,
                live_capture_minutes=live_capture_minutes,
            )
            worker.signals.status.connect(self._on_worker_status)
            worker.signals.detail.connect(self._on_worker_detail)
            worker.signals.finished.connect(self._on_worker_finished)
            self.pool.start(worker)

    @Slot(int, str)
    def _on_worker_status(self, job_id: int, status: str):
        row = self._job_row(job_id)
        if row is not None:
            self._set_status_item(row, status)
            bar = self.table.cellWidget(row, COL_PROGRESS)
            if status == ST_RUNNING:
                bar.setRange(0, 0)
                bar.setFormat("")
            elif status in (ST_DONE, ST_PARTIAL, ST_ERROR):
                bar.setRange(0, 100)
                bar.setValue(100 if status == ST_DONE else 0)
                bar.setFormat("완료" if status == ST_DONE else "확인 필요")
            self._refresh_queue()

    @Slot(int, str)
    def _on_worker_detail(self, job_id: int, msg: str):
        row = self._job_row(job_id)
        if row is None:
            return
        item = QTableWidgetItem(msg)
        item.setToolTip(msg)
        self.table.setItem(row, COL_DETAIL, item)
        self._update_row_progress(row, msg)
        self._refresh_result()

    def _update_row_progress(self, row: int, msg: str):
        """Show measured phase progress; unknown phases remain indeterminate."""
        bar = self.table.cellWidget(row, COL_PROGRESS)
        if bar is None:
            return
        m = _PCT_RE.search(msg)
        if m:
            bar.setRange(0, 100)
            bar.setFormat("%p%")
            bar.setValue(min(100, int(float(m.group(1)))))
        else:
            bar.setRange(0, 0)
            bar.setFormat("")

    @Slot(int, object)
    def _on_worker_finished(self, job_id: int, result: dict):
        row = self._job_row(job_id)
        files = result.get("files", [])
        errors = result.get("errors", [])

        if files and row is not None:
            title_item = self.table.item(row, COL_TITLE)
            title_item.setText(Path(files[0][1]).stem.removesuffix(".stt"))
            # Remember paths for double-click (open the first produced file).
            self._paths_by_job[job_id] = [p for _label, p in files]
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
        if row is not None:
            item = QTableWidgetItem(detail)
            item.setToolTip(detail)
            self.table.setItem(row, COL_DETAIL, item)

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
        self._refresh_queue()

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
        # running ones a moment to unwind cooperatively. The chat panel has its
        # own pool (and its own download/inference worker), so drain both.
        self._shutdown.set()
        self.pool.clear()
        # Disconnect the STT probe signal proactively: a probe worker may still
        # be importing pywhispercpp when the window closes, and its queued
        # `done` emit would otherwise land on a torn-down MainWindow. Even
        # though _on_stt_probe guards with try/except, dropping the connection
        # here means the emit becomes a no-op instead of relying on the slot
        # to catch the fallout. Best-effort — the signal/widget may already be
        # gone if closeEvent fires twice.
        try:
            if getattr(self, "_stt_probe_sig", None) is not None:
                self._stt_probe_sig.done.disconnect(self._on_stt_probe)
        except (RuntimeError, AttributeError):
            pass
        event.accept()
        ext_done = self.pool.waitForDone(4000)
        chat_done = self.chat.shutdown(timeout=4000)
        if ext_done and chat_done:
            return

        # A worker is stuck in uninterruptible native work (yt-dlp/ffmpeg,
        # or the model load). Without this the QThreadPool thread would keep the
        # interpreter — and the process — alive after the window closes. Kill the
        # whole process tree so nothing lingers.
        _force_kill_process_tree()


def _run_selftest() -> int:
    """Headless sanity check used to validate a packaged build.

    Verifies that bundled resources and runtime dependencies resolve in the
    (possibly frozen) environment: the GUI, ffmpeg binary, and YouTube runtime.
    Returns a process exit code; writes a result line to a
    file when SELFTEST_OUT is set (windowed builds have no stdout).
    """
    import os
    problems = []

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

    try:
        import yt_dlp_ejs
        from importlib.resources import files
        from .core import _youtube_runtime_opts
        scripts = files(yt_dlp_ejs) / "yt" / "solver"
        if not (scripts / "core.min.js").is_file():
            raise RuntimeError("YouTube EJS solver data missing")
        node = _youtube_runtime_opts().get("js_runtimes", {}).get("node", {}).get("path")
        if not node:
            raise RuntimeError("Node.js 22+ runtime missing")
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        version = subprocess.check_output([node, "--version"], text=True, timeout=10, **flags).strip()
        if int(version.lstrip("v").split(".")[0]) < 22:
            raise RuntimeError(f"Node.js too old: {version}")
    except Exception as e:
        problems.append(f"YouTube JS setup failed: {e}")

    # Built-in CPU model engine is optional (bundled only when llama-cpp-python
    # is installed at build time). Report whether it loaded so a release that's
    # meant to ship the model can confirm the compiled libs resolved — but don't
    # fail the test, since model-less builds are valid too.
    try:
        import llama_cpp  # noqa: F401
        llama_status = f"llama_cpp {getattr(llama_cpp, '__version__', '?')}"
        backend = Path(llama_cpp.__file__).parent / "lib" / "ggml-vulkan.dll"
        llama_status += f" | Vulkan backend {'bundled' if backend.is_file() else 'absent'}"
        if "--require-gpu-backend" in sys.argv and not backend.is_file():
            problems.append("Vulkan backend missing: build the GPU engine before packaging")
    except Exception as e:
        llama_status = f"llama_cpp absent ({e.__class__.__name__})"
        if "--require-gpu-backend" in sys.argv:
            problems.append(llama_status)

    # STT fallback engine is likewise optional. Report presence/absence for
    # build verification, but never fail the selftest over it.
    try:
        import pywhispercpp  # noqa: F401
        # The package doesn't export __version__; ask pip metadata instead.
        from importlib.metadata import version as _pkg_version
        try:
            _pw_ver = _pkg_version("pywhispercpp")
        except Exception:
            _pw_ver = "?"
        stt_status = f"pywhispercpp {_pw_ver}"
    except Exception as e:
        stt_status = f"pywhispercpp absent ({e.__class__.__name__})"

    # Instantiate the GUI offscreen to confirm widgets/resources load.
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv)
        from .theme import apply_theme
        apply_theme(app)
        win = MainWindow()
        out_dir = Path(win.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".selftest-write"
        probe.write_text("ok", encoding="utf-8")
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        win.show()
        app.processEvents()
        win.close()
    except Exception as e:
        problems.append(f"GUI/output setup failed: {e}")

    result = "SELFTEST OK" if not problems else "SELFTEST FAIL: " + "; ".join(problems)
    result += f" | {llama_status} | {stt_status}"
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
    from .theme import apply_theme
    apply_theme(app)
    win = MainWindow()
    win.show()
    for _ in range(5):
        app.processEvents()
    pix = win.grab()
    ok = pix.save(path)
    print(f"screenshot {'saved' if ok else 'FAILED'}: {path}")
    win.close()
    return 0 if ok else 1


def main():
    if "--gpu-selftest" in sys.argv:
        from .gpu_selftest import run
        sys.exit(run())
    if "--selftest" in sys.argv:
        sys.exit(_run_selftest())
    if "--screenshot" in sys.argv:
        i = sys.argv.index("--screenshot")
        out = sys.argv[i + 1] if i + 1 < len(sys.argv) else "screenshot.png"
        sys.exit(_run_screenshot(out))
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube 자막 추출기")
    from .theme import apply_theme
    apply_theme(app)

    from yt_extractor.splash import make_splash
    splash = make_splash()
    splash.show()
    app.processEvents()

    win = MainWindow()
    win.show()
    app.processEvents()

    # Always open on primary monitor (frameGeometry is valid after show)
    primary = app.primaryScreen().availableGeometry()
    fg = win.frameGeometry()
    win.move(
        primary.x() + (primary.width() - fg.width()) // 2,
        primary.y() + (primary.height() - fg.height()) // 2,
    )

    QTimer.singleShot(3000, lambda: splash.finish(win))

    from yt_extractor.updater import UpdateChecker
    _checker = UpdateChecker()
    _checker.update_available.connect(
        lambda v: QMessageBox.information(
            win, "업데이트 사용 가능",
            f"새 버전 {v} 이(가) 출시됐습니다.\n"
            "https://mazeline.tech/ 에서 다운로드하세요.",
        )
    )
    _checker.start()

    # Eagerly load the built-in model now so it's ready (and stays resident)
    # for the whole session. Done only on the real run — not in --selftest /
    # --screenshot — so those never trigger a multi-GB download/load.
    win.chat.preload_model()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
