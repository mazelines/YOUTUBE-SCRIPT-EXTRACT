"""Built-in STT (speech-to-text) via faster-whisper.

Mirrors ``local_llm.py``: faster-whisper is an *optional* dependency. The heavy
import is deferred until first use so the app launches fine even where it isn't
installed. Models download from Hugging Face on first use and cache under the
user's app-data dir, the same way the GGUF model does.

The output shape ``(text, start, duration)`` matches ``TranscriptResult.snippets``
so the rest of the pipeline (sentence/paragraph merge, markdown) works
unchanged for both real YouTube captions and STT-generated ones.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import requests


class STTError(Exception):
    """An STT request could not be completed (load / download / inference)."""


# Whisper model sizes we allow the user to pick. The default ``small`` is a
# good speed/accuracy tradeoff across languages including Korean; ``medium`` /
# ``large-v3`` improve accuracy at a steep CPU cost. Sizes map to the official
# OpenAI checkpoint names that SYSTRAN/faster-whisper converts on download.
STT_MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_STT_MODEL = "small"

# Cached WhisperModel instances keyed by size — loading is expensive.
_models: dict[str, object] = {}
# Guards the _models dict so concurrent QThreadPool workers don't each kick
# off a multi-GB WhisperModel load for the same size before any of them
# finishes. With GUI default concurrency=4, four caption-less URLs submitted
# together would otherwise load the model four times.
_models_lock = threading.Lock()
# Guards transcription calls: a single WhisperModel instance isn't guaranteed
# thread-safe (CTranslate2 sessions share state), so we serialise transcribe
# across workers. Inference is the slow step, so this effectively caps STT
# throughput at 1 regardless of concurrency — but it's correct.
_transcribe_lock = threading.Lock()


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
    """Local directory where a Whisper model of ``size`` is materialised.

    faster-whisper normally pulls from the HF cache, but we symlink/copy a
    named directory here too so ``is_model_present`` can give a definitive
    "ready" answer and users can wipe one folder to reset.
    """
    return _data_dir() / size


def _hf_repo_for(size: str) -> tuple[str, str]:
    """Return (repo_id, filename) for ``size`` on Hugging Face.

    We use SYSTRAN's converted-CTranslate2 mirrors, which faster-whisper loads
    directly without an on-the-fly conversion step.
    """
    if size == "large-v3":
        return ("Systran/faster-whisper-large-v3", "model.bin")
    return (f"Systran/faster-whisper-{size}", "model.bin")


def is_stt_available() -> bool:
    """True if faster-whisper imports (i.e. the optional dep is installed).

    This is checked at runtime to decide whether to enable STT UI; it never
    raises — the import is wrapped so a broken install doesn't crash startup.
    """
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def is_model_present(size: str = DEFAULT_STT_MODEL) -> bool:
    """True when the model directory contains the expected weight file.

    faster-whisper also keeps a copy in the HF cache (``~/.cache/huggingface``)
    that's reused across runs, so a model only counts as "absent" if *neither*
    location has it. The HF cache is the authoritative source — our own
    ``model_path`` is just a marker so the UI can show a clear download state.
    """
    if (model_path(size) / "model.bin").exists():
        return True
    # Fall back to the HF hub cache: faster-whisper uses the standard cache.
    try:
        from huggingface_hub import hf_hub_url, try_to_load_from_cache  # noqa
        repo, fname = _hf_repo_for(size)
        cached = try_to_load_from_cache(repo, fname)
        if cached is not None and Path(cached).exists():
            return True
    except Exception:
        pass
    return False


def _purge_model_dir(dest: Path) -> None:
    """Remove a (possibly partial) model directory so the next download starts
    clean. Best-effort: any failure is swallowed because this runs from error
    paths where raising would mask the original problem.
    """
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
    except Exception:
        pass


def download_model(size: str = DEFAULT_STT_MODEL,
                   on_progress=None, should_cancel=None) -> None:
    """Download a Whisper checkpoint via huggingface_hub if available.

    We prefer ``huggingface_hub.snapshot_download`` (the same path
    faster-whisper uses internally) so the result lands in the HF cache that
    ``_get_model`` reads from. If huggingface_hub isn't installed, we fall back
    to a plain ``requests`` stream of ``model.bin`` into our own dir — slower
    (no tokenizer files) but enough for a self-contained STT.

    ``on_progress(done_bytes, total_bytes)`` is called as data arrives.
    Raises STTError on any failure; a cancelled/aborted download leaves no
    partial state that later looks valid.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        snapshot_download = None

    repo, _ = _hf_repo_for(size)
    if snapshot_download is not None:
        # Clean up any partial download from a previous failed attempt before
        # re-running — huggingface_hub can leave config.json etc. behind on a
        # network timeout, then on the *next* call report "already downloaded"
        # and let our marker touch succeed even though model.bin is missing.
        # _purge_model_dir is best-effort and never raises.
        dest = model_path(size)
        try:
            # Note: ``local_dir_use_symlinks`` was removed in huggingface_hub
            # >= 1.0 (it's the default behaviour when ``local_dir`` is set),
            # so we don't pass it — older versions also accept its absence and
            # default to copying.
            snapshot_download(repo, local_dir=str(dest))
        except Exception as e:
            # snapshot_download may have written a partial tree; remove it so
            # the next attempt starts clean instead of resuming into a broken
            # state.
            _purge_model_dir(dest)
            raise STTError(f"STT 모델 다운로드 실패: {e}")
        # Verify the real model weight actually landed before trusting the
        # directory. snapshot_download can return without raising even when
        # only some files were fetched (resume-from-cache quirks), in which
        # case touching a marker would later make is_model_present lie and
        # _get_model would segfault on the missing model.bin.
        bin_path = dest / "model.bin"
        if not bin_path.exists() or bin_path.stat().st_size == 0:
            _purge_model_dir(dest)
            raise STTError(
                f"STT 모델 다운로드가 불완전합니다 (model.bin 누락). "
                "네트워크 상태를 확인하고 다시 시도하세요."
            )
        # Marker so is_model_present is fast next time even if HF cache
        # gets cleared. Only created after the integrity check above passed.
        bin_path.touch(exist_ok=True)
        return

    # Fallback: stream model.bin only. Good enough for a self-contained run,
    # though faster-whisper may re-fetch tokenizer config on first load.
    dest = model_path(size)
    dest.mkdir(parents=True, exist_ok=True)
    bin_path = dest / "model.bin"
    tmp = bin_path.with_suffix(".bin.part")
    url = f"https://huggingface.co/{repo}/resolve/main/model.bin"
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
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if should_cancel and should_cancel():
                        raise STTError("다운로드가 취소되었습니다.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
            if total > 0 and done != total:
                raise STTError(
                    f"다운로드가 불완전합니다 ({done:,}/{total:,} bytes)."
                )
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise STTError(f"STT 모델 다운로드 중 연결 오류: {e}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, bin_path)


def _resolve_model_arg(size: str) -> str:
    """Decide what to pass to WhisperModel — a local dir if we have it, else
    the bare size name so faster-whisper downloads to its HF cache itself."""
    local = model_path(size)
    if (local / "model.bin").exists() and (local / "config.json").exists():
        return str(local)
    return size


# Cached result of the CUDA-DLL probe so we only scan site-packages once.
_cuda_dlls_ok: bool | None = None


def _ensure_cuda_dlls() -> bool:
    """Locate NVIDIA CUDA DLLs (cublas/cudnn/nvrtc) installed via pip wheels
    and expose them to ctranslate2. Returns True if the required DLLs look
    present, False if CUDA can't be made to work (caller falls back to CPU).

    ctranslate2 loads ``cublas64_12.dll`` etc. via LoadLibrary, which on
    Windows searches PATH + os.add_dll_directory() targets — it does NOT
    search inside ``site-packages/nvidia/...`` automatically. The
    ``nvidia-cublas-cu12`` (and cudnn/nvrtc) pip packages drop the DLLs
    exactly there, so we have to register those bin dirs ourselves.

    Idempotent: the result is memoised in ``_cuda_dlls_ok`` so the directory
    scan happens only once per process.
    """
    global _cuda_dlls_ok
    if _cuda_dlls_ok is not None:
        return _cuda_dlls_ok

    try:
        import importlib.util

        # Find site-packages by looking up any pip-installed dist.
        # importlib.util.find_spec on a stdlib module gives its origin; we
        # actually want the *site-packages* dir, which we get from the
        # nvidia namespace package if it exists.
        import importlib
        spec = importlib.util.find_spec("faster_whisper")
        if spec is None or not spec.submodule_search_locations:
            _cuda_dlls_ok = False
            return False
        site_packages = list(spec.submodule_search_locations)[0]
        nvidia_root = Path(site_packages).parent / "nvidia"

        added = 0
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            bin_dir = nvidia_root / sub / "bin"
            if not bin_dir.is_dir():
                continue
            # os.add_dll_directory is the modern (3.8+) way and is required
            # because PATH-only discovery is unreliable for subprocess-launched
            # Python. We also prepend to PATH as belt-and-suspenders.
            try:
                os.add_dll_directory(str(bin_dir.resolve()))
            except (OSError, FileNotFoundError):
                pass
            os.environ["PATH"] = (
                str(bin_dir.resolve()) + os.pathsep + os.environ.get("PATH", "")
            )
            added += 1
        # Need all three (cublas + cudnn + nvrtc) to trust cuda float16.
        _cuda_dlls_ok = added >= 3
    except Exception:
        _cuda_dlls_ok = False
    return _cuda_dlls_ok



def _get_model(size: str = DEFAULT_STT_MODEL):
    """Load (once per size) and return a faster-whisper WhisperModel.

    Device selection:
    * If ``STT_USE_CUDA`` env var is set (1/true/yes): force CUDA float16.
    * If ``STT_USE_CUDA=0`` (or "false"/"no"): force CPU int8.
    * Otherwise: auto-detect. Use CUDA when the cublas/cudnn/nvrtc DLLs are
      installed (via the nvidia-*-cu12 pip wheels) — that's a 5-15x speedup
      for medium+ models on a discrete GPU. Fall back to CPU int8 otherwise.

    The naive "try cuda, catch at construction" pattern is unsafe for
    faster-whisper: ``WhisperModel(device="cuda")`` succeeds at construction
    (lazy CUDA context) and only fails inside ``transcribe()`` with a DLL
    load error like "cublas64_12.dll is not found" — past our fallback point.
    So we probe the DLLs explicitly with ``_ensure_cuda_dlls`` before
    attempting cuda, rather than catching the construction error.
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
            from faster_whisper import WhisperModel
        except ImportError:
            raise STTError(
                "STT 기능에 필요한 faster-whisper가 설치되어 있지 않습니다.\n"
                "requirements-stt.txt의 안내대로 설치하세요 "
                "(pip install -r requirements-stt.txt)."
            )
        if not is_model_present(size):
            raise STTError(f"STT 모델({size})이 없습니다. 먼저 다운로드가 필요합니다.")

        model_arg = _resolve_model_arg(size)
        threads = max(1, (os.cpu_count() or 4) - 1)

        # Decide cuda vs cpu BEFORE constructing WhisperModel — see docstring.
        cuda_pref = os.environ.get("STT_USE_CUDA", "").lower()
        if cuda_pref in ("0", "false", "no"):
            want_cuda = False
        elif cuda_pref in ("1", "true", "yes"):
            want_cuda = True
        else:
            want_cuda = _ensure_cuda_dlls()

        if want_cuda:
            try:
                m = WhisperModel(model_arg, device="cuda", compute_type="float16")
                _models[size] = m
                return m
            except Exception as e:
                # DLL probe said cuda should work but model load still failed
                # (e.g. out-of-VRAM, driver/runtime mismatch). Surface once
                # and fall through to CPU so STT still completes.
                import warnings
                warnings.warn(
                    f"CUDA 로딩 실패, CPU로 전환: {e}"
                )
        try:
            m = WhisperModel(
                model_arg, device="cpu", compute_type="int8", cpu_threads=threads,
            )
        except Exception as e:
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
    honoured between segments.
    """
    try:
        model = _get_model(model_size)
    except STTError:
        raise

    # Serialise transcription across workers. faster-whisper's WhisperModel is
    # not documented as thread-safe (CTranslate2 sessions share internal state),
    # so concurrent transcribe() calls on one cached model could race. This
    # caps effective STT parallelism at 1, which is fine — inference is the
    # bottleneck, not I/O, and model load is already serialised by _models_lock.
    with _transcribe_lock:
        try:
            segments, info = model.transcribe(
                str(audio_path),
                language=language,
                task="transcribe",
                beam_size=5,
                # VAD is OFF. faster-whisper ships Silero VAD, which is trained
                # on conversational speech — it classifies *singing* (music,
                # songs, melodic speech) as non-speech and filters it out, so
                # music videos / lyric videos / live concert streams produced
                # zero segments with vad_filter=True. Whisper itself handles
                # silence well via its own attention, so the VAD trade-off
                # (skip silence vs. drop singing) isn't worth it for the
                # general YouTube corpus this app targets.
                vad_filter=False,
                without_timestamps=False,
            )
        except Exception as e:
            raise STTError(f"STT 변환 시작 실패: {e}")

        # faster-whisper returns a *generator*; iteration does the actual work,
        # so we walk it here and surface progress + cancellation per segment.
        snippets: list[tuple[str, float, float]] = []
        try:
            for i, seg in enumerate(segments):
                if should_cancel and should_cancel():
                    raise STTError("STT 변환이 취소되었습니다.")
                text = (seg.text or "").strip()
                if not text:
                    continue
                snippets.append((text, float(seg.start), float(seg.end - seg.start)))
                if on_progress:
                    on_progress(i + 1)
        except STTError:
            raise
        except Exception as e:
            raise STTError(f"STT 변환 중 오류: {e}")

    return snippets


def detect_language(audio_path: str | Path,
                    *, model_size: str = DEFAULT_STT_MODEL) -> str | None:
    """Run only Whisper's 30s language detection (no transcription).

    Returns the detected language code (e.g. ``"en"``) or ``None`` on failure.
    Used to label STT-originated transcripts in the markdown header.
    """
    try:
        model = _get_model(model_size)
        _, info = model.transcribe(
            str(audio_path), language=None, task="transcribe",
            beam_size=1, vad_filter=False, without_timestamps=True,
        )
        return getattr(info, "language", None)
    except Exception:
        return None


def is_loaded(size: str = DEFAULT_STT_MODEL) -> bool:
    """True once a model of ``size`` is resident in memory."""
    return size in _models
