from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_NEEDS_PRIVILEGE, ActionSpec, ProtoError
from sa_home_bot.subscriptions.models import Subscription

APPS_SERVICE = "apps"

QBITTORRENT_APP_ID = "qbittorrent"

HIDDEN_MENU_APP_IDS = frozenset({QBITTORRENT_APP_ID})

_APP_STATUS = {
    "active": "✅ работает",
    "inactive": "⏹ остановлен",
    "failed": "❌ упал",
}


def visible_skill_actions(
    subscription: Subscription | None, app_actions: Sequence[ActionSpec]
) -> list[ActionSpec]:
    return [
        action
        for action in app_actions
        if not action.params
        and action.id not in HIDDEN_MENU_APP_IDS
        and commands.can_run_action(subscription, action.id, APPS_SERVICE)
    ]


def render_app_card(app: dict) -> str:
    status = _APP_STATUS.get(app.get("status", ""), f"❔ {app.get('status', '?')}")
    lines = [
        f"{app.get('title', app.get('id', '?'))} — {status}",
        f"Юнит: <code>{app.get('unit', '?')}</code>",
    ]
    urls = app.get("urls") or []
    if len(urls) == 1:
        lines.append(f"Веб-морда: {urls[0]}")
    elif urls:
        lines.append("Веб-морда:")
        lines.extend(urls)
    return "\n".join(lines)


async def run_app_skill(
    apps_link: ServiceLink,
    subscription: Subscription | None,
    action_id: str,
    value: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    action = await actions.find_action(apps_link, action_id)
    if action is None:
        text = (
            actions.unavailable_text(apps_link)
            if not apps_link.connected
            else "Умение недоступно."
        )
        return text, None
    try:
        result = await apps_link.command(action.id, actions.build_args(action, value))
    except ServiceUnavailableError:
        return actions.unavailable_text(apps_link), None
    except ProtoError as exc:
        if exc.code == ERR_NEEDS_PRIVILEGE:
            return f"⚠️ {exc.message}", None
        return f"⚠️ Ошибка: {exc.message}", None

    app_id = result.get("id") or value or action_id
    apps_actions = await apps_link.actions()
    keyboard = actions.build_choice_keyboard(subscription, apps_actions, APPS_SERVICE, app_id)
    return render_app_card(result), keyboard
