"""Behavior regressions for the redesigned workspace (no network/model use)."""
import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from yt_extractor import app as ui
from yt_extractor.theme import apply_theme


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
    for tab in (win.chat.summary_tab, win.chat.translate_tab):
        tab._on_cancelled()
    win.close()
    app.processEvents()


def completed_job(win, path, status=ui.ST_DONE):
    win._add_job_row("https://youtu.be/H49TUZY1pL0")
    row = win.table.rowCount() - 1
    job = win._job_id(row)
    win._total += 1
    win._on_worker_status(job, status)
    win._on_worker_finished(job, {"files": [("file", str(path))]})
    return row


def test_add_shortcut_preserves_invalid_input_and_empty_state(window):
    window.url_input.setPlainText("https://youtu.be/H49TUZY1pL0\ninvalid link")
    window.url_input.setFocus()
    QTest.keyClick(window.url_input, Qt.Key_Return, Qt.ControlModifier)
    assert window.table.rowCount() == 1
    assert window.url_input.toPlainText() == "invalid link"
    assert window.queue_stack.currentWidget() is window.table
    assert window.start_btn.isEnabled()
    window.on_clear_all()
    assert window.queue_stack.currentIndex() == 0
    assert not window.start_btn.isEnabled()
    assert window.progress.maximum() == 1  # no idle busy animation


def test_settings_preserve_values_and_independent_polling(window):
    window.settings_dialog.show()
    window.lang_input.setText("ja, en")
    window.stt_poll_spin.setValue(7)
    window._fmt_paragraph.click()
    window.concurrency.setValue(2)
    window.settings_dialog.reject()
    window.settings_dialog.show()
    assert window.lang_input.text() == "ja, en"
    assert window.stt_poll_spin.value() == 7
    assert window.stt_poll_spin.isEnabled()
    assert not window.stt_cb.isChecked()
    assert "단락" in window.settings_summary.text()
    assert "동시 2개" in window.settings_summary.text()
    window.settings_dialog.reject()
    assert window.bitrate_combo.isHidden()
    window.audio_cb.click()
    assert not window.bitrate_combo.isHidden()


def test_late_stt_probe_does_not_unlock_running_options(window):
    window._running = True
    window._set_controls_enabled(False)
    window._on_stt_probe(True)
    assert not window.lang_input.isEnabled()
    assert not window.stt_cb.isEnabled()
    assert not window.stt_poll_spin.isEnabled()
    window._running = False
    window._set_controls_enabled(True)
    assert window.stt_cb.isEnabled()


def test_selected_job_never_uses_another_jobs_transcript(window, tmp_path):
    md = tmp_path / "first.md"
    md.write_text("First transcript")
    mp4 = tmp_path / "second.mp4"
    mp4.write_bytes(b"media")
    completed_job(window, md)
    row = completed_job(window, mp4)
    window.table.selectRow(row)
    assert window._selected_transcript_path() is None
    assert not window.summarize_btn.isEnabled()
    window.table.selectRow(0)
    assert window._selected_transcript_path() == str(md)
    assert window.summarize_btn.isEnabled()


def test_explicit_file_selection_survives_updates_and_opens_correct_file(window, tmp_path, monkeypatch):
    md, srt = tmp_path / "video.md", tmp_path / "video.srt"
    md.write_text("transcript")
    srt.write_text("subtitle")
    completed_job(window, md)
    job = window._job_id(0)
    window._paths_by_job[job].append(str(srt))
    window.table.selectRow(0)
    window.file_combo.setCurrentIndex(1)
    window._refresh_result()
    opened = Mock()
    monkeypatch.setattr(ui.QDesktopServices, "openUrl", opened)
    window.file_open_btn.click()
    assert Path(opened.call_args.args[0].toLocalFile()) == srt
    window.file_folder_btn.click()
    assert Path(opened.call_args.args[0].toLocalFile()) == tmp_path
    srt.unlink()
    alert = Mock()
    monkeypatch.setattr(ui.QMessageBox, "information", alert)
    opened.reset_mock()
    window.file_open_btn.click()
    alert.assert_called_once()
    opened.assert_not_called()


def test_hiding_ai_preserves_inference_context_and_history(window, tmp_path):
    path = tmp_path / "video.md"
    path.write_text("Original transcript")
    completed_job(window, path)
    window.table.selectRow(0)
    window.chat.start_chat_worker = Mock()
    window.summarize_btn.click()
    worker = window.chat.summary_tab._worker
    assert worker is not None
    assert window.ai_dock.isVisible()
    window.ai_dock.close()
    assert window.chat.summary_tab._worker is worker
    assert window.chat._attached_path == str(path)
    window._show_ai()
    assert window.chat.summary_tab._worker is worker
    assert window.chat.summary_tab.stop_btn.isEnabled()
    window.chat.summary_tab.stop_btn.click()
    assert worker._cancel.is_set()


def test_progress_has_no_invented_completion_during_conversion(window):
    window._add_job_row("https://youtu.be/H49TUZY1pL0")
    job = window._job_id(0)
    window._on_worker_status(job, ui.ST_RUNNING)
    bar = window.table.cellWidget(0, ui.COL_PROGRESS)
    assert bar.maximum() == 0
    window._on_worker_detail(job, "[영상] 45.7%")
    assert bar.maximum() == 100 and bar.value() == 45
    window._on_worker_detail(job, "[MP3] 변환 중")
    assert bar.maximum() == 0
    window._on_worker_status(job, ui.ST_ERROR)
    assert bar.maximum() == 100 and bar.value() == 0


def test_saved_ai_translation_appears_in_its_original_job(window, tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("A")
    b.write_text("B")
    completed_job(window, a)
    completed_job(window, b)
    window.table.selectRow(1)
    window.translate_cb.setChecked(True)
    window.chat._maybe_auto_save_translation(str(a), "A translation")
    translated = str(a.with_suffix(".ko.md"))
    assert translated in window._paths_by_job[window._job_id(0)]
    assert translated not in window._paths_by_job[window._job_id(1)]
    assert window.file_combo.count() == 1
    window.table.selectRow(0)
    assert window.file_combo.findData(translated) >= 0


def test_small_window_keeps_settings_and_results_accessible(window):
    window.resize(760, 480)
    window.audio_cb.setChecked(True)
    window.video_cb.setChecked(True)
    QApplication.processEvents()
    viewport = window.centralWidget()
    assert viewport.horizontalScrollBar().maximum() == 0
    viewport.ensureWidgetVisible(window.file_open_btn)
    assert viewport.verticalScrollBar().maximum() > 0
    window._show_ai()
    assert window.ai_dock.isFloating()
    assert window.width() == 760


def test_partial_completion_can_be_cleared_without_changing_other_job_identity(window, tmp_path):
    path = tmp_path / "partial.md"
    path.write_text("partial result")
    completed_job(window, path, ui.ST_PARTIAL)
    window._add_job_row("https://youtu.be/abcdefghijk")
    pending = window._job_id(1)
    window.on_clear_done()
    assert window.table.rowCount() == 1
    assert window._job_id(0) == pending
    assert window._paths_by_job == {}
