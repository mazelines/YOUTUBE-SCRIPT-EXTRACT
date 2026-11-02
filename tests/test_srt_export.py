from pathlib import Path

import pytest

from yt_extractor import core


def source():
    return core.TranscriptResult(
        core.VideoMeta("H49TUZY1pL0", "Example"), "English", "en", False,
        [("hello\nworld", 59.9996, 1.0004), ("겹치는 자막", 60.5, 0.5)],
    )


def test_srt_preserves_cues_overlap_unicode_and_millisecond_carry():
    result = source()
    result.snippets += [(" ", 65, 1), ("hour", 3599.9996, 1.0004)]
    assert core.build_srt(result) == (
        "1\n00:01:00,000 --> 00:01:01,000\nhello\nworld\n\n"
        "2\n00:01:00,500 --> 00:01:01,000\n겹치는 자막\n\n"
        "3\n01:00:00,000 --> 01:00:01,000\nhour\n\n"
    )
    assert core.build_srt(core.TranscriptResult(result.meta, "en", "en", False)) == ""


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("style", core.TRANSCRIPT_FORMATS)
def test_original_srt_saved_independently_of_md_format(tmp_path, monkeypatch, fallback, style):
    result = source()
    monkeypatch.setattr(core, "fetch_video_meta", lambda *a: result.meta)
    monkeypatch.setattr(core, "fetch_transcript", lambda *a, **k: result)
    monkeypatch.setattr(core, "fetch_transcript_or_none", lambda *a, **k: result)
    translated = core.TranscriptResult(result.meta, "Korean", "ko", True, [("번역", 0, 1)])
    monkeypatch.setattr(core, "fetch_translated_transcript", lambda *a, **k: translated)
    extract = core.extract_to_markdown_with_fallback if fallback else core.extract_to_markdown
    paths = extract("H49TUZY1pL0", tmp_path, transcript_format=style, translate_to="ko")
    assert [p.name for p in paths] == [
        "Example [H49TUZY1pL0].md", "Example [H49TUZY1pL0].srt", "Example [H49TUZY1pL0].ko.md",
    ]
    assert paths[1].read_text(encoding="utf-8") == core.build_srt(result)
    assert "번역" not in paths[1].read_text(encoding="utf-8")


def test_stt_srt_is_distinguished_from_youtube(tmp_path, monkeypatch):
    result = source()
    result.origin = "stt"
    monkeypatch.setattr(core, "fetch_video_meta", lambda *a: result.meta)
    monkeypatch.setattr(core, "fetch_transcript_or_none", lambda *a, **k: result)
    paths = core.extract_to_markdown_with_fallback("H49TUZY1pL0", tmp_path)
    assert [p.name for p in paths] == ["Example [H49TUZY1pL0].stt.md", "Example [H49TUZY1pL0].stt.srt"]
    assert paths[1].read_text(encoding="utf-8") == core.build_srt(result)


@pytest.mark.parametrize("fallback", [False, True])
def test_worker_labels_srt_as_original_not_translation(monkeypatch, fallback):
    from yt_extractor import app as ui
    paths = [Path("original.md"), Path("original.srt"), Path("original.ko.md")]
    monkeypatch.setattr(ui, "extract_to_markdown", lambda *a, **k: paths)
    monkeypatch.setattr(ui, "extract_to_markdown_with_fallback", lambda *a, **k: paths)
    worker = ui.ExtractWorker(1, "H49TUZY1pL0", ".", {"transcript"}, ["en"], True,
                              core.TRANSCRIPT_SENTENCES, "192", translate_to="ko",
                              stt_fallback=fallback)
    results = []
    worker.signals.finished.connect(lambda job, result: results.append(result))
    worker.run()
    assert [label for label, _ in results[0]["files"]] == ["자막", "SRT 원본", "번역(ko)"]
