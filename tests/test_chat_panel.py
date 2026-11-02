"""GUI regressions without model/network access."""
import os
from pathlib import Path
from unittest.mock import Mock
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from yt_extractor import app as ui

@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def panel(qapp):
    mw = Mock()
    mw.translate_cb.isChecked.return_value = True
    panel = ui.ChatPanel(mw)
    panel.start_chat_worker = Mock()
    yield panel
    for tab in (panel.summary_tab, panel.translate_tab):
        tab._on_cancelled()
    panel.deleteLater()
    qapp.processEvents()

def finish(tab, answer):
    tab._streaming_assistant = answer
    tab._on_done()

def test_large_download_progress_queued(panel, qapp):
    signals = ui.BuiltinSignals()
    signals.progress.connect(panel._on_builtin_progress, Qt.QueuedConnection)
    signals.progress.emit(2_650_000_000, 5_300_000_000)
    qapp.processEvents()
    assert panel.dl_bar.maximum() == 100
    assert panel.dl_bar.value() == 50

def test_followup_and_cancel_preserve_translation(panel, tmp_path):
    source = tmp_path / 'A.md'
    output = source.with_suffix('.ko.md')
    panel.load_transcript('A', '## Transcript\nOriginal', translate=True, path=str(source))
    finish(panel.translate_tab, 'Complete translation')
    panel.translate_tab._send('What does this mean?')
    finish(panel.translate_tab, 'Explanation')
    assert output.read_text(encoding='utf-8') == 'Complete translation'
    panel.translate_tab._send('Translate', translate_full=True)
    panel.translate_tab._streaming_assistant = 'Partial translation'
    panel.translate_tab._on_cancelled()
    assert output.read_text(encoding='utf-8') == 'Complete translation'
    assert panel.translate_tab._worker is None
    assert panel.translate_tab.send_btn.isEnabled()

def test_context_change_guard_and_history_reset(panel, tmp_path, monkeypatch):
    monkeypatch.setattr(ui.QMessageBox, 'information', Mock())
    a, b = tmp_path / 'A.md', tmp_path / 'B.md'
    panel.load_transcript('A', 'A text', translate=True, path=str(a))
    panel.load_transcript('B', 'B text', summarize=True, path=str(b))
    assert panel._attached_path == str(a)
    finish(panel.translate_tab, 'A translation')
    assert a.with_suffix('.ko.md').read_text(encoding='utf-8') == 'A translation'
    assert not b.with_suffix('.ko.md').exists()
    panel.load_transcript('B', 'B text', summarize=True, path=str(b))
    assert panel.translate_tab._history == []
    assert all('A translation' not in m['content'] for m in panel.summary_tab._worker.messages)

def test_translation_path_snapshot(panel, tmp_path):
    source = tmp_path / 'A.md'
    panel.load_transcript('A', 'A text', translate=True, path=str(source))
    panel._attached_path = str(tmp_path / 'B.md')
    finish(panel.translate_tab, 'A translation')
    assert source.with_suffix('.ko.md').exists()
    assert not (tmp_path / 'B.ko.md').exists()

def test_translation_chunks_and_summary(panel):
    panel._attached = [('A', '## Transcript\n' + 'sentence\n' * 1000)]
    panel.translate_tab._send('Translate', translate_full=True)
    assert len(panel.translate_tab._worker.chunks) > 1
    assert '## Transcript' not in panel.translate_tab._worker.chunks[0]
    panel.summary_tab._send('Summarize')
    assert panel.summary_tab._worker.chunks is None
    assert panel.summary_tab._worker.messages

@pytest.mark.parametrize('cancel_before_run', [True, False])
def test_worker_cancellation_terminal_signal(monkeypatch, cancel_before_run):
    monkeypatch.setattr(ui, 'is_model_present', lambda: True)
    monkeypatch.setattr(ui, 'is_loaded', lambda: True)
    worker = ui.BuiltinWorker(chunks=['one', 'two'])
    monkeypatch.setattr(ui, 'stream_local_chat', lambda *a, **k: worker.cancel())
    done, cancelled = Mock(), Mock()
    worker.signals.done.connect(done)
    worker.signals.cancelled.connect(cancelled)
    if cancel_before_run:
        worker.cancel()
    worker.run()
    cancelled.assert_called_once()
    done.assert_not_called()

def test_job_identity_after_deletion(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(ui.MainWindow, '_probe_stt_async', lambda self: None)
    monkeypatch.setattr(ui, '_default_output_dir', lambda: str(tmp_path))
    win = ui.MainWindow()
    try:
        win._add_job_row('A'); win._add_job_row('B')
        a, b = win._job_id(0), win._job_id(1)
        path_a, path_b = tmp_path / 'A.md', tmp_path / 'B.md'
        path_a.write_text('A'); path_b.write_text('B')
        win._total = 2; win._running = True
        win._on_worker_status(a, ui.ST_DONE)
        win._on_worker_finished(a, {'files': [('MD', str(path_a))]})
        win.on_clear_done()
        assert win._job_id(0) == b
        win._on_worker_detail(b, 'B progress')
        assert win.table.item(0, ui.COL_DETAIL).text() == 'B progress'
        win._on_worker_status(b, ui.ST_DONE)
        win._on_worker_finished(b, {'files': [('MD', str(path_b))]})
        assert win._selected_transcript_path() == str(path_b)
        assert a not in win._paths_by_job
        opened = Mock()
        monkeypatch.setattr(ui.QDesktopServices, 'openUrl', opened)
        win.on_row_double_clicked(0, 0)
        assert Path(opened.call_args.args[0].toLocalFile()) == path_b
        win.on_clear_all()
        assert win._paths_by_job == {}
        win._add_job_row('C')
        assert win._job_id(0) > b
        assert win._selected_transcript_path() is None
    finally:
        win._running = False
        win.close()
