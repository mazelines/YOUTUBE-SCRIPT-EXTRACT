"""Built-in local LLM: a small GGUF model run on CPU via llama-cpp-python.

The model is downloaded from Hugging Face on first use and cached under the
user's app-data dir, so the shipped app stays small and needs no manual setup.
The heavy import (`llama_cpp`) is deferred until the model is actually used, so
the app launches fine even where that optional dependency isn't installed yet.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from .llm import LLMError

# Gemma 4 E4B Instruct, Q4_K_M quant (~5.3 GB, needs ~6 GB RAM). The earlier
# 0.5B model was too weak — it echoed transcripts instead of summarizing — so we
# use Google's edge model: Apache-2.0, strong multilingual (incl. Korean), and
# good instruction-following. ggml-org's repo is ungated. Architecture "gemma4"
# is supported by the bundled llama.cpp (verified in llama.dll).
MODEL_REPO = "ggml-org/gemma-4-E4B-it-GGUF"
MODEL_FILE = "gemma-4-E4B-it-Q4_K_M.gguf"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/main/{MODEL_FILE}"

_llm = None  # cached llama_cpp.Llama instance (loading is expensive — do it once)


def _data_dir() -> Path:
    """Per-user cache dir for the downloaded model."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
    d = Path(base) / "YouTubeTranscriptExtractor" / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path() -> Path:
    return _data_dir() / MODEL_FILE


def is_model_present() -> bool:
    p = model_path()
    return p.exists() and p.stat().st_size > 0


def download_model(on_progress=None, should_cancel=None):
    """Stream the GGUF from Hugging Face to the cache, atomically.

    Writes to a `.part` file and renames on success, so a cancelled or failed
    download never leaves a truncated file that later looks valid.
    `on_progress(done_bytes, total_bytes)` is called as data arrives.
    """
    dest = model_path()
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": "YouTubeTranscriptExtractor"}
    try:
        with requests.get(MODEL_URL, stream=True, timeout=60,
                          headers=headers) as resp:
            if resp.status_code != 200:
                raise LLMError(
                    f"모델 다운로드 실패 (HTTP {resp.status_code}). "
                    "네트워크 상태를 확인하세요."
                )
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB
                    if should_cancel and should_cancel():
                        raise LLMError("다운로드가 취소되었습니다.")
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        # Guard against a silently truncated download: a short file would still
        # rename to the real path and then fail cryptically at model load.
        if total > 0 and done != total:
            raise LLMError(
                f"다운로드가 불완전합니다 ({done:,}/{total:,} bytes). "
                "다시 시도하세요."
            )
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise LLMError(f"모델 다운로드 중 연결 오류: {e}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)


def _get_llm():
    """Load (once) and return the in-process llama_cpp model."""
    global _llm
    if _llm is not None:
        return _llm
    try:
        from llama_cpp import Llama
    except ImportError:
        raise LLMError(
            "내장 모델 실행에 필요한 llama-cpp-python이 설치되어 있지 않습니다.\n"
            "requirements-ai.txt의 안내대로 설치하세요. (사전빌드 CPU 휠은 AVX512가 "
            "필요해 많은 CPU에서 충돌하므로 AVX2 소스 빌드를 권장합니다.)"
        )
    if not is_model_present():
        raise LLMError("모델 파일이 없습니다. 먼저 다운로드가 필요합니다.")
    # 16k context holds a Korean transcript (~1 char ≈ 1 token, so
    # MAX_CONTEXT_CHARS=12000 ≈ 12k tokens) plus room for the answer.
    # chat_format is left unset so llama-cpp-python uses the chat template
    # embedded in the GGUF metadata — correct for whatever model is loaded
    # (Gemma 4 here) without hardcoding a format.
    common = dict(
        model_path=str(model_path()),
        n_ctx=16384,
        n_threads=max(1, (os.cpu_count() or 4) - 1),
        verbose=False,
    )
    # Use the GPU when one is available (offload all layers), else CPU.
    # A CUDA-enabled build with no GPU still loads and just runs on CPU; the
    # try/except additionally catches the small-GPU out-of-memory case and
    # retries on CPU. Same single build covers GPU and CPU (Ollama-style).
    try:
        _llm = Llama(**common, n_gpu_layers=-1)
    except Exception:
        _llm = Llama(**common, n_gpu_layers=0)
    return _llm


def is_loaded() -> bool:
    """True once the model is resident in memory."""
    return _llm is not None


def ensure_loaded(*, on_status=None, on_progress=None, should_cancel=None):
    """Download (if missing) and load the model into memory, ready to use.

    Used to eagerly prepare the model at app startup. Safe to call repeatedly:
    the load happens once and the model then stays resident for the process
    lifetime (cached in the module-global `_llm`). Raises LLMError on failure.
    """
    if _llm is not None:
        return
    if not is_model_present():
        if on_status:
            on_status("내장 모델 다운로드 중… (최초 1회)")
        download_model(on_progress=on_progress, should_cancel=should_cancel)
    if should_cancel and should_cancel():
        return
    if on_status:
        on_status("내장 모델 로딩 중…")
    _get_llm()
    # Warm up the GPU once now. The Vulkan backend JIT-compiles its shader
    # pipelines on first use (a one-time ~10-20s cost on a long prompt), so we
    # pay it here during startup preload instead of on the user's first summary.
    if should_cancel and should_cancel():
        return
    if on_status:
        on_status("내장 모델 워밍업 중…")
    _warmup()


def _warmup():
    """Run a throwaway generation to force GPU pipeline/shader compilation."""
    try:
        # A longish prompt so prefill (large-batch) pipelines compile too.
        primer = "다음 내용을 한 문장으로 요약하세요.\n" + ("가나다라마바사아자차 " * 60)
        _get_llm().create_chat_completion(
            messages=[{"role": "user", "content": primer}], max_tokens=4,
        )
    except Exception:
        pass  # warmup is best-effort; never block startup on it


def stream_local_chat(messages, *, on_token=None, should_cancel=None,
                      temperature: float = 0.3, max_tokens: int = 4096) -> str:
    """Stream a chat completion from the in-process model.

    Same shape as llm.stream_chat so the GUI treats local and remote backends
    identically. `max_tokens` caps generation so a misbehaving model can't run
    away repeating itself. Raises LLMError on any failure.
    """
    llm = _get_llm()
    parts: list[str] = []
    try:
        stream = llm.create_chat_completion(
            messages=messages, stream=True, temperature=temperature,
            max_tokens=max_tokens,
        )
        for chunk in stream:
            if should_cancel and should_cancel():
                break
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            piece = delta.get("content")
            if piece:
                parts.append(piece)
                if on_token:
                    on_token(piece)
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"내장 모델 추론 오류: {e}")
    return "".join(parts)
