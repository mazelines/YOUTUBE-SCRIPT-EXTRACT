"""Lock down the '질문하기' (follow-up question) path on the AI chat panel.

Regression guard: on the translate tab, the follow-up question must go through
the normal chat-completion array (history + transcript context), NOT the chunked
whole-transcript translation. Before the fix, _send chunked on every translate-
tab send, so a follow-up question silently re-translated the whole transcript
instead of being answered.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import yt_extractor.app as app_module
from yt_extractor.app import ChatPanel


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _panel_with_transcript(qapp, body="## Transcript\nhello world this is a caption"):
    # ChatPanel stores the main window but only touches it on translation
    # auto-save, which never fires here (the worker is intercepted). A bare
    # stub is enough; give it the two attributes that path reads anyway.
    class _StatusBar:
        def showMessage(self, *a, **k):
            pass

    class _MW:
        translate_cb = type("CB", (), {"isChecked": lambda self: False})()
        statusBar = _StatusBar

    panel = ChatPanel(_MW())
    captured = {}
    # Intercept the worker so it is built but never run (no model load/inference).
    panel.start_chat_worker = lambda worker: captured.setdefault("worker", worker)
    panel._attached = [("video.md", body)]
    panel._attached_path = None
    return panel, captured


def test_initial_translation_uses_chunks(qapp):
    panel, captured = _panel_with_transcript(qapp)
    panel.translate_tab._send(app_module.TRANSLATE_PROMPT, translate_full=True)
    assert captured["worker"].chunks is not None
    assert captured["worker"].messages is None


def test_followup_on_translate_tab_does_not_chunk(qapp):
    panel, captured = _panel_with_transcript(qapp)
    tab = panel.translate_tab
    tab.input.setPlainText("이 부분 좀 더 자세히 설명해 줘")
    tab.on_send_clicked()

    worker = captured["worker"]
    assert worker.chunks is None            # not re-translating the transcript
    assert worker.messages is not None      # normal chat-completion array
    assert worker.messages[-1]["role"] == "user"
    assert worker.messages[-1]["content"] == "이 부분 좀 더 자세히 설명해 줘"


def test_followup_carries_history_and_transcript_context(qapp):
    panel, captured = _panel_with_transcript(qapp)
    tab = panel.translate_tab
    tab._history = [{"role": "assistant", "content": "(이전 번역 결과)"}]
    tab.input.setPlainText("여기서 핵심이 뭐야?")
    tab.on_send_clicked()

    msgs = captured["worker"].messages
    assert msgs[0]["role"] == "system"
    assert "자막" in msgs[0]["content"]          # transcript attached as context
    assert msgs[-2] == {"role": "assistant", "content": "(이전 번역 결과)"}
    assert msgs[-1]["content"] == "여기서 핵심이 뭐야?"


def test_send_button_is_renamed(qapp):
    panel, _ = _panel_with_transcript(qapp)
    for tab in (panel.summary_tab, panel.translate_tab):
        assert tab.send_btn.text() == "질문하기"
