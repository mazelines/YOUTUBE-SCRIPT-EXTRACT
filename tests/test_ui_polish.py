"""Stop/retry, queue hygiene and preference persistence (no network/model use)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from yt_extractor import app as ui
from yt_extractor.theme import apply_theme
from yt_extractor.ui_layout import TONE_ROLE


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    monkeypatch.setattr(ui.MainWindow, "_probe_stt_async", lambda self: None)
    monkeypatch.setattr(ui, "_default_output_dir", lambda: str(tmp_path))
    win = ui.MainWindow()
    win.show()
    app.processEvents()
    yield win
    win._running = False
    win.close()
    app.processEvents()


def test_url_hint_counts_links_and_gates_add_button(window):
    assert not window.add_btn.isEnabled()
    window.url_input.setPlainText("https://youtu.be/H49TUZY1pL0\nnope")
    assert window.add_btn.isEnabled()
    assert window.url_hint.property("tone") == "warning"
    assert "1개" in window.url_hint.text()


def test_duplicate_links_are_not_queued_twice(window):
    window.url_input.setPlainText("https://youtu.be/H49TUZY1pL0\n"
                                  "https://www.youtube.com/watch?v=H49TUZY1pL0")
    window.on_add_urls()
    window.url_input.setPlainText("https://youtu.be/H49TUZY1pL0")
    window.on_add_urls()
    assert window.table.rowCount() == 1
    assert window.url_input.toPlainText() == ""


def test_stopped_worker_reports_back_and_row_can_restart(window, monkeypatch):
    window._add_job_row("https://youtu.be/H49TUZY1pL0")
    started = []
    monkeypatch.setattr(window.pool, "start", lambda worker: started.append(worker))
    window.on_start()
    assert window.stop_btn.isVisible() and not window.start_btn.isVisible()
    window.on_stop()
    worker = started[0]
    worker.signals.status.connect(window._on_worker_status)
    worker.run()  # a queued job that sees the stop flag must still finish
    assert not window._running
    assert window._row_status(0) == ui.ST_CANCELLED
    assert window.table.item(0, ui.COL_STATUS).data(TONE_ROLE) == "neutral"
    assert window.start_btn.isEnabled() and not window.stop_btn.isVisible()
    window.on_start()
    assert window._row_status(0) == ui.ST_PENDING
    assert not window._cancel.is_set()


def test_retry_and_remove_respect_running_jobs(window):
    window._add_job_row("https://youtu.be/H49TUZY1pL0")
    window._add_job_row("https://youtu.be/abcdefghijk")
    failed = window._job_id(0)
    window._total = 1
    window._on_worker_status(failed, ui.ST_ERROR)
    window._on_worker_finished(failed, {"files": [], "errors": [("자막", "x")]})
    window.on_retry_row(0)
    assert window._row_status(0) == ui.ST_PENDING
    window._running = True
    window.table.selectRow(1)
    window.on_remove_selected()
    assert window.table.rowCount() == 2  # queued in the running batch
    window._running = False
    window.on_remove_selected()
    assert window.table.rowCount() == 1


def test_preferences_round_trip(window, tmp_path):
    settings = QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat)
    window.restore_settings(settings)
    window.audio_cb.setChecked(True)
    window.bitrate_combo.setCurrentIndex(2)
    window.lang_input.setText("ja")
    window._fmt_timestamp.click()
    window.concurrency.setValue(3)
    window.save_settings()
    fresh = ui.MainWindow()
    try:
        fresh.restore_settings(QSettings(str(tmp_path / "prefs.ini"), QSettings.IniFormat))
        assert fresh.audio_cb.isChecked()
        assert fresh.bitrate_combo.currentIndex() == 2
        assert fresh.lang_input.text() == "ja"
        assert fresh._fmt_timestamp.isChecked()
        assert fresh.concurrency.value() == 3
        assert fresh.out_dir == window.out_dir
    finally:
        fresh.close()
