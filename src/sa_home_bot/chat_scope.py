from __future__ import annotations

from typing import Any

from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError


def require_chat_id(args: dict[str, Any], missing_hint: str) -> int:
    raw = args.get("chat_id")
    if raw is None:
        raise ProtoError(ERR_BAD_REQUEST, f"не указан chat_id — {missing_hint}")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc
