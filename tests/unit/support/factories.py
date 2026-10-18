from __future__ import annotations

from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

OWNER_ALLOWED = ("*",)


def make_subscription(
    chat_id: int = 1, name: str = "me", *allowed: str, **options
) -> Subscription:
    return Subscription(
        chat_id=chat_id,
        name=name,
        allowed_commands=frozenset(allowed or OWNER_ALLOWED),
        **options,
    )


def make_book(*subscriptions: Subscription) -> SubscriptionBook:
    return SubscriptionBook(list(subscriptions))


def png_bytes(size: int = 8, color: str = "brown") -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()
