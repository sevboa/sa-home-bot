from __future__ import annotations

import sys

import pytest
from PIL import Image

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import imagegen
from sa_home_bot.llm.imagegen import MODELS, pipeline_device


@pytest.fixture(autouse=True)
def _clean_pipelines():
    imagegen._pipelines.clear()
    yield
    imagegen._pipelines.clear()


def test_device_cpu_by_default_for_sd15():
    assert pipeline_device(MODELS["dream"], LlmConfig()) == "cpu"


def test_device_cuda_for_sd15_when_config_cuda():
    cfg = LlmConfig(imagegen_device="cuda")
    assert pipeline_device(MODELS["dream"], cfg) == "cuda"
    assert pipeline_device(MODELS["rv"], cfg) == "cuda"


def test_device_cuda_never_for_sdxl_turbo():
    cfg = LlmConfig(imagegen_device="cuda")
    assert pipeline_device(MODELS["turbo"], cfg) == "cpu"


def test_device_cpu_for_sd15_when_config_cpu():
    cfg = LlmConfig(imagegen_device="cpu")
    assert pipeline_device(MODELS["dream"], cfg) == "cpu"


async def test_pipelines_cache_keeps_cpu_and_cuda_entries_separately(monkeypatch):
    imagegen._pipelines.clear()
    loads: list[tuple[str, str]] = []

    def fake_load(spec, cfg, device):
        loads.append((spec.repo, device))
        return f"pipe-{device}"

    monkeypatch.setattr(imagegen, "_load_pipeline_sync", fake_load)
    cfg = LlmConfig(imagegen_device="cuda")
    cuda_loaded = await imagegen._get_pipeline(MODELS["dream"], cfg)
    cpu_loaded = await imagegen._get_pipeline(MODELS["dream"], cfg, "cpu")
    again = await imagegen._get_pipeline(MODELS["dream"], cfg)
    assert loads == [("Lykon/dreamshaper-8", "cuda"), ("Lykon/dreamshaper-8", "cpu")]
    assert cuda_loaded.device == "cuda" and cpu_loaded.device == "cpu"
    assert cuda_loaded.pipe == "pipe-cuda" and cpu_loaded.pipe == "pipe-cpu"
    assert again is cuda_loaded
    assert sorted(imagegen._pipelines) == [
        ("Lykon/dreamshaper-8", "cpu"),
        ("Lykon/dreamshaper-8", "cuda"),
    ]


async def test_get_pipeline_uses_cpu_for_turbo_even_with_cuda_config(monkeypatch):
    imagegen._pipelines.clear()
    loads: list[tuple[str, str]] = []

    def fake_load(spec, cfg, device):
        loads.append((spec.repo, device))
        return f"pipe-{device}"

    monkeypatch.setattr(imagegen, "_load_pipeline_sync", fake_load)
    cfg = LlmConfig(imagegen_device="cuda")
    loaded = await imagegen._get_pipeline(MODELS["turbo"], cfg)
    assert loaded.device == "cpu"
    assert loads == [("stabilityai/sdxl-turbo", "cpu")]


async def test_pipelines_cache_evicts_by_repo_ignoring_device(monkeypatch):
    imagegen._pipelines.clear()

    def fake_load(spec, cfg, device):
        return f"pipe-{spec.repo}-{device}"

    monkeypatch.setattr(imagegen, "_load_pipeline_sync", fake_load)
    cfg = LlmConfig(imagegen_device="cuda", imagegen_model="Lykon/dreamshaper-8")
    for name in ("rv", "epic", "cyber", "ghostmix"):
        await imagegen._get_pipeline(MODELS[name], cfg)
    kept = sorted(repo for repo, _ in imagegen._pipelines)
    assert kept == ["cyberdelia/CyberRealistic", "digiplay/GhostMix", "emilianJR/epiCRealism"]


class _Gen:
    def manual_seed(self, seed):
        return self


class _Tok:
    def __call__(self, text):
        class _R:
            input_ids = [0] * (len(text.split()) + 2)

        return _R()


class _Pipe:
    tokenizer = _Tok()


class OutOfMemoryError(Exception):
    pass


def _fake_torch(monkeypatch) -> list[int]:
    module = type(sys)("torch")
    module.Generator = _Gen
    calls: list[int] = []

    class _Cuda:
        def empty_cache(self):
            calls.append(1)

    module.cuda = _Cuda()
    monkeypatch.setitem(sys.modules, "torch", module)
    return calls


async def _prepare_draw(monkeypatch, exc: BaseException, fail_on: int):
    imagegen._pipelines.clear()
    caches = _fake_torch(monkeypatch)
    loads: list[str] = []
    attempts: list[str] = []

    async def fake_get(spec, cfg, device=None):
        device = device or imagegen.pipeline_device(spec, cfg)
        loads.append(device)
        return imagegen._Loaded(_Pipe(), spec, device)

    def fake_sync(loaded, prompt, negative, cfg, job):
        attempts.append(loaded.device)
        if len(attempts) == fail_on:
            raise exc
        return Image.new("RGB", (512, 512))

    monkeypatch.setattr(imagegen, "_get_pipeline", fake_get)
    monkeypatch.setattr(imagegen, "_generate_sync", fake_sync)
    return loads, attempts, caches


def test_out_of_memory_is_detected_by_class_name():
    assert imagegen.is_out_of_memory(OutOfMemoryError("no space"))
    assert not imagegen.is_out_of_memory(RuntimeError("out of memory"))
    assert not imagegen.is_out_of_memory(ValueError("x"))


async def test_cuda_frame_retries_once_on_cpu_after_oom(monkeypatch):
    cfg = LlmConfig(imagegen_device="cuda")
    loads, attempts, caches = await _prepare_draw(monkeypatch, OutOfMemoryError("no space"), 1)
    result = await imagegen.generate_image("radio", "", cfg)
    assert loads == ["cuda", "cpu"]
    assert attempts == ["cuda", "cpu"]
    assert result["device"] == "cpu"
    assert caches


async def test_cuda_frame_keeps_device_when_generation_succeeds(monkeypatch):
    cfg = LlmConfig(imagegen_device="cuda")
    loads, attempts, caches = await _prepare_draw(monkeypatch, OutOfMemoryError("no space"), 99)
    result = await imagegen.generate_image("radio", "", cfg)
    assert loads == ["cuda"]
    assert attempts == ["cuda"]
    assert result["device"] == "cuda"
    assert caches


async def test_ordinary_error_is_not_retried(monkeypatch):
    cfg = LlmConfig(imagegen_device="cuda")
    loads, attempts, caches = await _prepare_draw(monkeypatch, RuntimeError("boom"), 1)
    with pytest.raises(RuntimeError):
        await imagegen.generate_image("radio", "", cfg)
    assert loads == ["cuda"]
    assert attempts == ["cuda"]
    assert caches


async def test_cpu_frame_reports_cpu_device_and_skips_empty_cache(monkeypatch):
    cfg = LlmConfig()
    _, attempts, caches = await _prepare_draw(monkeypatch, OutOfMemoryError("no space"), 99)
    result = await imagegen.generate_image("radio", "", cfg)
    assert attempts == ["cpu"]
    assert result["device"] == "cpu"
    assert not caches


def test_release_gpu_drops_only_cuda_entries(monkeypatch):
    _fake_torch(monkeypatch)
    imagegen._pipelines.clear()
    imagegen._pipelines[("a", "cpu")] = imagegen._Loaded("cpu-a", MODELS["dream"], "cpu")
    imagegen._pipelines[("a", "cuda")] = imagegen._Loaded("cuda-a", MODELS["dream"], "cuda")
    imagegen._pipelines[("b", "cuda")] = imagegen._Loaded("cuda-b", MODELS["rv"], "cuda")
    assert imagegen.release_gpu() == 2
    assert list(imagegen._pipelines) == [("a", "cpu")]


def test_release_gpu_returns_zero_without_cuda_entries(monkeypatch):
    caches = _fake_torch(monkeypatch)
    imagegen._pipelines.clear()
    imagegen._pipelines[("a", "cpu")] = imagegen._Loaded("cpu-a", MODELS["dream"], "cpu")
    assert imagegen.release_gpu() == 0
    assert not caches


class AcceleratorError(Exception):
    pass


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AcceleratorError("CUDA error: out of memory"), True),
        (AcceleratorError("CUDA error: illegal memory access"), False),
    ],
)
def test_accelerator_out_of_memory_is_detected(exc, expected):
    assert imagegen.is_out_of_memory(exc) is expected


class _OffloadPipe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enable_model_cpu_offload(self):
        self.calls.append("model")

    def enable_sequential_cpu_offload(self):
        self.calls.append("sequential")

    def remove_all_hooks(self):
        self.calls.append("remove")


@pytest.mark.parametrize(("mode", "call"), [("model", "model"), ("sequential", "sequential")])
def test_enable_offload_picks_mode(mode, call):
    pipe = _OffloadPipe()
    imagegen.enable_offload(pipe, mode)
    assert pipe.calls == [call]


def test_switch_offload_only_on_change_and_without_offload_keeps_active_mode():
    pipe = _OffloadPipe()
    loaded = imagegen._Loaded(pipe, MODELS["dream"], "cuda", "model")
    imagegen._switch_offload(loaded, "model")
    assert pipe.calls == []
    imagegen._switch_offload(loaded, "sequential")
    assert loaded.active_offload == "sequential"
    assert loaded.offload == "model"
    changed: list[int] = []
    imagegen._without_offload(loaded, lambda: changed.append(1))
    assert pipe.calls == ["sequential", "remove", "sequential"]
    assert changed == [1]


async def test_cuda_pipeline_remembers_offload_mode_from_config(monkeypatch):
    imagegen._pipelines.clear()
    monkeypatch.setattr(imagegen, "_load_pipeline_sync", lambda spec, cfg, device: "pipe")
    cfg = LlmConfig(imagegen_device="cuda", imagegen_gpu_offload="model")
    cuda_loaded = await imagegen._get_pipeline(MODELS["dream"], cfg)
    cpu_loaded = await imagegen._get_pipeline(MODELS["dream"], cfg, "cpu")
    assert (cuda_loaded.offload, cuda_loaded.active_offload) == ("model", "model")
    assert cpu_loaded.offload == ""


def test_gpu_offload_defaults_to_sequential():
    assert LlmConfig().imagegen_gpu_offload == "sequential"
