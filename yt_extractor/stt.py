"""Built-in STT (speech-to-text) via whisper.cpp (pywhispercpp).

Mirrors ``local_llm.py``: pywhispercpp is an *optional* dependency. The heavy
import is deferred until first use so the app launches fine even where it isn't
installed. Models download from Hugging Face on first use and cache under the
user's app-data dir, the same way the GGUF model does.

Why whisper.cpp instead of faster-whisper: it shares the ggml backend with the
built-in LLM (llama.cpp), so a single Vulkan build accelerates STT on ANY GPU
vendor (NVIDIA/AMD/Intel) — no CUDA-only path, no ~600MB of cuBLAS/cuDNN
wheels. Machines without a usable GPU fall back to the AVX2 CPU backend
automatically inside whisper.cpp.

The output shape ``(text, start, duration)`` matches ``TranscriptResult.snippets``
so the rest of the pipeline (sentence/paragraph merge, markdown) works
unchanged for both real YouTube captions and STT-generated ones.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import requests


class STTError(Exception):
    """An STT request could not be completed (load / download / inference)."""


# Whisper model sizes we allow the user to pick. The default ``small`` is a
# good speed/accuracy tradeoff across languages including Korean; ``medium`` /
# ``large-v3`` improve accuracy at a higher inference cost. Sizes map to the
# GGML conversions that ggerganov/whisper.cpp publishes on Hugging Face.
STT_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_STT_MODEL = "small"

# ggerganov's official GGML conversions of the OpenAI checkpoints. Single-file
# models (no tokenizer/config sidecars), so a plain streamed download is the
# whole story — no huggingface_hub needed.
_MODEL_URL_TMPL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{size}.bin"
)

# Cached pywhispercpp Model instances keyed by size — loading is expensive.
_models: dict[str, object] = {}
# Guards the _models dict so concurrent QThreadPool workers don't each kick
# off a multi-GB model load for the same size before any of them finishes.
# With GUI default concurrency=4, four caption-less URLs submitted together
# would otherwise load the model four times.
_models_lock = threading.Lock()
# Guards transcription calls: a whisper.cpp context is not safe for concurrent
# whisper_full() runs, so we serialise transcribe across workers. Inference is
# the slow step, so this effectively caps STT throughput at 1 regardless of
# concurrency — but it's correct.
_transcribe_lock = threading.Lock()
_download_locks = {size: threading.Lock() for size in STT_MODEL_SIZES}


def _data_dir() -> Path:
    """Per-user cache dir for downloaded STT models.

    Reuses the same root as ``local_llm`` so a single folder holds every
    on-disk model the app manages.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
    d = Path(base) / "YouTubeTranscriptExtractor" / "stt-models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(size: str = DEFAULT_STT_MODEL) -> Path:
    """Local file where the GGML Whisper model of ``size`` is stored."""
    return _data_dir() / f"ggml-{size}.bin"


def _model_url(size: str) -> str:
    return _MODEL_URL_TMPL.format(size=size)


def is_stt_available() -> bool:
    """True if pywhispercpp imports (i.e. the optional dep is installed).

    This is checked at runtime to decide whether to enable STT UI; it never
    raises — the import is wrapped so a broken install doesn't crash startup.
    Importing ``pywhispercpp.model`` loads the compiled extension, so a build
    that can't load (missing runtime DLLs) correctly reports unavailable.
    """
    try:
        from pywhispercpp.model import Model  # noqa: F401
        return True
    except Exception:
        return False


def is_model_present(size: str = DEFAULT_STT_MODEL) -> bool:
    """True when the single-file GGML model exists and is non-empty."""
    p = model_path(size)
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def download_model(size: str = DEFAULT_STT_MODEL,
                   on_progress=None, should_cancel=None) -> None:
    """Stream the GGML Whisper model from Hugging Face, atomically.

    Writes to a ``.part`` file and renames on success, so a cancelled or
    failed download never leaves a truncated file that later looks valid
    (same pattern as ``local_llm.download_model``).

    ``on_progress(done_bytes, total_bytes)`` is called as data arrives.
    Raises STTError on any failure.
    """
    if size not in _download_locks:
        raise STTError(f"지원하지 않는 STT 모델: {size}")
    lock = _download_locks[size]
    while not lock.acquire(timeout=0.1):
        if should_cancel and should_cancel():
            raise STTError("다운로드가 취소되었습니다.")
    try:
        if should_cancel and should_cancel():
            raise STTError("다운로드가 취소되었습니다.")
        if not is_model_present(size):
            _download_model_file(size, on_progress, should_cancel)
    finally:
        lock.release()


def _download_model_file(size, on_progress, should_cancel):
    """Called with the model's download lock held, through atomic publication."""
    dest = model_path(size)
    tmp = dest.with_suffix(dest.suffix + ".part")
    url = _model_url(size)
    headers = {"User-Agent": "YouTubeTranscriptExtractor"}
    try:
        with requests.get(url, stream=True, timeout=60, headers=headers) as resp:
            if resp.status_code != 200:
                raise STTError(
                    f"STT 모델 다운로드 실패 (HTTP {resp.status_code}). "
                    "네트워크 상태를 확인하세요."
                )
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB
                    if should_cancel and should_cancel():
                        raise STTError("다운로드가 취소되었습니다.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
            # Guard against a silently truncated download: a short file would
            # still rename to the real path and then fail cryptically at load.
            if total > 0 and done != total:
                raise STTError(
                    f"다운로드가 불완전합니다 ({done:,}/{total:,} bytes). "
                    "다시 시도하세요."
                )
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise STTError(f"STT 모델 다운로드 중 연결 오류: {e}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)


def _load_audio(audio_path: str | Path):
    """Decode any audio/video file to the mono 16 kHz float32 PCM whisper
    expects, using the ffmpeg binary already shipped via imageio-ffmpeg.

    We decode ourselves instead of handing pywhispercpp a file path so the
    supported-formats story is identical to the rest of the app (whatever
    ffmpeg can read), rather than depending on whisper.cpp's built-in decoders.
    Returns a numpy float32 array. Raises STTError when decoding fails.
    """
    import numpy as np
    from imageio_ffmpeg import get_ffmpeg_exe

    cmd = [
        get_ffmpeg_exe(), "-v", "error",
        "-i", str(audio_path),
        "-f", "f32le", "-ac", "1", "-ar", "16000",
        "-",
    ]
    run_kwargs = {}
    if os.name == "nt":
        run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, capture_output=True, **run_kwargs)
    except OSError as e:
        raise STTError(f"오디오 디코딩(ffmpeg) 실행 실패: {e}")
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-300:]
        raise STTError(f"오디오 디코딩 실패: {tail}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise STTError("오디오 디코딩 결과가 비어 있습니다 (무음/손상 파일).")
    return audio


def _get_model(size: str = DEFAULT_STT_MODEL):
    """Load (once per size) and return a pywhispercpp Model.

    GPU selection:
    * ``STT_USE_GPU=0`` (or "false"/"no"): force the CPU backend.
    * Otherwise the GPU is used whenever the Vulkan backend can initialise —
      whisper.cpp itself falls back to CPU when no usable GPU/driver exists,
      so one build covers every machine (same story as the built-in LLM).
    """
    # Fast path: already loaded. Checked outside the lock so the common case
    # (model resident, several workers transcribing) doesn't serialise here.
    if size in _models:
        return _models[size]
    with _models_lock:
        # Re-check inside the lock: another worker may have loaded it while we
        # were waiting. This is the double-checked locking pattern.
        if size in _models:
            return _models[size]
        try:
            from pywhispercpp.model import Model
        except ImportError:
            raise STTError(
                "STT 기능에 필요한 pywhispercpp가 설치되어 있지 않습니다.\n"
                "requirements-stt.txt의 안내대로 설치하세요."
            )
        if not is_model_present(size):
            raise STTError(f"STT 모델({size})이 없습니다. 먼저 다운로드가 필요합니다.")

        gpu_pref = os.environ.get("STT_USE_GPU", "").lower()
        use_gpu = gpu_pref not in ("0", "false", "no")
        threads = max(1, (os.cpu_count() or 4) - 1)

        try:
            m = Model(
                str(model_path(size)),
                # Silence whisper.cpp's C-level log spam — the GUI app has no
                # console, and load/inference logs would just be lost noise.
                redirect_whispercpp_logs_to=None,
                n_threads=threads,
                context_params={"use_gpu": use_gpu},
            )
        except STTError:
            raise
        except Exception as e:
            if use_gpu:
                # GPU init can fail outright on broken Vulkan drivers. Retry
                # once on CPU so STT still completes instead of dying.
                try:
                    m = Model(
                        str(model_path(size)),
                        redirect_whispercpp_logs_to=None,
                        n_threads=threads,
                        context_params={"use_gpu": False},
                    )
                except Exception as e2:
                    raise STTError(f"STT 모델 로딩 실패: {e2}")
            else:
                raise STTError(f"STT 모델 로딩 실패: {e}")
        _models[size] = m
        return m


def transcribe_audio(audio_path: str | Path,
                     *,
                     model_size: str = DEFAULT_STT_MODEL,
                     language: str | None = None,
                     on_progress=None,
                     should_cancel=None) -> list[tuple[str, float, float]]:
    """Transcribe ``audio_path`` and return ``[(text, start, duration), ...]``.

    The return shape matches ``TranscriptResult.snippets`` exactly so the
    downstream markdown builder doesn't care where the captions came from.
    ``language`` (e.g. ``"en"``, ``"ko"``) skips the auto-detect step — pass
    ``None`` to let Whisper detect. ``on_progress(seg_idx)`` is called per
    emitted segment so the GUI can show "segment N" progress. Cancellation is
    checked inside whisper.cpp's abort callback, so a cancel takes effect
    mid-transcription (between ggml graph runs), not only at the end.
    """
    model = _get_model(model_size)
    audio = _load_audio(audio_path)

    # Serialise transcription across workers — a whisper.cpp context doesn't
    # support concurrent whisper_full() calls. This caps effective STT
    # parallelism at 1, which is fine: inference is the bottleneck, not I/O,
    # and model load is already serialised by _models_lock.
    with _transcribe_lock:
        seg_counter = {"n": 0}

        def _on_new_segment(_seg):
            # Progress only — never let a GUI-side error kill transcription.
            seg_counter["n"] += 1
            if on_progress:
                try:
                    on_progress(seg_counter["n"])
                except Exception:
                    pass

        def _abort() -> bool:
            try:
                return bool(should_cancel and should_cancel())
            except Exception:
                return False

        try:
            segments = model.transcribe(
                audio,
                # whisper.cpp treats "" as "run language detection first";
                # an explicit code like "ko" skips it. ("auto" also works but
                # triggers a harmless "unknown language" stderr warning.)
                language=(language or ""),
                translate=False,
                # Progress goes through new_segment_callback → the GUI; the
                # C-level "Progress: N%" stderr prints are just noise (and
                # invisible in the windowed app anyway).
                print_progress=False,
                new_segment_callback=_on_new_segment,
                abort_callback=_abort,
            )
        except Exception as e:
            raise STTError(f"STT 변환 실패: {e}")

        if should_cancel and should_cancel():
            raise STTError("STT 변환이 취소되었습니다.")

        # Segment times are in centiseconds (10 ms units — whisper.cpp's
        # native resolution), so /100 yields seconds.
        snippets: list[tuple[str, float, float]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            start = float(seg.t0) / 100.0
            duration = float(seg.t1 - seg.t0) / 100.0
            snippets.append((text, start, duration))

    return snippets


def detect_language(audio_path: str | Path,
                    *, model_size: str = DEFAULT_STT_MODEL) -> str | None:
    """Run only Whisper's 30s language detection (no transcription).

    Returns the detected language code (e.g. ``"en"``) or ``None`` on failure.
    Used to label STT-originated transcripts in the markdown header.
    """
    try:
        model = _get_model(model_size)
        audio = _load_audio(audio_path)
        with _transcribe_lock:
            (lang, _prob), _all = model.auto_detect_language(audio)
        return lang or None
    except Exception:
        return None


def is_loaded(size: str = DEFAULT_STT_MODEL) -> bool:
    """True once a model of ``size`` is resident in memory."""
    return size in _models
