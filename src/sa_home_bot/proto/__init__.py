"""Протокол v0 — общение служб ноды: JSON поверх unix-сокета.

Публичный API пакета: сообщения (`messages`), сервер (`server`), клиент
(`client`). Описание протокола — в PROTOCOL.md в корне репозитория.
"""

from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.messages import (
    PROTO_VERSION,
    ActionParam,
    ActionSpec,
    Address,
    Envelope,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import ProtoServer, ServiceHandler

__all__ = [
    "PROTO_VERSION",
    "ActionParam",
    "ActionSpec",
    "Address",
    "Envelope",
    "ProtoClient",
    "ProtoError",
    "ProtoServer",
    "ServiceDescription",
    "ServiceHandler",
    "ServiceInfo",
]
