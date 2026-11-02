"""Actual placement must not be inferred from n_gpu_layers or GPU support."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yt_extractor import local_llm, gpu_runtime


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    monkeypatch.setattr(local_llm, "_llm", None)
    monkeypatch.setattr(local_llm, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(local_llm, "is_model_present", lambda: True)
    monkeypatch.setattr(local_llm, "install_log_capture", lambda module: None)
    monkeypatch.delenv("YT_EXTRACTOR_FORCE_CPU", raising=False)
    gpu_runtime.begin_attempt()
    yield
    gpu_runtime.begin_attempt()


def engine(monkeypatch, factory, available=True):
    module = SimpleNamespace(Llama=factory, llama_backend_init=Mock(),
                             llama_supports_gpu_offload=lambda: available)
    monkeypatch.setitem(sys.modules, "llama_cpp", module)


def gpu_logs():
    for line in (
        "llama_model_load: using device Vulkan0 (NVIDIA GeForce RTX 4090) (0000:01:00.0) - 22000 MiB free",
        "load_tensors: offloaded 36/36 layers to GPU",
        "load_tensors:      Vulkan0 model buffer size = 4900.25 MiB",
    ):
        gpu_runtime.record_native_log(line)


def test_actual_offload_recorded_and_cached(monkeypatch, tmp_path):
    model = object()
    def load(**kwargs):
        assert kwargs["n_gpu_layers"] == -1
        gpu_logs()
        return model
    factory = Mock(side_effect=load)
    engine(monkeypatch, factory)
    assert local_llm._get_llm() is model
    assert local_llm._get_llm() is model
    factory.assert_called_once()
    info = json.loads((tmp_path / "model-runtime.json").read_text(encoding="utf-8"))
    assert info["gpu_layers"] == info["total_layers"] == 36
    assert info["gpu_buffer_mib"] == 4900.25
    assert "RTX 4090" in gpu_runtime.runtime_summary()


def test_capability_and_requested_layers_do_not_prove_gpu(monkeypatch):
    engine(monkeypatch, Mock(return_value=object()))
    local_llm._get_llm()
    assert gpu_runtime.runtime_summary().startswith("CPU")
    assert gpu_runtime.runtime_info()["gpu_layers"] == 0


def test_gpu_failure_discards_partial_placement_and_explains_cpu(monkeypatch):
    def load(**kwargs):
        if kwargs["n_gpu_layers"] == -1:
            gpu_logs()
            raise RuntimeError("GPU out of memory")
        return object()
    factory = Mock(side_effect=load)
    engine(monkeypatch, factory)
    local_llm._get_llm()
    assert [c.kwargs["n_gpu_layers"] for c in factory.call_args_list] == [-1, 0]
    info = gpu_runtime.runtime_info()
    assert info["gpu_layers"] == 0 and info["gpu_buffer_mib"] == 0
    assert "out of memory" in gpu_runtime.runtime_summary()


@pytest.mark.parametrize("forced", [False, True])
def test_cpu_engine_and_explicit_cpu_mode(monkeypatch, forced):
    factory = Mock(return_value=object())
    engine(monkeypatch, factory, available=forced)
    if forced:
        monkeypatch.setenv("YT_EXTRACTOR_FORCE_CPU", "1")
    local_llm._get_llm()
    assert factory.call_args.kwargs["n_gpu_layers"] == 0
    assert gpu_runtime.runtime_summary().startswith("CPU")


def test_logs_do_not_store_prompts_and_snapshot_is_independent():
    gpu_runtime.record_native_log("a private user question")
    assert gpu_runtime.runtime_info()["evidence"] == []
    info = gpu_runtime.runtime_info()
    info["evidence"].append("changed")
    assert gpu_runtime.runtime_info()["evidence"] == []
