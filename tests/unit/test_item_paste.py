"""Этап 49.3: вставка сюжетного предмета (llm/item_paste.py) и действия службы
llm ``item_portrait`` / ``generate_image`` с ``paste`` (генератор, зрение и
rembg замоканы — diffusers и rembg в dev-окружении нет)."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image

from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import item_paste
from sa_home_bot.llm import service as llm_service
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _cut(w=100, h=60) -> Image.Image:
    """RGBA: белый прямоугольник на прозрачном поле с отступами."""
    im = Image.new("RGBA", (w + 20, h + 20), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (w, h), (250, 250, 250, 255)), (10, 10))
    return im


def _svc(tmp_path, **llm) -> LlmService:
    llm.setdefault("idle_sleep_after_s", 1800.0)
    llm.setdefault("items_dir", tmp_path / "items")
    return LlmService(Settings(llm=LlmConfig(model="qwen2.5:7b", imagegen_enabled=True, **llm)))


def _png(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="PNG")
    return buf.getvalue()


# --- чистые функции ---


def test_paste_places_object_by_bottom_center_and_width():
    scene = Image.new("RGB", (512, 512), (40, 40, 40))
    where = item_paste.Placement(cx=256, bottom=400, width=200)
    pasted, mask, box = item_paste.paste(scene, _cut(), where)
    assert box == (156, 280, 356, 400)  # 100x60 → 200x120, низ на 400
    assert mask.getbbox() == box
    # Предмет светлее фона, но подогнан к тёмной сцене (не чисто белый).
    assert 40 < pasted.getpixel((256, 340))[0] < 250
    # Тень правее-ниже предмета темнит фон.
    assert pasted.getpixel((362, 404))[0] < 40
    assert pasted.getpixel((20, 20)) == (40, 40, 40)


def test_paste_keeps_object_inside_frame():
    scene = Image.new("RGB", (512, 512), "black")
    _, _, box = item_paste.paste(scene, _cut(), item_paste.Placement(cx=500, bottom=600, width=300))
    assert box[0] >= 0 and box[2] <= 512 and box[3] <= 512


def test_paste_empty_cut_is_error():
    with pytest.raises(ValueError):
        item_paste.paste(
            Image.new("RGB", (512, 512)), Image.new("RGBA", (10, 10)), item_paste.PLACEMENTS["desk"]
        )


def test_restore_returns_object_inside_and_harmonized_outside():
    pasted = Image.new("RGB", (64, 64), (200, 0, 0))
    harmonized = Image.new("RGB", (64, 64), (0, 0, 200))
    mask = Image.new("L", (64, 64), 0)
    mask.paste(255, (16, 16, 48, 48))
    out = item_paste.restore(harmonized, pasted, mask)
    assert out.getpixel((32, 32)) == (200, 0, 0)
    assert out.getpixel((2, 2)) == (0, 0, 200)


def test_desk_placement_from_gemma_box():
    # box_2d: [ymin, xmin, ymax, xmax]
    where = item_paste.desk_placement([500, 250, 700, 750])
    assert where == item_paste.Placement(cx=256, bottom=(256 + 0.6 * 102.4), width=0.8 * 256)
    # Узкая, высокая (дальний стол у окна), кривая — места нет.
    assert item_paste.desk_placement([500, 450, 700, 550]) is None
    assert item_paste.desk_placement([100, 100, 200, 900]) is None
    assert item_paste.desk_placement(None) is None
    assert item_paste.desk_placement([1, 2, 3]) is None
    assert item_paste.desk_placement(["a", 1, 2, 3]) is None


def test_cut_path_rejects_bad_keys(tmp_path):
    cfg = LlmConfig(model="m", items_dir=tmp_path)
    assert item_paste.cut_path(cfg, "radio-5-ab12") == tmp_path / "radio-5-ab12.png"
    for bad in ("../x", "Radio", "", "a/b"):
        with pytest.raises(ValueError):
            item_paste.cut_path(cfg, bad)


# --- item_portrait ---


async def test_item_portrait_retries_seed_on_miss_and_saves_cut(tmp_path, monkeypatch):
    calls = []

    async def fake_generate(prompt, negative, cfg, **kw):
        calls.append({"prompt": prompt, **kw})
        return {
            "png": _png(),
            "width": 64,
            "height": 64,
            "seconds": 10.0,
            "seed": kw["seed"],
            "original": Image.new("RGB", (512, 512), "grey"),
            "full_prompt": prompt,
        }

    answers = iter(
        [
            {"description": "Радио без микрофона.", "missing": ["mic"], "answers": {"mic": False}},
            {"description": "Радио с микрофоном.", "missing": [], "answers": {"mic": True}},
        ]
    )

    async def fake_inspect(image_b64, expect, cfg, *, think=None):
        assert expect == ["old radio", "mic :: a plug does NOT count"]
        return next(answers)

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    monkeypatch.setattr(llm_service.photo_check, "inspect", fake_inspect)
    monkeypatch.setattr(llm_service.item_paste, "cut_out", lambda im, cfg: _cut())
    svc = _svc(tmp_path)
    result = await svc.run_command(
        "item_portrait",
        {
            "prompt": "1950s ham radio,  dusty",
            "key": "radio-7-1",
            "seed": 100,
            "checks": ["old radio", "mic :: a plug does NOT count"],
        },
    )
    assert [c["seed"] for c in calls] == [100, 101]
    assert calls[0]["prompt"] == "1950s ham radio, dusty, " + llm_service.ITEM_PORTRAIT_BACKGROUND
    assert calls[0]["model"] == "turbo" and calls[0]["fit"] is False
    assert calls[0]["weighted"] is True
    assert result["seed"] == 101 and result["attempts"] == 2 and result["missing"] == []
    assert result["seconds"] == 20.0 and result["seen"] == "Радио с микрофоном."
    assert base64.b64decode(result["png_b64"]) == _png()
    saved = Image.open(tmp_path / "items" / "radio-7-1.png")
    assert saved.mode == "RGBA"


async def test_item_portrait_keeps_best_attempt_when_all_miss(tmp_path, monkeypatch):
    async def fake_generate(prompt, negative, cfg, **kw):
        return {
            "png": _png((kw["seed"], 0, 0)),
            "width": 64,
            "height": 64,
            "seconds": 1.0,
            "seed": kw["seed"],
            "original": Image.new("RGB", (512, 512)),
        }

    misses = iter([["a", "b"], ["a"], ["a", "b"]])

    async def fake_inspect(image_b64, expect, cfg, *, think=None):
        m = next(misses)
        return {"description": "x", "missing": m, "answers": {}}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    monkeypatch.setattr(llm_service.photo_check, "inspect", fake_inspect)
    monkeypatch.setattr(llm_service.item_paste, "cut_out", lambda im, cfg: _cut())
    result = await _svc(tmp_path).run_command(
        "item_portrait", {"prompt": "radio", "key": "r1", "seed": 5, "checks": ["a", "b"]}
    )
    assert result["seed"] == 6 and result["missing"] == ["a"] and result["attempts"] == 3


async def test_item_portrait_validates_key(tmp_path):
    with pytest.raises(ProtoError) as excinfo:
        await _svc(tmp_path).run_command("item_portrait", {"prompt": "radio", "key": "../x"})
    assert excinfo.value.code == ERR_BAD_REQUEST


# --- generate_image + paste ---


async def test_generate_image_paste_harmonizes_and_restores_item(tmp_path, monkeypatch):
    (tmp_path / "items").mkdir()
    _cut().save(tmp_path / "items" / "radio-7-1.png")
    calls = []

    async def fake_generate(prompt, negative, cfg, **kw):
        calls.append({"prompt": prompt, "negative": negative, **kw})
        color = (30, 30, 30) if kw.get("ref") is None else (0, 0, 220)
        return {
            "png": _png(),
            "width": 64,
            "height": 64,
            "seconds": 5.0,
            "seed": 42,
            "prompt": "dark study",
            "full_negative": "blurry",
            "original": Image.new("RGB", (512, 512), color),
        }

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    asked = []

    async def fake_chat(cfg, messages, system, **kw):
        asked.append(messages[0]["content"])
        # Сетка 0..1000: столешница в нижней половине на всю ширину.
        return {"message": {"content": '{"box_2d": [600, 100, 800, 900]}'}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    result = await _svc(tmp_path).run_command(
        "generate_image",
        {
            "prompt": "dark study",
            "model": "revanim",
            "loras": [["rottech", 0.8]],
            "paste": {"key": "radio-7-1", "place": "desk", "hint": "old radio on the desk"},
        },
    )
    scene, harm = calls
    assert scene.get("ref") is None
    assert harm["ref"] is not None and harm["strength"] == 0.35 and harm["seed"] == 42
    assert harm["loras"] == [("rottech", 0.4)] and harm["model"] == "revanim"
    # Композиция под стол — в начале промпта и сцены, и гармонизации.
    assert scene["prompt"] == f"{item_paste.DESK_COMPOSITION}, dark study"
    assert harm["prompt"] == "dark study, old radio on the desk"
    assert result["seconds"] == 10.0
    assert asked == [item_paste.DESK_BOX_QUESTION]
    x0, y0, x1, y1 = result["item_box"]
    # Низ предмета — 60% глубины столешницы: 600 + 0.6·200 → 720/1000 кадра.
    assert y1 == int(0.72 * 512) and x1 - x0 == int(0.42 * 512)
    final = Image.open(io.BytesIO(base64.b64decode(result["png_b64"]))).convert("RGB")
    # Итог — уменьшенный кадр (не PNG первой генерации): центр предмета —
    # вставленный светлый предмет, угол — гармонизированный фон.
    w = final.width
    assert final.getpixel((w // 2, int(w * (y0 + y1) / 2 / 512)))[0] > 60
    assert final.getpixel((1, 1))[2] > 150


async def test_generate_image_paste_validation(tmp_path):
    svc = _svc(tmp_path)
    for paste in ({"key": "nope"}, {"key": "Bad!"}, {"key": "r1", "place": "floor"}, "x"):
        with pytest.raises(ProtoError) as excinfo:
            await svc.run_command("generate_image", {"prompt": "room", "paste": paste})
        assert excinfo.value.code == ERR_BAD_REQUEST


async def test_item_portrait_preview_rejects_seed_before_full_render(tmp_path, monkeypatch):
    """49.3.4: черновик не прошёл — прогон прерван, следующее зерно; вторая
    проверка готового кадра не нужна (вердикт черновика)."""
    calls = []

    async def fake_generate(prompt, negative, cfg, **kw):
        calls.append(kw)
        draft = Image.new("RGB", (64, 64))
        # Как в проде: черновик проверяется из потока генерации.
        if kw["preview"] is not None and not await asyncio.to_thread(kw["preview"], draft):
            raise llm_service.imagegen.PreviewRejected
        return {
            "png": _png(),
            "width": 64,
            "height": 64,
            "seconds": 3.0,
            "seed": kw["seed"],
            "original": Image.new("RGB", (512, 512)),
        }

    verdicts = iter([["mic"], []])
    inspected = []

    async def fake_inspect(image_b64, expect, cfg, *, think=None):
        inspected.append(expect)
        return {"description": "x", "missing": next(verdicts), "answers": {}}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    monkeypatch.setattr(llm_service.photo_check, "inspect", fake_inspect)
    monkeypatch.setattr(llm_service.item_paste, "cut_out", lambda im, cfg: _cut())
    result = await _svc(tmp_path, item_preview_check=True).run_command(
        "item_portrait", {"prompt": "radio", "key": "r1", "seed": 5, "checks": ["mic"]}
    )
    assert [c["seed"] for c in calls] == [5, 6]
    assert result["seed"] == 6 and result["missing"] == [] and len(inspected) == 2


async def test_item_portrait_restyle_curses_the_checked_portrait(tmp_path, monkeypatch):
    calls = []

    async def fake_generate(prompt, negative, cfg, **kw):
        calls.append({"prompt": prompt, **kw})
        cursed = kw.get("ref") is not None
        return {
            "png": _png((200, 0, 0) if cursed else (0, 0, 0)),
            "width": 64,
            "height": 64,
            "seconds": 2.0,
            "seed": kw["seed"],
            "original": Image.new("RGB", (512, 512)),
        }

    cut_from = []
    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    monkeypatch.setattr(
        llm_service.item_paste, "cut_out", lambda im, cfg: cut_from.append(im) or _cut()
    )
    result = await _svc(tmp_path).run_command(
        "item_portrait",
        {
            "prompt": "(radio)1.1",
            "key": "r1",
            "seed": 3,
            "restyle": {
                "model": "revanim",
                "loras": [["rottech", 0.8]],
                "strength": 0.4,
                "prompt": "old radio, rust",
            },
        },
    )
    base, cursed = calls
    assert cursed["ref"] is not None and cursed["strength"] == 0.4 and cursed["seed"] == 3
    assert cursed["model"] == "revanim" and cursed["loras"] == [("rottech", 0.8)]
    assert cursed["prompt"].startswith("old radio, rust")
    assert base64.b64decode(result["png_b64"]) == _png((200, 0, 0))
    assert result["seed"] == 3 and result["seconds"] == 4.0


async def test_item_portrait_bad_restyle_is_refused_before_drawing(tmp_path, monkeypatch):
    async def fake_generate(*a, **kw):
        raise AssertionError("не должен рисовать")

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    with pytest.raises(ProtoError):
        await _svc(tmp_path).run_command(
            "item_portrait",
            {
                "prompt": "radio",
                "key": "r1",
                "restyle": {"model": "dream", "strength": 2},
            },
        )
