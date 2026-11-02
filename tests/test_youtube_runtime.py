from pathlib import Path
from unittest.mock import MagicMock

from yt_extractor import core


def test_frozen_runtime_works_without_system_node(tmp_path, monkeypatch):
    node = tmp_path / "node" / "node.exe"
    node.parent.mkdir()
    node.touch()
    monkeypatch.setattr(core.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)
    assert core._youtube_runtime_opts() == {"js_runtimes": {"node": {"path": str(node)}}}


def test_development_node_enabled_explicitly(monkeypatch):
    monkeypatch.delattr(core.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda name: "node-path")
    assert core._youtube_runtime_opts() == {"js_runtimes": {"node": {"path": "node-path"}}}


def test_absent_node_retains_default_deno_detection(monkeypatch):
    monkeypatch.delattr(core.sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(core.shutil, "which", lambda name: None)
    assert core._youtube_runtime_opts() == {}


def test_video_download_passes_runtime_and_preserves_height(tmp_path, monkeypatch):
    import yt_dlp
    final = tmp_path / "video.mp4"
    final.write_bytes(b"video")
    options = {}
    ydl = MagicMock()

    def create(opts):
        options.update(opts)
        return ydl

    def extract(*args, **kwargs):
        for hook in options["postprocessor_hooks"]:
            hook({"status": "finished", "info_dict": {"filepath": str(final)}})
        return {"title": "video"}

    ydl.__enter__.return_value.extract_info.side_effect = extract
    monkeypatch.setattr(yt_dlp, "YoutubeDL", create)
    monkeypatch.setattr(core, "_ffmpeg_location", lambda: None)
    monkeypatch.setattr(core, "_youtube_runtime_opts", lambda: {"js_runtimes": {"node": {"path": "bundled-node"}}})
    assert core.extract_video("H49TUZY1pL0", tmp_path, height=720) == final
    assert options["js_runtimes"]["node"]["path"] == "bundled-node"
    assert "height<=720" in options["format"]
    assert options["merge_output_format"] == "mp4"
