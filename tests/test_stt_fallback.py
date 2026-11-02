"""Tests for the STT fallback pipeline.

We don't hit the network or run real Whisper — instead we monkeypatch the
``core`` module's externals (``fetch_transcript_or_none``,
``fetch_transcript_with_polling``, ``is_live_video``, ``_download_for_stt``,
``_run_stt``) and assert the orchestrator ``extract_to_markdown_with_fallback``
takes the right branch under each combination of options.

The ``stt`` module's pure-Python helpers (model-path resolution, availability
probe) are tested directly without importing pywhispercpp.
"""

import unittest
from pathlib import Path
from unittest import mock

from yt_extractor import core, stt
from yt_extractor.core import (
    ExtractionError,
    TranscriptResult,
    VideoMeta,
    extract_to_markdown_with_fallback,
)


def _make_result(text="hello", lang_code="en"):
    return TranscriptResult(
        meta=VideoMeta(video_id="vid", title="t"),
        language="English",
        language_code=lang_code,
        is_generated=False,
        snippets=[(text, 0.0, 1.0)],
    )


def _make_stt_result(text="stt text", lang_code="en"):
    """Mirror what stt._run_stt actually produces.

    Sets origin="stt" (the field the orchestrator now keys the ``.stt.`` suffix
    off of) and keeps the human-readable Korean language label for realism.
    """
    return TranscriptResult(
        meta=VideoMeta(video_id="vid", title="t"),
        language=f"음성 인식 ({lang_code or 'auto'})",
        language_code=lang_code,
        is_generated=True,
        snippets=[(text, 0.0, 1.0)],
        origin="stt",
    )


class _BasePipelineTest(unittest.TestCase):
    """Common scaffolding: monkeypatch externals, capture progress + writes."""

    def setUp(self):
        # Patch every external the orchestrator can call. Each test overrides
        # the ones it cares about. Defaults model "caption present" so the
        # fallback paths are never taken unless a test opts in.
        self._patches = {
            "extract_video_id": mock.patch.object(
                core, "extract_video_id", return_value="vid",
            ),
            "fetch_video_meta": mock.patch.object(
                core, "fetch_video_meta",
                return_value=VideoMeta(video_id="vid", title="t"),
            ),
            "fetch_transcript_or_none": mock.patch.object(
                core, "fetch_transcript_or_none",
                return_value=_make_result(),
            ),
            "fetch_transcript_with_polling": mock.patch.object(
                core, "fetch_transcript_with_polling", return_value=None,
            ),
            "is_live_video": mock.patch.object(
                core, "is_live_video", return_value=False,
            ),
            "_download_for_stt": mock.patch.object(
                core, "_download_for_stt", return_value=Path("/tmp/a.mp3"),
            ),
            "_run_stt": mock.patch.object(
                core, "_run_stt", return_value=_make_stt_result(text="stt text"),
            ),
            "build_markdown": mock.patch.object(
                core, "build_markdown", return_value="md body",
            ),
            "save_markdown": mock.patch.object(
                core, "save_markdown", side_effect=self._fake_save,
            ),
            "is_stt_available": mock.patch.object(
                stt, "is_stt_available", return_value=True,
            ),
        }
        self._patches["is_model_present"] = mock.patch.object(stt, "is_model_present", return_value=True)
        self.mocks = {name: p.start() for name, p in self._patches.items()}
        self.addCleanup(self._stop_patches)
        self.saved_paths = []

    def _stop_patches(self):
        for p in self._patches.values():
            p.stop()

    def _fake_save(self, content, out_dir, filename):
        # Return a real Path object so callers can do .name / .exists().
        p = Path(out_dir) / filename
        self.saved_paths.append((content, filename))
        return p


class CaptionPresentTests(_BasePipelineTest):
    def test_uses_youtube_caption_when_available(self):
        # Captions exist -> STT/poll must never be invoked.
        paths = extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out",
            stt_fallback=True, poll_wait_min=30,
        )
        self.assertEqual(len(paths), 1)
        self.mocks["fetch_transcript_with_polling"].assert_not_called()
        self.mocks["_download_for_stt"].assert_not_called()
        self.mocks["_run_stt"].assert_not_called()
        # The saved filename uses the non-STT form.
        _, fname = self.saved_paths[0]
        self.assertNotIn(".stt.", fname)


class PollingTests(_BasePipelineTest):
    def test_polling_invoked_when_caption_missing(self):
        self.mocks["fetch_transcript_or_none"].return_value = None
        # Polling succeeds on the second attempt.
        self.mocks["fetch_transcript_with_polling"].return_value = _make_result()

        extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out", poll_wait_min=30,
        )
        self.mocks["fetch_transcript_with_polling"].assert_called_once()
        # STT should NOT be reached because polling returned a result.
        self.mocks["_download_for_stt"].assert_not_called()

    def test_stt_on_with_zero_poll_skips_polling(self):
        # Regression: previously `stt_fallback=True` silently forced a
        # 30-minute poll before reaching STT. Now poll_wait_min is independent
        # of stt_fallback — at 0, STT must be invoked directly with no poll.
        self.mocks["fetch_transcript_or_none"].return_value = None

        extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out",
            stt_fallback=True, poll_wait_min=0,
        )
        self.mocks["fetch_transcript_with_polling"].assert_not_called()
        self.mocks["_download_for_stt"].assert_called_once()
        self.mocks["_run_stt"].assert_called_once()

    def test_polling_only_without_stt_does_not_trigger_stt(self):
        # The reverse independence: polling on, STT off. After polling misses,
        # the pipeline must NOT fall through to STT.
        self.mocks["fetch_transcript_or_none"].return_value = None
        self.mocks["fetch_transcript_with_polling"].return_value = None

        with self.assertRaises(ExtractionError):
            extract_to_markdown_with_fallback(
                "https://youtu.be/vid", "/tmp/out",
                stt_fallback=False, poll_wait_min=5,
            )
        self.mocks["_download_for_stt"].assert_not_called()
        self.mocks["_run_stt"].assert_not_called()


class STTFallbackTests(_BasePipelineTest):
    def setUp(self):
        super().setUp()
        # For STT tests, captions are missing and there's no polling.
        self.mocks["fetch_transcript_or_none"].return_value = None

    def test_stt_path_invoked_for_captionless_vod(self):
        extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out",
            stt_fallback=True, poll_wait_min=0,
        )
        self.mocks["_download_for_stt"].assert_called_once()
        self.mocks["_run_stt"].assert_called_once()
        # STT-origin files are tagged with ".stt." so they're distinguishable.
        _, fname = self.saved_paths[0]
        self.assertIn(".stt.", fname)

    def test_live_without_capture_window_raises_actionable_error(self):
        self.mocks["is_live_video"].return_value = True
        with self.assertRaises(ExtractionError) as ctx:
            extract_to_markdown_with_fallback(
                "https://youtu.be/vid", "/tmp/out",
                stt_fallback=True, live_capture_minutes=0,
            )
        # The message must steer the user: either set a window or wait for VOD.
        self.assertIn("라이브", str(ctx.exception))
        self.mocks["_download_for_stt"].assert_not_called()

    def test_live_with_capture_window_downloads_then_stt(self):
        self.mocks["is_live_video"].return_value = True
        extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out",
            stt_fallback=True, live_capture_minutes=30,
        )
        self.mocks["_download_for_stt"].assert_called_once()
        # Confirm the capture window propagated into the downloader call.
        _args, kwargs = self.mocks["_download_for_stt"].call_args
        self.assertEqual(kwargs["live_capture_minutes"], 30)

    def test_missing_pywhispercpp_raises_install_hint(self):
        self.mocks["is_stt_available"].return_value = False
        with self.assertRaises(ExtractionError) as ctx:
            extract_to_markdown_with_fallback(
                "https://youtu.be/vid", "/tmp/out", stt_fallback=True,
            )
        self.assertIn("pywhispercpp", str(ctx.exception))

    def test_stt_returns_nothing_raises_error(self):
        self.mocks["_run_stt"].return_value = None
        with self.assertRaises(ExtractionError):
            extract_to_markdown_with_fallback(
                "https://youtu.be/vid", "/tmp/out", stt_fallback=True,
            )

    def test_stt_audio_cleaned_up_by_default(self):
        # _download_for_stt returns a real-ish Path; we can't easily inspect
        # unlink calls on it, but we can verify the orchestrator didn't raise
        # and that save happened. The unlink is best-effort and never fatal.
        extract_to_markdown_with_fallback(
            "https://youtu.be/vid", "/tmp/out", stt_fallback=True,
        )
        self.assertEqual(len(self.saved_paths), 1)


class NoFallbackTests(_BasePipelineTest):
    def test_no_fallback_and_no_caption_raises(self):
        self.mocks["fetch_transcript_or_none"].return_value = None
        with self.assertRaises(ExtractionError):
            extract_to_markdown_with_fallback(
                "https://youtu.be/vid", "/tmp/out",
                stt_fallback=False, poll_wait_min=0,
            )


class STTModuleHelpersTests(unittest.TestCase):
    """Pure-Python helpers in stt.py that don't need pywhispercpp."""

    def test_is_stt_available_returns_bool(self):
        # Either True or False, but never raises — even on a broken install.
        result = stt.is_stt_available()
        self.assertIsInstance(result, bool)

    def test_model_path_is_under_data_dir(self):
        p = stt.model_path("small")
        self.assertTrue(str(p).endswith("ggml-small.bin"))
        self.assertIn("stt-models", str(p))

    def test_model_url_for_known_sizes(self):
        self.assertEqual(
            stt._model_url("small"),
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
            "ggml-small.bin",
        )
        self.assertTrue(stt._model_url("large-v3").endswith("ggml-large-v3.bin"))

    def test_default_model_size_is_small(self):
        self.assertEqual(stt.DEFAULT_STT_MODEL, "small")
        self.assertIn("small", stt.STT_MODEL_SIZES)

    def test_is_model_present_false_for_fresh_dir(self):
        # A brand-new size name won't have any cached files.
        self.assertFalse(stt.is_model_present("definitely-not-cached-xyz"))


if __name__ == "__main__":
    unittest.main()
