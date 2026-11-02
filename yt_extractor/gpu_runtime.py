"""Record actual llama.cpp placement, rather than requested GPU settings."""
from __future__ import annotations

import re
import os
from copy import deepcopy

_callback = None  # Keep the ctypes callback alive for the entire native lifetime.
_state = {}


def begin_attempt(reason=""):
    _state.clear()
    _state.update(pid=os.getpid(), loaded=False, backend="CPU", device="", gpu_layers=0,
                  total_layers=0, gpu_buffer_mib=0.0, reason=reason, evidence=[])


begin_attempt()


def record_native_log(text: str):
    """Consume only device/placement diagnostics; never retain prompt text."""
    device = re.search(r"using device (\S+) \(([^)]+)\)", text)
    layers = re.search(r"offloaded (\d+)/(\d+) layers to GPU", text)
    buffer = re.search(r"(Vulkan\d+|CUDA\d+|SYCL\d+) model buffer size\s*=\s*([\d.]+) MiB", text)
    if device:
        _state["device"] = device.group(2)
        _state["backend"] = re.sub(r"\d+$", "", device.group(1))
    if layers:
        _state["gpu_layers"], _state["total_layers"] = map(int, layers.groups())
    if buffer:
        _state["backend"] = re.sub(r"\d+$", "", buffer.group(1))
        _state["gpu_buffer_mib"] += float(buffer.group(2))
    if device or layers or buffer:
        _state["evidence"].append(text.strip())


def install_log_capture(llama_cpp):
    global _callback
    if _callback is None:
        @llama_cpp.llama_log_callback
        def callback(level, text, user_data):
            # An exception must never cross the native callback boundary.
            try:
                record_native_log(text.decode("utf-8", errors="replace"))
            except Exception:
                pass
        _callback = callback
    llama_cpp.llama_log_set(_callback, None)


def mark_loaded():
    _state["loaded"] = True
    if not _state["gpu_layers"]:
        _state["backend"] = "CPU"
        if not _state["reason"]:
            _state["reason"] = "GPU 레이어 배치가 확인되지 않아 CPU로 동작합니다."


def runtime_info():
    return deepcopy(_state)


def runtime_summary():
    if not _state["loaded"]:
        return "AI 실행 장치 확인 중"
    if _state["gpu_layers"] > 0:
        device = _state["device"] or _state["backend"]
        return f"GPU · {device} · {_state['gpu_layers']}/{_state['total_layers']} 레이어"
    return f"CPU · {_state['reason']}"
