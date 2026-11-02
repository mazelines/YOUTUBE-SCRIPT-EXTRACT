"""Download/STT regressions with isolated files and no external network."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, MagicMock
import subprocess
import threading

import imageio_ffmpeg
import pytest
from yt_extractor import core, stt


def test_live_capture_mp3_sidecar_with_real_ffmpeg(tmp_path, monkeypatch):
    ydl = MagicMock()
    ydl.YoutubeDL.return_value.__enter__.return_value.extract_info.return_value = {
        "title": "Live", "url": "https://example.invalid/live"
    }
    run = subprocess.run

    def synthetic_input(cmd, **kwargs):
        # Replace the network source only. Exercise the actual output arguments
        # against ffmpeg so missing -f mp3 fails just as it does in production.
        output_args = cmd[cmd.index("-t"):]
        return run([cmd[0], "-v", "error", "-f", "lavfi", "-i", "anullsrc"]
                   + output_args, **kwargs)

    monkeypatch.setattr(subprocess, "run", synthetic_input)
    output = core._capture_live_audio(
        ydl_module=ydl, video_id="abcdefghijk", out_dir=tmp_path,
        capture_minutes=1 / 60, ffmpeg_exe=imageio_ffmpeg.get_ffmpeg_exe(),
    )
    assert output.suffix == ".mp3"
    assert output.stat().st_size > 0
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("keep_audio,fail", [(False, False), (False, True), (True, False)])
def test_stt_audio_isolated_and_existing_mp3_preserved(tmp_path, monkeypatch, keep_audio, fail):
    existing = tmp_path / "Video [abcdefghijk].mp3"
    existing.write_bytes(b"user audio")
    monkeypatch.setattr(core, "fetch_video_meta", lambda *a: core.VideoMeta("abcdefghijk", "Video"))
    monkeypatch.setattr(core, "fetch_transcript_or_none", lambda *a, **k: None)
    monkeypatch.setattr(core, "is_live_video", lambda *a: False)
    monkeypatch.setattr(stt, "is_stt_available", lambda: True)
    monkeypatch.setattr(stt, "is_model_present", lambda *a: True)
    download_dirs = []

    def download(url, out_dir, **kwargs):
        folder = Path(out_dir)
        download_dirs.append(folder)
        assert folder != tmp_path
        audio = folder / existing.name
        audio.write_bytes(b"temporary audio")
        (folder / "fragment.part").write_bytes(b"partial")
        return audio

    def transcribe(*a, **k):
        if fail:
            raise core.ExtractionError("inference failed")
        return core.TranscriptResult(None, "English", "en", True,
                                     [("hello", 0, 1)], origin="stt")

    monkeypatch.setattr(core, "_download_for_stt", download)
    monkeypatch.setattr(core, "_run_stt", transcribe)
    if fail:
        with pytest.raises(core.ExtractionError, match="inference failed"):
            core.extract_to_markdown_with_fallback("abcdefghijk", tmp_path, stt_fallback=True)
    else:
        core.extract_to_markdown_with_fallback("abcdefghijk", tmp_path,
                                              stt_fallback=True, keep_audio=keep_audio)
    assert existing.read_bytes() == b"user audio"
    assert not download_dirs[0].exists()
    copies = list(tmp_path.glob("*.stt-*.mp3"))
    assert len(copies) == int(keep_audio)
    if copies:
        assert copies[0].read_bytes() == b"temporary audio"


def test_concurrent_stt_download_fetches_once(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "_data_dir", lambda: tmp_path)
    barrier = threading.Barrier(4)
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.headers = {"Content-Length": "8"}
    response.iter_content.return_value = [b"1234", b"5678"]
    get = Mock(return_value=response)
    monkeypatch.setattr(stt.requests, "get", get)

    def download():
        barrier.wait(timeout=5)
        stt.download_model("small")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download) for _ in range(4)]
        for future in futures:
            future.result(timeout=5)
    get.assert_called_once()
    assert stt.model_path("small").read_bytes() == b"12345678"
    assert not list(tmp_path.glob("*.part"))


def test_cancelled_waiter_does_not_remove_active_download(tmp_path, monkeypatch):
    monkeypatch.setattr(stt, "_data_dir", lambda: tmp_path)
    part = tmp_path / "ggml-small.bin.part"
    part.write_bytes(b"active download")
    lock = stt._download_locks["small"]
    with lock:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stt.download_model, "small", should_cancel=lambda: True)
            with pytest.raises(stt.STTError):
                future.result(timeout=5)
    assert part.read_bytes() == b"active download"
