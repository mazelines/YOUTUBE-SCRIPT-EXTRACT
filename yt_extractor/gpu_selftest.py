"""Explicit, opt-in model inference check for GPU-enabled release artifacts."""
import json
import os
import time
from pathlib import Path

from . import __version__
from .gpu_runtime import runtime_info
from .local_llm import is_model_present, ensure_loaded, stream_local_chat


def run() -> int:
    report = {"version": __version__, "ok": False}
    started = time.perf_counter()
    try:
        if not is_model_present():
            raise RuntimeError("GPU selftest requires an already downloaded model.")
        ensure_loaded()
        report["load_and_warmup_seconds"] = round(time.perf_counter() - started, 3)
        report["runtime"] = runtime_info()
        info = report["runtime"]
        if not (info["gpu_layers"] > 0 and
                info["gpu_layers"] == info["total_layers"] and
                info["gpu_buffer_mib"] > 0):
            raise RuntimeError("Full GPU model placement was not confirmed.")
        started = time.perf_counter()
        answer = stream_local_chat([
            {"role": "user", "content": "다음 문장을 한국어로 번역하세요. "
             "The application extracts video transcripts and saves original "
             "subtitle timestamps. GPU acceleration runs the local language "
             "model, while the CPU continues to handle application controls."}
        ], max_tokens=192)
        report["inference_seconds"] = round(time.perf_counter() - started, 3)
        if len(answer.strip()) < 10:
            raise RuntimeError("Inference produced no meaningful output.")
        report["answer"] = answer
        report["ok"] = True
    except Exception as error:
        report["error"] = str(error)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    destination = os.environ.get("GPU_SELFTEST_OUT")
    if destination:
        Path(destination).write_text(serialized, encoding="utf-8")
    print(serialized)
    return 0 if report["ok"] else 1
