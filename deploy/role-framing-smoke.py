"""Живой тест: role="system" vs role="user" для СИСТЕМНЫХ ДИРЕКТИВ (Этап 5
плана ~/.claude/plans/vast-skipping-crane.md, "Разметка system vs user").
Прогнан 2026-09-28 на mycraft, итог — комментарий-живая находка рядом с
_DIRECTIVE_ROLE в llm/prompt.py (роль оставлена "user").

Не pytest/CI — разовый ручной скрипт, требует живой Ollama НА ТОЙ МАШИНЕ,
где она реально крутится (сейчас mycraft — persona_prompt/llm-prompt.toml
там же, локальный gitignored файл, ollama_url="http://127.0.0.1:11434" —
loopback-only, см. llm/ollama.py). Запуск: скопировать этот файл и
src-дерево на нужную ноду (или клонировать репозиторий) и выполнить
интерпретатором с pydantic/pydantic-settings в PYTHONPATH:
    python role-framing-smoke.py

Бьёт напрямую в llm.ollama.chat(), мимо bot/tasks/service, чтобы
изолировать вопрос именно про full_messages/role.

Две геометрии:
  (a) первое сообщение совсем нового треда (геометрия _offer_directive —
      pending_actions.py, адресат ещё не писал);
  (b) сообщение в хвосте существующего треда (геометрия tool_remind /
      _outcome_directive / _welcome_directive — собеседник уже писал раньше).

Для каждой геометрии — role="user" vs role="system" x N=5 повторов.
Печатает каждый сырой ответ с меткой варианта + эвристический грep по
"Принято|поручение|распоряжение" в начале ответа как быстрый фильтр.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# Предполагает, что рядом (в том же checkout'е) лежит src/sa_home_bot —
# см. докстринг выше про способ запуска (клон репозитория рядом со
# скриптом или сам скрипт внутри repo/deploy/).
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))

from sa_home_bot.config import Settings  # noqa: E402
from sa_home_bot.llm import ollama  # noqa: E402

# Путь к config.toml НОДЫ, на которой реально крутится Ollama (persona_prompt
# лежит рядом, в llm-prompt.toml, gitignored) — поправить под конкретную
# машину при повторном запуске.
CONFIG_PATH = "/home/sevboa/.config/sa-home-bot/config.toml"
N_REPEATS = 5

# Содержательная часть без band-aid-запрета (аналог directive до маркера) —
# ровно геометрия (a): офер-директива pending_actions._offer_directive,
# но без "Это НЕ поручение..." — именно то, что сейчас реально уходит
# в новый тред (после этапа 4 запрет добавляет только сам маркер).
DIRECTIVE_BODY_NEW_THREAD = (
    "Тебе, Альфреду, нужно передать весть — ты сам НЕ участник этого "
    "знакомства. Гость «Алексей Александрович Севбо» предложил(а) "
    "подтвердить знакомство С ЧЕЛОВЕКОМ, С КОТОРЫМ ТЫ СЕЙЧАС РАЗГОВАРИВАЕШЬ "
    "(не с тобой); после согласия ты сможешь передавать сообщения между ними. "
    "Коротко, своими словами сообщи суть. Сразу после твоего сообщения "
    "собеседник получит отдельную форму с кнопками «Принять» и «Отклонить» — "
    "решение принимается ТОЛЬКО кнопкой, скажи об этом. Сам ничего не "
    "подтверждай и не отклоняй, не проси ответить текстом."
)

# Геометрия (b): remind-подобная директива, дописываемая в хвост уже
# существующего треда с реальной историей.
DIRECTIVE_BODY_EXISTING_THREAD = (
    "Настало время: напомни собеседнику полить цветы, как он просил ранее."
)

FAKE_HISTORY = [
    {"role": "user", "content": "Привет, как дела?"},
    {"role": "assistant", "content": "Здравствуйте, сударыня. Всё в порядке, благодарю."},
]

GREP_PATTERN = re.compile(r"^\s*(Пг?ринято|поручение|погучение|распоряжение)", re.IGNORECASE)


async def run_variant(cfg, system: str, label: str, messages: list[dict]) -> None:
    for i in range(N_REPEATS):
        try:
            result = await ollama.chat(cfg, messages, system)
        except Exception as exc:  # noqa: BLE001
            print(f"[{label} #{i + 1}] ОШИБКА: {exc}")
            continue
        text = result.get("message", {}).get("content", "<нет content>")
        flag = "⚠️ ПОХОЖЕ НА ПОРУЧЕНИЕ" if GREP_PATTERN.search(text) else "ok"
        print(f"\n=== [{label} #{i + 1}] {flag} ===")
        print(text)


async def main() -> None:
    settings = Settings.load(CONFIG_PATH)
    cfg = settings.llm
    system = cfg.persona_prompt or "(ПУСТОЙ persona_prompt — фоллбэк)"
    print(f"persona_prompt длина: {len(system)} символов")
    print(f"model: {cfg.model}, ollama_url: {cfg.ollama_url}\n")

    print("\n\n########## ГЕОМЕТРИЯ (a): первое сообщение НОВОГО треда ##########")
    print('\n----- role="user" -----')
    await run_variant(
        cfg, system, "a/user", [{"role": "user", "content": DIRECTIVE_BODY_NEW_THREAD}]
    )
    print('\n----- role="system" -----')
    await run_variant(
        cfg, system, "a/system", [{"role": "system", "content": DIRECTIVE_BODY_NEW_THREAD}]
    )

    print("\n\n########## ГЕОМЕТРИЯ (b): хвост СУЩЕСТВУЮЩЕГО треда ##########")
    print('\n----- role="user" -----')
    await run_variant(
        cfg,
        system,
        "b/user",
        [*FAKE_HISTORY, {"role": "user", "content": DIRECTIVE_BODY_EXISTING_THREAD}],
    )
    print('\n----- role="system" -----')
    await run_variant(
        cfg,
        system,
        "b/system",
        [*FAKE_HISTORY, {"role": "system", "content": DIRECTIVE_BODY_EXISTING_THREAD}],
    )


asyncio.run(main())
