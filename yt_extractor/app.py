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
    Qt, QObject, QRunnable, QThreadPool, Signal, Slot, QUrl, QTimer,
)
from PySide6.QtGui import QDesktopServices, QBrush, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPlainTextEdit, QPushButton, QLabel, QLineEdit, QCheckBox, QSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog, QProgressBar,
    QGroupBox, QAbstractItemView, QMessageBox, QComboBox, QFrame, QSplitter,
    QTextBrowser, QTabWidget,
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
from .llm import build_messages, stream_chat, LLMError, MAX_CONTEXT_CHARS
from .local_llm import (
    is_model_present, download_model, stream_local_chat, ensure_loaded,
    is_loaded,
)
from .chat_render import render_conversation


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
    ST_PENDING: "#8a836f",
    ST_RUNNING: "#b3cf4e",
    ST_DONE: "#7f8a3f",
    ST_PARTIAL: "#b07000",
    ST_ERROR: "#e85d3e",
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
                 translate_to: str | None = None, should_cancel=None):
        super().__init__()
        self.row = row
        self.url = url
        self.out_dir = out_dir
        self.outputs = outputs
        self.langs = langs
        self.prefer_manual = prefer_manual
        self.transcript_format = transcript_format
        self.bitrate = bitrate
        self.translate_to = translate_to
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
                paths = extract_to_markdown(
                    self.url, self.out_dir,
                    preferred_langs=self.langs,
                    prefer_manual=self.prefer_manual,
                    transcript_format=self.transcript_format,
                    translate_to=self.translate_to,
                    progress=lambda m: self.signals.detail.emit(row, f"[자막] {m}"),
                )
                for i, p in enumerate(paths):
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


class ChatSignals(QObject):
    """Signals emitted by a ChatWorker, marshalled to the GUI thread."""

    token = Signal(str)      # one streamed text delta
    done = Signal()          # stream completed normally
    error = Signal(str)      # user-facing failure message


class ChatWorker(QRunnable):
    """Stream one LLM chat completion off the GUI thread.

    Mirrors ExtractWorker: native/blocking work runs here and reaches the UI
    only through signals. Cancellation is cooperative via an Event.
    """

    def __init__(self, messages, base_url: str, model: str, api_key: str):
        super().__init__()
        self.messages = messages
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.signals = ChatSignals()
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    @Slot()
    def run(self):
        try:
            stream_chat(
                self.messages, self.base_url, self.model, api_key=self.api_key,
                on_token=lambda t: self.signals.token.emit(t),
                should_cancel=self._cancel.is_set,
            )
        except LLMError as e:
            self.signals.error.emit(str(e))
            return
        except Exception as e:  # pragma: no cover - defensive
            self.signals.error.emit(f"예기치 못한 오류: {e}")
            return
        self.signals.done.emit()


class BuiltinSignals(QObject):
    """Signals for the bundled CPU model worker (download + inference)."""

    status = Signal(str)          # high-level phase, e.g. "모델 로딩 중…"
    progress = Signal(int, int)   # downloaded, total bytes (first-run download)
    token = Signal(str)           # streamed text delta
    done = Signal()
    error = Signal(str)


class BuiltinWorker(QRunnable):
    """Run the built-in model off the GUI thread.

    On first use the model isn't on disk, so this downloads it (reporting
    progress) before loading and streaming. Cancellation aborts the download or
    the generation, whichever is in flight.
    """

    def __init__(self, messages):
        super().__init__()
        self.messages = messages
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
                return
            # The model is normally preloaded at startup, so this is just
            # inference. Only say "로딩 중" if it actually isn't resident yet
            # (e.g. preload hasn't finished) — otherwise it looks like a reload.
            if is_loaded():
                self.signals.status.emit("AI가 처리 중…")
            else:
                self.signals.status.emit("모델 로딩 중… (최초 1회는 시간이 걸립니다)")
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
        self.signals.done.emit()


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
    """Clickable MazeLine ad banner — Lime Forward B theme, pinned at the bottom."""

    def __init__(self, url: str = BANNER_URL, image_path=None,
                 max_height: int = 110):
        super().__init__()
        self.url = url
        self._pix = None  # kept for selftest compatibility
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"{url} 바로가기")
        self.setFixedHeight(64)
        self.setStyleSheet(
            "BannerWidget { background-color: #cde86a; border-radius: 0px; }"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 0, 28, 0)
        lay.setSpacing(0)

        brand = QLabel("MazeLine")
        brand.setStyleSheet(
            "font-size: 15px; font-weight: 800; color: #2a2622; background: transparent;"
        )
        lay.addWidget(brand)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(18)
        sep.setStyleSheet(
            "QFrame { color: rgba(42,38,34,80); background: transparent; }"
        )
        lay.addSpacing(14)
        lay.addWidget(sep)
        lay.addSpacing(14)

        tagline = QLabel("게임 개발의 새로운 기준")
        tagline.setStyleSheet(
            "font-size: 13px; color: #575451; background: transparent;"
        )
        lay.addWidget(tagline)

        lay.addStretch(1)

        link = QLabel("mazeline.tech ↗")
        link.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #f47458;"
            "font-family: 'JetBrains Mono', 'Consolas', monospace;"
            "background: transparent;"
        )
        lay.addWidget(link)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(self.url))
        super().mousePressEvent(event)


SUMMARY_PROMPT = "첨부된 자막을 핵심 위주로 한국어로 요약해 주세요."
TRANSLATE_PROMPT = "첨부된 자막을 자연스러운 한국어로 번역해 주세요."


class ChatTab(QWidget):
    """One conversation strand (요약 or 번역) inside a parent ChatPanel.

    Each tab keeps its own history, view, input, and worker so summarizing on
    one tab and translating on the other don't interleave in a single chat
    log. The shared provider config and attached transcript live on the parent
    panel and are pulled in here on send.
    """

    def __init__(self, panel: "ChatPanel", quick_label: str, quick_prompt: str,
                 placeholder: str = "", manual_instruction: str | None = None):
        super().__init__()
        self.panel = panel
        self.quick_prompt = quick_prompt
        self.manual_instruction = manual_instruction
        self._history: list[dict] = []
        self._worker: ChatWorker | BuiltinWorker | None = None
        self._streaming_assistant: str | None = None
        self._last_error: str | None = None
        # Coalesce stream-driven re-renders (~80 ms) so a fast model doesn't
        # trigger one setHtml() per token.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._rerender)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        # Force a light canvas regardless of system palette — chat bubbles are
        # tuned for a light page.
        self.view.setStyleSheet(
            "QTextBrowser { background-color: #f6f6f6; color: #222222; }"
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
        self.input.setPlaceholderText("메시지 입력 (Ctrl+Enter 전송)")
        self.input.setMaximumHeight(80)
        self.input.installEventFilter(self)
        lay.addWidget(self.input)

        send_row = QHBoxLayout()
        self.send_btn = QPushButton("전송")
        self.send_btn.setStyleSheet("font-weight: bold;")
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
                "먼저 왼쪽 목록에서 자막 항목을 선택하고 'AI로 요약' 또는 "
                "'AI로 번역'을 눌러 불러오세요.",
            )
            return
        self._send(self.quick_prompt)

    def on_send_clicked(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        if self.manual_instruction:
            text = f"{self.manual_instruction}\n\n{text}"
        self._send(text)

    def _send(self, text: str):
        if self._worker is not None:
            QMessageBox.information(self, "응답 대기 중",
                                    "현재 답변이 끝난 뒤 보내주세요.")
            return
        panel = self.panel
        builtin = (panel.provider.currentText() == panel.BUILTIN_PROVIDER)
        if not builtin:
            base_url = panel.base_url.text().strip()
            model = panel.model.text().strip()
            if not base_url or not model:
                QMessageBox.information(
                    self, "설정 필요", "AI 서비스 주소와 모델 이름을 입력하세요."
                )
                return
            if not panel.api_key.text().strip() and not panel._is_local(base_url):
                QMessageBox.information(
                    self, "API 키 필요",
                    "이 서비스는 API 키가 필요합니다. 위 'AI 서비스' 설정에서 "
                    "키를 입력하세요.",
                )
                return

        messages = build_messages(text, history=self._history,
                                  transcripts=panel._attached,
                                  max_chars=None if not builtin else MAX_CONTEXT_CHARS)
        self._history.append({"role": "user", "content": text})
        # Empty in-flight assistant turn — _rerender() shows it as a bubble
        # with a streaming cursor until tokens fill it in.
        self._streaming_assistant = ""
        self._last_error = None
        self._set_busy(True)
        self._rerender()

        if builtin:
            worker = BuiltinWorker(messages)
            worker.signals.status.connect(panel._on_builtin_status)
            worker.signals.progress.connect(panel._on_builtin_progress)
        else:
            worker = ChatWorker(messages, panel.base_url.text().strip(),
                                panel.model.text().strip(),
                                panel.api_key.text())
        worker.signals.token.connect(self._on_token)
        worker.signals.done.connect(self._on_done)
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
        if self is self.panel.translate_tab:
            self.panel._maybe_auto_save_translation()

    @Slot(str)
    def _on_error(self, msg: str):
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

    def _set_busy(self, busy: bool):
        self.send_btn.setEnabled(not busy)
        self.quick_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)

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
        stick = sb.value() >= sb.maximum() - 8
        self.view.setHtml(html_doc)
        if stick:
            sb.setValue(sb.maximum())


class ChatPanel(QWidget):
    """Right-side AI chat: summarize / translate / Q&A over transcripts.

    Hosts two independent conversation tabs (요약 / 번역) that share the same
    provider config and attached transcript. Each tab keeps its own history so
    the two workflows don't pollute each other.
    """

    # Provider presets. label -> (base_url, default model, needs_key, key_url).
    # The built-in CPU model is the default so the app works with zero setup;
    # users who have their own AI account can switch to a cloud/local provider.
    BUILTIN_PROVIDER = "내장 모델 (GPU/CPU)"
    PROVIDERS = {
        BUILTIN_PROVIDER: ("", "", False, ""),
        "OpenAI": (
            "https://api.openai.com/v1", "gpt-4o-mini",
            True, "https://platform.openai.com/api-keys",
        ),
        "로컬 Ollama": (
            "http://localhost:11434/v1", "qwen2.5:1.5b", False, "",
        ),
        "로컬 SGLang · vLLM": (
            "http://localhost:8000/v1", "Qwen/Qwen2.5-1.5B-Instruct", False, "",
        ),
        "직접 입력": ("", "", False, ""),
    }
    DEFAULT_PROVIDER = BUILTIN_PROVIDER

    def __init__(self, main_window: "MainWindow"):
        super().__init__()
        self.mw = main_window
        # Dedicated pools keep AI work off the extraction pool. Remote chat must
        # not queue behind the multi-GB built-in model preload, while built-in
        # preload/inference stay serialized to avoid duplicate model loads.
        self.chat_pool = QThreadPool()
        self.chat_pool.setMaxThreadCount(1)
        self.model_pool = QThreadPool()
        self.model_pool.setMaxThreadCount(1)
        self._attached: list[tuple] = []       # (name, text) — shared by tabs
        self._attached_path: str | None = None  # full path of the loaded .md
        self._preload_worker: PreloadWorker | None = None
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 0, 0, 0)

        title = QLabel("🤖 AI 채팅 — 자막 요약 · 번역 · Q&A")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        lay.addWidget(title)

        # --- Endpoint config ---
        cfg = QGroupBox("AI 서비스 (OpenAI 호환)")
        cfg_grid = QGridLayout(cfg)
        cfg_grid.setContentsMargins(8, 6, 8, 6)

        cfg_grid.addWidget(QLabel("제공자:"), 0, 0)
        self.provider = QComboBox()
        self.provider.addItems(self.PROVIDERS.keys())
        self.provider.currentTextChanged.connect(self._on_provider_changed)
        cfg_grid.addWidget(self.provider, 0, 1, 1, 3)

        cfg_grid.addWidget(QLabel("주소:"), 1, 0)
        self.base_url = QLineEdit()
        self.base_url.setToolTip("OpenAI 호환 엔드포인트의 베이스 URL")
        cfg_grid.addWidget(self.base_url, 1, 1, 1, 3)

        cfg_grid.addWidget(QLabel("모델:"), 2, 0)
        self.model = QLineEdit()
        self.model.setToolTip("서비스/서버에서 사용할 모델 이름")
        cfg_grid.addWidget(self.model, 2, 1)
        cfg_grid.addWidget(QLabel("API 키:"), 2, 2)
        self.api_key = QLineEdit()
        self.api_key.setPlaceholderText("로컬은 비워둠")
        self.api_key.setEchoMode(QLineEdit.Password)
        cfg_grid.addWidget(self.api_key, 2, 3)

        # Hint with a clickable link to get a (free) key for the chosen service.
        self.key_hint = QLabel()
        self.key_hint.setOpenExternalLinks(True)
        self.key_hint.setWordWrap(True)
        self.key_hint.setStyleSheet("color: #7a7a7a; font-size: 11px;")
        cfg_grid.addWidget(self.key_hint, 3, 0, 1, 4)
        lay.addWidget(cfg)

        # Apply the default provider (built-in CPU model) to set field state.
        self.provider.setCurrentText(self.DEFAULT_PROVIDER)
        self._on_provider_changed(self.DEFAULT_PROVIDER)

        # --- Context indicator ---
        # Transcripts enter the conversation via the left pane's "AI로 요약" /
        # "AI로 번역" button, which reads the .md the extractor just produced
        # — so there's no manual file-picking here. This label just shows what's
        # loaded; it's shared between tabs since both operate on the same .md.
        self.attach_label = QLabel(
            "컨텍스트 없음 — 왼쪽에서 자막을 추출한 뒤 'AI로 요약' 또는 "
            "'AI로 번역'을 누르세요"
        )
        self.attach_label.setStyleSheet("color: #7a7a7a;")
        self.attach_label.setWordWrap(True)
        lay.addWidget(self.attach_label)

        # First-run model download / load indicator (hidden until needed) —
        # shared because either tab's send can be the trigger.
        self.builtin_status = QLabel("")
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
            self, "📝 자막 요약 시작", SUMMARY_PROMPT,
            placeholder=(
                "왼쪽에서 자막을 추출해 'AI로 요약'을 누르거나, "
                "여기에 질문을 입력하세요."
            ),
        )
        self.translate_tab = ChatTab(
            self, "🌐 자막 번역 시작", TRANSLATE_PROMPT,
            placeholder=(
                "왼쪽에서 자막을 추출해 'AI로 번역'을 누르거나, "
                "여기에 번역할 부분을 입력하세요."
            ),
            manual_instruction="다음 내용을 자연스러운 한국어로 번역해 주세요:",
        )
        self.tabs.addTab(self.summary_tab, "📝 요약")
        self.tabs.addTab(self.translate_tab, "🌐 번역")
        lay.addWidget(self.tabs, stretch=1)

    # --------------------------------------------------------- provider ---
    def _on_provider_changed(self, name: str):
        cfg = self.PROVIDERS.get(name)
        if not cfg:
            return
        base_url, model, needs_key, key_url = cfg
        builtin = (name == self.BUILTIN_PROVIDER)
        # The built-in model needs no endpoint config; grey those controls out.
        for w in (self.base_url, self.model, self.api_key):
            w.setEnabled(not builtin)
        if builtin:
            self.key_hint.setText(
                "앱 내장 모델. GPU(NVIDIA·AMD·Intel)가 있으면 자동 가속, "
                "없으면 CPU로 동작합니다. 설정이 필요 없습니다."
            )
            return
        if name == "직접 입력":
            self.key_hint.setText("주소·모델·키를 직접 입력하세요.")
            return
        self.base_url.setText(base_url)
        self.model.setText(model)
        if not needs_key:
            self.key_hint.setText(
                "로컬 서버이므로 키가 필요 없습니다. 서버를 먼저 실행하세요."
            )
        else:
            self.key_hint.setText(f'키 발급: <a href="{key_url}">{key_url}</a>')

    @staticmethod
    def _is_local(base_url: str) -> bool:
        return ("localhost" in base_url) or ("127.0.0.1" in base_url)

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
        # Don't swap context out from under that tab's in-flight answer.
        if target is not None and target._worker is not None:
            QMessageBox.information(
                self, "응답 대기 중", "현재 답변이 끝난 뒤 다시 시도하세요."
            )
            return
        self._attached = [(name, text)]
        self._attached_path = path
        self._refresh_context_label()
        if summarize:
            self.tabs.setCurrentWidget(self.summary_tab)
            self.summary_tab._send(SUMMARY_PROMPT)
        elif translate:
            self.tabs.setCurrentWidget(self.translate_tab)
            self.translate_tab._send(TRANSLATE_PROMPT)

    def _refresh_context_label(self):
        if not self._attached:
            self.attach_label.setText(
                "컨텍스트 없음 — 왼쪽에서 자막을 추출한 뒤 'AI로 요약' 또는 "
                "'AI로 번역'을 누르세요"
            )
            self.attach_label.setStyleSheet("color: #7a7a7a;")
            return
        names = ", ".join(n for n, _ in self._attached)
        self.attach_label.setText(f"📎 컨텍스트: {names}")
        self.attach_label.setStyleSheet("color: #1b7f3b;")

    def _maybe_auto_save_translation(self):
        """Save the translate tab's last AI response as a .ko.md file if the checkbox is on."""
        if not self.mw.translate_cb.isChecked():
            return
        if not self._attached_path:
            return
        history = self.translate_tab._history
        assistant_turns = [t["content"] for t in history if t.get("role") == "assistant"]
        if not assistant_turns:
            return
        text = assistant_turns[-1].strip()
        if not text:
            return
        try:
            src = Path(self._attached_path)
            out_path = src.with_suffix(".ko.md")
            out_path.write_text(text, encoding="utf-8")
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

    @Slot(int, int)
    def _on_builtin_progress(self, done: int, total: int):
        self.dl_bar.setVisible(True)
        if total > 0:
            self.dl_bar.setRange(0, total)
            self.dl_bar.setValue(done)
            self.dl_bar.setFormat(f"{done / 1e6:.0f} / {total / 1e6:.0f} MB  (%p%)")
        else:
            self.dl_bar.setRange(0, 0)  # indeterminate (unknown size)

    def _hide_builtin_ui(self):
        self.builtin_status.setVisible(False)
        self.dl_bar.setVisible(False)

    def start_chat_worker(self, worker: ChatWorker | BuiltinWorker):
        if isinstance(worker, BuiltinWorker):
            self.model_pool.start(worker)
        else:
            self.chat_pool.start(worker)

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
        self.builtin_status.setText("✅ 내장 모델 준비 완료")

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
        self.chat_pool.clear()
        self.model_pool.clear()
        chat_done = self.chat_pool.waitForDone(timeout)
        model_done = self.model_pool.waitForDone(timeout)
        return chat_done and model_done


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube 자막 추출기")
        self.resize(1200, 980)

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
        outer = QVBoxLayout(central)

        # Left pane holds the entire existing extraction UI; the chat panel
        # sits to its right in a splitter, and the banner spans the bottom.
        left = QWidget()
        root = QVBoxLayout(left)

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

        from PySide6.QtWidgets import QButtonGroup
        fmt_row = QHBoxLayout()
        fmt_row.setSpacing(10)
        self._fmt_sentence = QPushButton("Sentence\n문장 단위")
        self._fmt_sentence.setCheckable(True)
        self._fmt_sentence.setChecked(True)
        self._fmt_sentence.setObjectName("fmtCard")
        self._fmt_paragraph = QPushButton("Paragraph\n단락 단위")
        self._fmt_paragraph.setCheckable(True)
        self._fmt_paragraph.setObjectName("fmtCard")
        self._fmt_timestamp = QPushButton("Timestamp\n타임스탬프")
        self._fmt_timestamp.setCheckable(True)
        self._fmt_timestamp.setObjectName("fmtCard")
        self._fmt_group = QButtonGroup(self)
        self._fmt_group.setExclusive(True)
        self._fmt_group.addButton(self._fmt_sentence)
        self._fmt_group.addButton(self._fmt_paragraph)
        self._fmt_group.addButton(self._fmt_timestamp)
        for b in (self._fmt_sentence, self._fmt_paragraph, self._fmt_timestamp):
            fmt_row.addWidget(b)
        fmt_widget = QWidget()
        fmt_widget.setLayout(fmt_row)
        opt.addWidget(fmt_widget, 2, 0, 1, 4)

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

        self.translate_cb = QCheckBox("🌐 한국어 번역도 함께 저장 (.ko.md)")
        self.translate_cb.setToolTip(
            "원문이 한국어가 아닐 때, YouTube의 번역 자막을 별도 '.ko.md' "
            "파일로 함께 저장합니다. 번역 자막이 없는 영상은 원문만 저장합니다."
        )
        opt.addWidget(self.translate_cb, 4, 0, 1, 4)

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
        self.start_btn.setObjectName("start_btn")
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

        self.summarize_btn = QPushButton("🤖 AI로 요약")
        self.summarize_btn.setToolTip(
            "선택한(또는 가장 최근) 자막을 오른쪽 AI 채팅 '요약' 탭으로 "
            "불러와 한국어로 요약합니다."
        )
        self.summarize_btn.clicked.connect(self.on_ai_summarize)
        action.addWidget(self.summarize_btn)

        self.translate_ai_btn = QPushButton("🌐 AI로 번역")
        self.translate_ai_btn.setToolTip(
            "선택한(또는 가장 최근) 자막을 오른쪽 AI 채팅 '번역' 탭으로 "
            "불러와 자연스러운 한국어로 번역합니다."
        )
        self.translate_ai_btn.clicked.connect(self.on_ai_translate)
        action.addWidget(self.translate_ai_btn)

        action.addStretch(1)
        root.addLayout(action)

        # --- Progress + status bar ---
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        root.addWidget(self.progress)

        # --- Right pane: AI chat ---
        self.chat = ChatPanel(self)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self.chat)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 500])
        outer.addWidget(splitter, stretch=1)

        # --- Company ad banner (clickable, spans full width at the bottom) ---
        self.banner = BannerWidget(image_path=_banner_image_path())
        outer.addWidget(self.banner)

        self.statusBar().showMessage("준비됨")

        self._paths_by_row: dict[int, str] = {}

    # ------------------------------------------------------------- helpers --
    def _sync_output_controls(self):
        want_t = self.transcript_cb.isChecked()
        want_a = self.audio_cb.isChecked()
        self.lang_input.setEnabled(want_t)
        self.manual_cb.setEnabled(want_t)
        for b in (self._fmt_sentence, self._fmt_paragraph, self._fmt_timestamp):
            b.setEnabled(want_t)
        self.translate_cb.setEnabled(want_t)
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

    def _selected_transcript_path(self):
        """The .md transcript to summarize: the selected row's, else newest."""
        selected = [i.row() for i in self.table.selectionModel().selectedRows()]
        order = selected + sorted(self._paths_by_row, reverse=True)
        seen = set()
        for row in order:
            if row in seen:
                continue
            seen.add(row)
            for p in self._paths_by_row.get(row, []):
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
        self.chat.load_transcript(
            Path(path).name, text, summarize=summarize, translate=translate,
            path=path,
        )

    def _set_controls_enabled(self, enabled: bool):
        for w in (self.start_btn, self.add_btn, self.dir_btn,
                  self.concurrency, self.clear_all_btn,
                  self.transcript_cb, self.audio_cb):
            w.setEnabled(enabled)
        if enabled:
            self._sync_output_controls()  # re-apply per-output enable rules
        else:
            for w in (self.lang_input, self.manual_cb, self._fmt_sentence,
                      self._fmt_paragraph, self._fmt_timestamp,
                      self.translate_cb, self.bitrate_combo):
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
        if self._fmt_paragraph.isChecked():
            transcript_format = TRANSCRIPT_PARAGRAPHS
        elif self._fmt_timestamp.isChecked():
            transcript_format = TRANSCRIPT_TIMESTAMPED
        else:
            transcript_format = TRANSCRIPT_SENTENCES
        bitrate = self.bitrate_combo.currentText().split()[0]  # "192 kbps" -> "192"
        translate_to = "ko" if self.translate_cb.isChecked() else None
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
                translate_to=translate_to,
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
        # running ones a moment to unwind cooperatively. The chat panel has its
        # own pool (and its own download/inference worker), so drain both.
        self._shutdown.set()
        self.pool.clear()
        event.accept()
        ext_done = self.pool.waitForDone(4000)
        chat_done = self.chat.shutdown(timeout=4000)
        if ext_done and chat_done:
            return

        # A worker is stuck in uninterruptible native work (yt-dlp/ffmpeg/aria2c,
        # or the model load). Without this the QThreadPool thread would keep the
        # interpreter — and the process — alive after the window closes. Kill the
        # whole process tree so nothing lingers.
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

    # Built-in CPU model engine is optional (bundled only when llama-cpp-python
    # is installed at build time). Report whether it loaded so a release that's
    # meant to ship the model can confirm the compiled libs resolved — but don't
    # fail the test, since model-less builds are valid too.
    try:
        import llama_cpp  # noqa: F401
        llama_status = f"llama_cpp {getattr(llama_cpp, '__version__', '?')}"
    except Exception as e:
        llama_status = f"llama_cpp absent ({e.__class__.__name__})"

    # Instantiate the GUI offscreen to confirm widgets/resources load.
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication(sys.argv)
        win = MainWindow()
        win.show()
        app.processEvents()
    except Exception as e:
        problems.append(f"GUI instantiation failed: {e}")

    result = "SELFTEST OK" if not problems else "SELFTEST FAIL: " + "; ".join(problems)
    result += f" | {llama_status}"
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

    from yt_extractor.splash import make_splash
    splash = make_splash()
    splash.show()
    app.processEvents()

    app.setStyleSheet("""
        QWidget { background-color: #f6f2ea; color: #2a2622;
                  font-family: 'Gothic A1', 'Apple SD Gothic Neo', 'Malgun Gothic', system-ui, sans-serif; }
        QMainWindow { background-color: #efe8da; }
        QMainWindow::centralWidget { background-color: #f6f2ea; }

        QGroupBox { border: none; background: #ffffff; border-radius: 16px;
                    padding: 16px; padding-top: 24px; margin-top: 8px; }
        QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
                           left: 16px; top: 4px; color: #8a836f; font-size: 12px;
                           font-weight: 700; letter-spacing: 1px; }

        QPlainTextEdit { border: 1px solid #ece5d8; border-radius: 12px;
                         background: #ffffff; padding: 8px; }
        QLineEdit { border: 1px solid #ece5d8; border-radius: 12px;
                    background: #ffffff; padding: 6px 10px; }
        QLineEdit[readOnly="true"] { background: #efe8da; color: #575451; }

        QPushButton { background: #2a2622; color: #ffffff; border-radius: 12px;
                      padding: 8px 16px; font-weight: 700; border: none; }
        QPushButton:hover { background: #3d3733; }
        QPushButton:pressed { background: #1a1614; }
        QPushButton:disabled { background: #c9c2b2; color: #8a836f; }

        QPushButton#start_btn { background: #f47458; color: #ffffff;
                                font-size: 14px; padding: 12px 24px; }
        QPushButton#start_btn:hover { background: #e85d3e; }
        QPushButton#start_btn:disabled { background: #c9c2b2; color: #ffffff; }

        QPushButton#fmtCard { background: #ffffff; color: #2a2622;
                               border: 1px solid #ece5d8; border-radius: 12px;
                               padding: 12px 8px; font-weight: 400; }
        QPushButton#fmtCard:checked { background: #cde86a; border: 1px solid #b3cf4e;
                                       font-weight: 700; color: #2a2622; }
        QPushButton#fmtCard:hover { background: #f6f2ea; }
        QPushButton#fmtCard:checked:hover { background: #bdd860; }

        QComboBox { border: 1px solid #ece5d8; border-radius: 12px;
                    background: #ffffff; padding: 6px 10px; }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #ece5d8;
                                       selection-background-color: #cde86a;
                                       selection-color: #2a2622; }

        QSpinBox { border: 1px solid #ece5d8; border-radius: 12px;
                   background: #ffffff; padding: 6px 10px; }
        QSpinBox::up-button, QSpinBox::down-button { width: 18px; border: none; }

        QCheckBox { color: #2a2622; spacing: 6px; }
        QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #c9c2b2;
                               border-radius: 4px; background: #ffffff; }
        QCheckBox::indicator:checked { background: #f47458; border-color: #f47458; }

        QProgressBar { border: none; border-radius: 999px; background: #ece4d4;
                       min-height: 6px; max-height: 6px; }
        QProgressBar::chunk { background: #b3cf4e; border-radius: 999px; }

        QProgressBar#rowProgress { min-height: 20px; max-height: 20px; border-radius: 4px;
                                   border: 1px solid #ddd5c8; font-size: 11px; color: #2a2622; }
        QProgressBar#rowProgress::chunk { border-radius: 3px; }

        QTableWidget { border: none; background: #ffffff; gridline-color: #ece5d8;
                       border-radius: 12px; }
        QTableWidget::item { padding: 6px 8px; border: none; }
        QTableWidget::item:selected { background: #eef6cf; color: #2a2622; }
        QHeaderView::section { background: #f6f2ea; border: none; border-bottom: 1px solid #ece5d8;
                                padding: 6px 8px; font-weight: 700; color: #8a836f;
                                font-size: 12px; }

        QTabWidget::pane { border: none; background: transparent; }
        QTabBar::tab { background: transparent; color: #8a836f; padding: 6px 18px;
                       border-radius: 999px; font-weight: 600; margin: 2px; }
        QTabBar::tab:selected { background: #f47458; color: #ffffff; }
        QTabBar::tab:hover:!selected { background: #ece5d8; color: #2a2622; }

        QTextBrowser { border: none; background: #efe8da; border-radius: 12px; }

        QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
        QScrollBar::handle:vertical { background: rgba(42,38,34,0.18); border-radius: 999px;
                                       min-height: 24px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }
        QScrollBar::handle:horizontal { background: rgba(42,38,34,0.18); border-radius: 999px;
                                         min-width: 24px; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

        QLabel { background: transparent; }
        QSplitter::handle { background: #ece5d8; }
        QStatusBar { background: #f6f2ea; color: #8a836f; font-size: 12px; }
        QMessageBox { background: #f6f2ea; }
    """)
    win = MainWindow()
    win.show()
    app.processEvents()

    # Always open on primary monitor (frameGeometry is valid after show)
    primary = app.primaryScreen().geometry()
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
