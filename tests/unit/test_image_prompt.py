"""Художник-промптер (llm/image_prompt.py): чистка стилевых тегов, подгонка
под 77 токенов CLIP, разбор ответа модели и откат на описание при сбое."""

from __future__ import annotations

import json

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import image_prompt

TEMPLATE = "{prompt}, video game art, flat shading"


def _words(text: str) -> int:
    return len(text.replace(",", " ").split())


def test_strip_style_tags_drops_style_and_quality():
    prompt = (
        "colossal eldritch monster, massive tentacles, dark stormy sky, "
        "cinematic lighting, epic scale, hyperrealism, oil painting style, 8k, "
        "detailed skin texture"
    )
    assert image_prompt.strip_style_tags(prompt) == (
        "colossal eldritch monster, massive tentacles, dark stormy sky"
    )


def test_strip_style_tags_keeps_painting_as_object():
    prompt = "a painting of red poppies, ornate gold frame, gallery wall"
    assert image_prompt.strip_style_tags(prompt) == prompt


def test_strip_style_tags_all_style_returns_original():
    assert image_prompt.strip_style_tags("cinematic, 8k") == "cinematic, 8k"


def test_fit_prompt_drops_tail_tags_until_template_fits():
    prompt = "dragon, castle, sunset, clouds, birds, river"
    fitted = image_prompt.fit_prompt(prompt, TEMPLATE, _words, limit=8)
    # dragon castle sunset + 5 слов шаблона = 8
    assert fitted == "dragon, castle, sunset"


def test_fit_prompt_never_drops_main_subject():
    assert image_prompt.fit_prompt("a huge red dragon", TEMPLATE, _words, limit=2) == (
        "a huge red dragon"
    )


def test_fit_prompt_short_prompt_untouched():
    assert image_prompt.fit_prompt("cat, sofa", TEMPLATE, _words) == "cat, sofa"


async def test_compose_parses_json_and_strips_style(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, *, tools, think, response_format):
        seen.update(messages=messages, format=response_format, think=think)
        content = json.dumps(
            {"prompt": "ginger cat, blue sofa, cinematic lighting", "negative": "dogs"}
        )
        return {"message": {"content": content}}

    monkeypatch.setattr(image_prompt.ollama, "chat", fake_chat)
    result = await image_prompt.compose("рыжий кот на синем диване", LlmConfig(), think=False)
    assert result == ("ginger cat, blue sofa", "dogs")
    assert seen["messages"] == [{"role": "user", "content": "рыжий кот на синем диване"}]
    assert seen["format"] == "json"
    assert seen["think"] is False


async def test_compose_falls_back_to_description_on_bad_answer(monkeypatch):
    async def fake_chat(*args, **kwargs):
        return {"message": {"content": "не JSON"}}

    monkeypatch.setattr(image_prompt.ollama, "chat", fake_chat)
    result = await image_prompt.compose("a cat, sofa, 8k", LlmConfig())
    assert result == ("a cat, sofa", "")
