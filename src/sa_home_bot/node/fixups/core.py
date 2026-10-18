from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sa_home_bot.config import Settings


class FixupError(Exception):
    pass


@dataclass(frozen=True)
class Fixup:
    id: str
    title: str
    needed: Callable[[Settings], bool]
    check: Callable[[], bool]
    apply: Callable[[], None]


def _check_or_false(fixup: Fixup) -> bool:
    try:
        return fixup.check()
    except FixupError as exc:
        print(f"  ⚠️ {fixup.title}: проверка не удалась ({exc}) — пробую применить")
        return False


def run_fixups(fixups: list[Fixup]) -> list[str]:
    failed: list[str] = []
    for fixup in fixups:
        if _check_or_false(fixup):
            print(f"  ✅ {fixup.title} — уже применено")
            continue
        print(f"  ⏳ {fixup.title} — применяю (может спросить пароль sudo)...")
        try:
            fixup.apply()
        except FixupError as exc:
            print(f"  ❌ {fixup.title}: {exc}")
            failed.append(fixup.id)
            continue
        if _check_or_false(fixup):
            print(f"  ✅ {fixup.title} — применено")
        else:
            print(f"  ⚠️ {fixup.title}: команда прошла, но проверка всё ещё отрицательна")
            failed.append(fixup.id)
    return failed
