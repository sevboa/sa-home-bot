"""llm/model_profiles.py — резолв профиля по имени модели и перевод
намерения-уровня рассуждения в параметр Ollama ``think``."""

from __future__ import annotations

import textwrap

import pytest

from sa_home_bot.llm.model_profiles import (
    REASON_LEVELS,
    ModelProfile,
    ModelProfileSummary,
    load_profiles,
)


def test_packaged_profiles_load_and_have_default():
    reg = load_profiles(None)
    names = [p.name for p in reg.profiles]
    assert "_default" in names
    # _default идёт последним (фоллбэк), у него пустой match
    assert reg.profiles[-1].name == "_default"
    assert reg.profiles[-1].match == ()


@pytest.mark.parametrize(
    ("model", "expected", "matched"),
    [
        ("hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-IQ4_XS", "gemma-4", True),
        ("gemma4:26b", "gemma-4", True),
        ("gpt-oss:20b", "reasoning-effort", True),
        ("qwen3.8:27b", "reasoning-effort", True),
        ("qwen2.5:7b", "_default", False),
        ("llama3.1:8b", "_default", False),
    ],
)
def test_resolve_matches_by_substring(model, expected, matched):
    reg = load_profiles(None)
    profile, was_matched = reg.resolve(model)
    assert profile.name == expected
    assert was_matched is matched


def test_num_ctx_default_from_packaged_profile():
    reg = load_profiles(None)
    gemma, _ = reg.resolve("gemma-4-26B")
    assert gemma.num_ctx == 14336


@pytest.mark.parametrize(
    ("control", "reason", "expected"),
    [
        ("flag", "off", False),
        ("flag", "low", True),
        ("flag", "high", True),
        ("flag_implicit_on", "off", False),
        ("flag_implicit_on", "medium", None),  # флаг не слать — модель думает сама
        ("effort", "off", False),
        ("effort", "low", "low"),
        ("effort", "medium", "medium"),
        ("effort", "high", "high"),
        ("none", "off", None),
        ("none", "high", None),
    ],
)
def test_think_arg_translation(control, reason, expected):
    p = ModelProfile(name="x", think_control=control)
    assert p.think_arg(reason) == expected


def test_all_reason_levels_translate_without_error():
    for control in ("flag", "flag_implicit_on", "effort", "none"):
        p = ModelProfile(name="x", think_control=control)
        for level in REASON_LEVELS:
            p.think_arg(level)  # не должно кидать


def test_summary_roundtrip():
    p = ModelProfile(name="x", router=False, thinking_hidden=True, num_ctx=12000)
    payload = p.summary().to_payload()
    back = ModelProfileSummary.from_payload(payload)
    assert back == p.summary()
    assert back.router is False
    assert back.thinking_hidden is True


def test_local_override_replaces_by_name_and_appends_new(tmp_path):
    local = tmp_path / "model-profiles.toml"
    local.write_text(
        textwrap.dedent(
            """
            [[profile]]
            name = "gemma-4"
            match = ["gemma-4"]
            think_control = "effort"
            num_ctx = 9000

            [[profile]]
            name = "my-model"
            match = ["my-model"]
            think_control = "none"
            router = false
            """
        ),
        encoding="utf-8",
    )
    reg = load_profiles(local)
    gemma, _ = reg.resolve("gemma-4:latest")
    assert gemma.think_control == "effort"  # заменён локальным
    assert gemma.num_ctx == 9000
    mine, matched = reg.resolve("my-model:1")
    assert matched and mine.think_control == "none" and mine.router is False
    # новая запись вставлена ПЕРЕД _default
    assert reg.profiles[-1].name == "_default"


def test_unknown_think_control_falls_back_to_flag(tmp_path, caplog):
    local = tmp_path / "model-profiles.toml"
    local.write_text(
        '[[profile]]\nname="weird"\nmatch=["weird"]\nthink_control="telepathy"\n',
        encoding="utf-8",
    )
    reg = load_profiles(local)
    p, _ = reg.resolve("weird-1")
    assert p.think_control == "flag"


def test_broken_local_file_is_ignored(tmp_path):
    local = tmp_path / "model-profiles.toml"
    local.write_text("this is not = valid toml [[[", encoding="utf-8")
    reg = load_profiles(local)  # не кидает
    assert any(p.name == "_default" for p in reg.profiles)
