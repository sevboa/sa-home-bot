"""Сравнение версий вида "0.21.0" (semver-подобных, без пред-релизов)."""

from __future__ import annotations


def version_key(version: str) -> tuple[int, ...]:
    """Покомпонентное сравнение: "0.21.0" → (0, 21, 0).

    Нечисловые компоненты (например, случайный суффикс) считаются 0 —
    не роняет сравнение на неожиданном формате тега/версии.
    """
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)
