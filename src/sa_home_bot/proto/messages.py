"""Протокол v0: конверт, сообщения и (де)сериализация.

Кадрирование — одна строка UTF-8 JSON на сообщение (NDJSON). Конверт несёт
версию протокола и адресата (`dst`): маршрутизация к удалённым нодам позже
ляжет в тот же формат, фронтенду достаточно одного подключения к своей ноде.
Полное описание — в PROTOCOL.md.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 0

# --- Типы сообщений ---
MSG_HELLO = "hello"
MSG_DESCRIBE = "describe"
MSG_GET_STATE = "get_state"
MSG_COMMAND = "command"
MSG_RESPONSE = "response"
MSG_EVENT = "event"

REQUEST_TYPES = frozenset({MSG_HELLO, MSG_DESCRIBE, MSG_GET_STATE, MSG_COMMAND})

# --- Коды ошибок ---
ERR_BAD_REQUEST = "bad_request"
ERR_UNSUPPORTED_PROTO = "unsupported_proto"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_INTERNAL = "internal"

# Максимальная длина одного сообщения на проводе (защита от мусора в сокете).
MAX_MESSAGE_BYTES = 1 * 1024 * 1024


class ProtoError(Exception):
    """Ошибка протокола: невалидное сообщение или отрицательный ответ."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Address:
    """Адрес в конверте: нода + служба. node=None — локальная нода."""

    node: str | None = None
    service: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "service": self.service}

    @classmethod
    def from_dict(cls, raw: Any) -> Address | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ProtoError(ERR_BAD_REQUEST, "адрес должен быть объектом")
        node = raw.get("node")
        service = raw.get("service")
        if node is not None and not isinstance(node, str):
            raise ProtoError(ERR_BAD_REQUEST, "node должен быть строкой")
        if service is not None and not isinstance(service, str):
            raise ProtoError(ERR_BAD_REQUEST, "service должен быть строкой")
        return cls(node=node, service=service)


@dataclass(frozen=True)
class Envelope:
    """Конверт сообщения. Ответ несёт `id` исходного запроса."""

    type: str
    id: str
    v: int = PROTO_VERSION
    src: Address | None = None
    dst: Address | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None  # только для response
    error: dict[str, Any] | None = None  # {"code", "message"} при ok=False

    def error_code(self) -> str | None:
        return (self.error or {}).get("code")

    def error_message(self) -> str:
        return (self.error or {}).get("message", "")


def new_id() -> str:
    return uuid.uuid4().hex


def make_request(
    type_: str,
    payload: dict[str, Any] | None = None,
    *,
    src: Address | None = None,
    dst: Address | None = None,
) -> Envelope:
    return Envelope(type=type_, id=new_id(), src=src, dst=dst, payload=payload or {})


def make_response(request: Envelope, payload: dict[str, Any] | None = None) -> Envelope:
    return Envelope(type=MSG_RESPONSE, id=request.id, ok=True, payload=payload or {})


def make_error_response(request_id: str, code: str, message: str) -> Envelope:
    return Envelope(
        type=MSG_RESPONSE,
        id=request_id,
        ok=False,
        error={"code": code, "message": message},
    )


def make_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    src: Address | None = None,
) -> Envelope:
    return Envelope(
        type=MSG_EVENT,
        id=new_id(),
        src=src,
        payload={"event": event_type, "data": data or {}},
    )


def encode(env: Envelope) -> bytes:
    """Конверт → одна NDJSON-строка (с завершающим \\n)."""
    raw: dict[str, Any] = {"v": env.v, "id": env.id, "type": env.type}
    if env.src is not None:
        raw["src"] = env.src.to_dict()
    if env.dst is not None:
        raw["dst"] = env.dst.to_dict()
    if env.ok is not None:
        raw["ok"] = env.ok
    if env.error is not None:
        raw["error"] = env.error
    if env.payload:
        raw["payload"] = env.payload
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> Envelope:
    """NDJSON-строка → конверт. Бросает ProtoError на мусор/чужую версию."""
    try:
        raw = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtoError(ERR_BAD_REQUEST, f"невалидный JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtoError(ERR_BAD_REQUEST, "сообщение должно быть объектом")

    v = raw.get("v")
    if not isinstance(v, int):
        raise ProtoError(ERR_BAD_REQUEST, "нет версии протокола (v)")
    if v != PROTO_VERSION:
        raise ProtoError(
            ERR_UNSUPPORTED_PROTO, f"версия {v} не поддерживается (наша {PROTO_VERSION})"
        )

    msg_id = raw.get("id")
    msg_type = raw.get("type")
    if not isinstance(msg_id, str) or not msg_id:
        raise ProtoError(ERR_BAD_REQUEST, "нет id сообщения")
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtoError(ERR_BAD_REQUEST, "нет типа сообщения")

    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtoError(ERR_BAD_REQUEST, "payload должен быть объектом")
    error = raw.get("error")
    if error is not None and not isinstance(error, dict):
        raise ProtoError(ERR_BAD_REQUEST, "error должен быть объектом")
    ok = raw.get("ok")
    if ok is not None and not isinstance(ok, bool):
        raise ProtoError(ERR_BAD_REQUEST, "ok должен быть булевым")

    return Envelope(
        type=msg_type,
        id=msg_id,
        v=v,
        src=Address.from_dict(raw.get("src")),
        dst=Address.from_dict(raw.get("dst")),
        payload=payload,
        ok=ok,
        error=error,
    )


# --- Описание службы (hello / describe) ---


@dataclass(frozen=True)
class ServiceInfo:
    """Кто ты: ответ на hello."""

    node: str
    service: str
    version: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "service": self.service,
            "version": self.version,
            "proto": PROTO_VERSION,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ServiceInfo:
        try:
            return cls(
                node=str(payload["node"]),
                service=str(payload["service"]),
                version=str(payload["version"]),
            )
        except KeyError as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"hello без поля {exc}") from exc


@dataclass(frozen=True)
class ActionParam:
    """Параметр действия: имя, тип, обязательность."""

    name: str
    type: str = "string"  # string | int | float | bool
    required: bool = True
    title: str | None = None  # человекочитаемое имя для UI

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.title is not None:
            raw["title"] = self.title
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ActionParam:
        return cls(
            name=str(raw["name"]),
            type=str(raw.get("type", "string")),
            required=bool(raw.get("required", True)),
            title=raw.get("title"),
        )


@dataclass(frozen=True)
class ActionSpec:
    """Действие службы: id, название для UI, параметры.

    Фронтенды строят кнопки и проверяют права (`действие@нода`) по этому
    списку, ничего не хардкодя.
    """

    id: str
    title: str
    params: tuple[ActionParam, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "params": [p.to_dict() for p in self.params]}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ActionSpec:
        return cls(
            id=str(raw["id"]),
            title=str(raw.get("title", raw["id"])),
            params=tuple(ActionParam.from_dict(p) for p in raw.get("params", [])),
        )


@dataclass(frozen=True)
class ServiceDescription:
    """Ответ на describe: кто ты + capabilities + список действий."""

    info: ServiceInfo
    capabilities: tuple[str, ...] = ()
    actions: tuple[ActionSpec, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = self.info.to_payload()
        payload["capabilities"] = list(self.capabilities)
        payload["actions"] = [a.to_dict() for a in self.actions]
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ServiceDescription:
        try:
            return cls(
                info=ServiceInfo.from_payload(payload),
                capabilities=tuple(str(c) for c in payload.get("capabilities", [])),
                actions=tuple(ActionSpec.from_dict(a) for a in payload.get("actions", [])),
            )
        except (KeyError, TypeError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"невалидный describe: {exc}") from exc

    def find_action(self, action_id: str) -> ActionSpec | None:
        for action in self.actions:
            if action.id == action_id:
                return action
        return None
