from pathlib import Path

import yt_extractor.app as app_module
from yt_extractor.app import _default_output_dir


def test_default_output_dir_prefers_documents(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    appdata = tmp_path / "AppData"

    def fake_location(kind):
        if kind == app_module.QStandardPaths.DocumentsLocation:
            return str(docs)
        if kind == app_module.QStandardPaths.AppLocalDataLocation:
            return str(appdata)
        return ""

    monkeypatch.setattr(
        app_module.QStandardPaths, "writableLocation", staticmethod(fake_location)
    )

    out_dir = Path(_default_output_dir())

    assert out_dir == docs / "YouTubeTranscriptExtractor" / "transcripts"
    assert out_dir.exists()


def test_default_output_dir_falls_back_to_writable_path(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked-file"
    blocked.write_text("not a directory", encoding="utf-8")
    appdata = tmp_path / "AppData"

    def fake_location(kind):
        if kind == app_module.QStandardPaths.DocumentsLocation:
            return str(blocked)
        if kind == app_module.QStandardPaths.AppLocalDataLocation:
            return str(appdata)
        return ""

    monkeypatch.setattr(
        app_module.QStandardPaths, "writableLocation", staticmethod(fake_location)
    )

    out_dir = Path(_default_output_dir())

    assert out_dir == appdata / "transcripts"
    probe = out_dir / ".pytest-write"
    probe.write_text("ok", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "ok"
    probe.unlink()