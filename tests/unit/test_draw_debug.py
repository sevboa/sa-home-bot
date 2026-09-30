"""Отладочный /draw (Этап 49): разбор команды и подпись."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import draw_debug
from sa_home_bot.bot.draw_debug import DrawCommand, DrawRequest, DrawSyntaxError


@pytest.mark.parametrize("args", [None, "", "  ", "help", "HELP", "?"])
def test_empty_and_help_give_memo(args):
    assert draw_debug.parse(args) == DrawCommand("help")


def test_service_commands():
    assert draw_debug.parse("clean") == DrawCommand("clean")
    assert draw_debug.parse("keep 12") == DrawCommand("keep", 12)
    assert draw_debug.parse("keep #12") == DrawCommand("keep", 12)
    with pytest.raises(DrawSyntaxError):
        draw_debug.parse("keep")


def test_plain_mode_and_description():
    req = draw_debug.parse("item старый радиопередатчик с антенной")
    assert isinstance(req, DrawRequest)
    assert req.mode == "item"
    assert req.description == "старый радиопередатчик с антенной"
    assert req.service_args() == {"description": req.description, "mode": "item"}


def test_keys_only_before_description():
    # «raw» внутри описания — часть описания, не ключ
    req = draw_debug.parse("item raw seed=42 s=0,6 raw meat on a plate")
    assert req.raw is True
    assert req.numbers == {"seed": 42, "strength": 0.6}
    assert req.description == "raw meat on a plate"


def test_ref_context_and_negative():
    req = draw_debug.parse(
        "scene ref=7 ip=0.35 nostyle Альфред держит передатчик "
        "| режиссёр: чердак | neg: people, text"
    )
    assert req.ref_id == 7
    assert req.style is False
    assert req.context == "чердак"
    assert req.negative == "people, text"
    args = req.service_args()
    assert args["ip_scale"] == 0.35 and args["style"] is False
    assert "ref" not in args  # образец кладёт обработчик байтами
    assert req.params()["ref"] == 7


@pytest.mark.parametrize(
    "text",
    [
        "paint кот",  # неизвестный режим
        "item",  # нет описания
        "item seed=abc кот",
        "item s=2 кот",  # вне диапазона
        "variant ip=0.4 кот",
        "scene s=0.5 кот",
    ],
)
def test_syntax_errors(text):
    with pytest.raises(DrawSyntaxError):
        draw_debug.parse(text)


def test_ref_rules():
    assert draw_debug.needs_ref(draw_debug.parse("variant кот"))
    assert draw_debug.accepts_ref(draw_debug.parse("scene кот"))
    assert not draw_debug.accepts_ref(draw_debug.parse("item кот"))


def test_caption_fits_and_escapes():
    req = draw_debug.parse("variant s=0.6 кот")
    result = {
        "seed": 123, "steps": 10, "seconds": 27.5, "prompt_seconds": 2.1, "tokens": 80,
        "full_prompt": "a <cat> " + "x" * 3000, "full_negative": "photo, " * 200,
    }
    text = draw_debug.caption(41, req, result, "#12")
    assert text.startswith("#41 · variant · seed 123 · s 0.6 · шагов 10 · 27.5 с (+промптер 2.1 с)")
    assert "образец #12" in text
    assert "&lt;cat&gt;" in text
    assert "больше 75" in text
    assert len(text) <= 1024


def test_help_mentions_every_mode_and_key():
    for word in (*draw_debug.MODES, "raw", "nostyle", "seed=", "s=", "ip=", "ref=", "keep",
                 "clean", "neg:", "px=", "colors=", "model=", *draw_debug.MODELS):
        assert word in draw_debug.HELP


def test_negative_without_pipe_is_not_glued_to_description():
    req = draw_debug.parse("scene raw girl neg: pants")
    assert req.description == "girl"
    assert req.negative == "pants"
    assert req.service_args()["negative"] == "pants"


def test_negative_without_pipe_after_context():
    req = draw_debug.parse("scene Альфред у окна | чердак негатив: люди, текст")
    assert req.context == "чердак"
    assert req.negative == "люди, текст"


def test_output_size_and_palette_keys():
    req = draw_debug.parse("scene px=128 colors=0 замок под луной")
    assert req.description == "замок под луной"
    assert req.service_args()["size"] == 128 and req.service_args()["colors"] == 0
    with pytest.raises(DrawSyntaxError):
        draw_debug.parse("scene px=1024 замок")


def test_caption_shows_output_size():
    req = draw_debug.parse("free px=128 кот")
    result = {"seed": 1, "steps": 6, "seconds": 30, "width": 128, "height": 128, "colors": 32}
    assert "128×128, 32 цв." in draw_debug.caption(1, req, result, None)
    result["colors"] = 0
    assert "128×128 ·" in draw_debug.caption(1, req, result, None)


def test_model_key():
    req = draw_debug.parse("item raw model=RV радио")
    assert req.description == "радио"
    assert req.service_args()["model"] == "rv" and req.params()["model"] == "rv"
    with pytest.raises(DrawSyntaxError):
        draw_debug.parse("item model=sdxl радио")
    with pytest.raises(DrawSyntaxError):
        draw_debug.parse("scene model=turbo ref=3 радио на столе")


def test_caption_shows_model():
    req = draw_debug.parse("item model=rv радио")
    result = {"seed": 1, "steps": 6, "seconds": 30, "model": "rv"}
    assert "#1 · item · rv · seed 1" in draw_debug.caption(1, req, result, None)


def test_help_fits_one_message():
    assert len(draw_debug.HELP) < 4096
