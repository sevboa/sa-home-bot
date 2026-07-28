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
MSG_AUTH = "auth"  # только TCP: первое сообщение соединения, payload {"token"}
MSG_HELLO = "hello"
MSG_DESCRIBE = "describe"
MSG_GET_STATE = "get_state"
MSG_COMMAND = "command"
MSG_RESPONSE = "response"
MSG_EVENT = "event"

REQUEST_TYPES = frozenset({MSG_AUTH, MSG_HELLO, MSG_DESCRIBE, MSG_GET_STATE, MSG_COMMAND})

# --- Коды ошибок ---
ERR_BAD_REQUEST = "bad_request"
ERR_UNSUPPORTED_PROTO = "unsupported_proto"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_UNAUTHORIZED = "unauthorized"  # TCP без/до auth или неверный токен; соединение закрывается
ERR_UNKNOWN_DST = "unknown_dst"  # dst указывает на неизвестную ноду/службу
ERR_UNAVAILABLE = "unavailable"  # нода/служба известна, но сейчас нет соединения
ERR_INTERNAL = "internal"
ERR_NEEDS_PRIVILEGE = "needs_privilege"  # действию не хватает прав; см. `nodectl fix`

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
    """Конверт сообщения. Ответ несёт `id` исходного запроса.

    ``timeout_s`` — необязательный таймаут ожидания ответа (сек), который
    хочет вызывающий вместо дефолтного `ProtoClient.DEFAULT_TIMEOUT`. Едет
    вместе с конвертом через все хопы форварда без изменений (сервер
    пересылает тот же объект `Envelope` дальше, см. `node/peers.py`), поэтому
    один параметр на клиенте применяется на каждом хопе цепочки
    бот → своя нода → пир/локальная служба — без переделки протокола
    маршрутизации. Нужен, в частности, для `llm.chat`/`llm.ask` (генерация,
    в т.ч. с холодным стартом, дольше дефолтных 10с — см. LLM_INTEGRATION_PLAN.md §3).

    ``hops`` — сколько ретрансляций пережило СОБЫТИЕ (второй предохранитель
    от шторма, независимый от дедупа SeenEvents — см. ARCHITECTURE §11
    «топология событийной сети» и node/app.py::MAX_EVENT_HOPS). Инкрементится
    нодой при каждой ретрансляции; отсутствие поля в JSON = 0, поэтому ноды
    старых версий события с ним принимают и не ломаются (лишь не наращивают
    счётчик — защита деградирует до одного рубежа, как было до v0.43).
    """

    type: str
    id: str
    v: int = PROTO_VERSION
    src: Address | None = None
    dst: Address | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None  # только для response
    error: dict[str, Any] | None = None  # {"code", "message"} при ok=False
    timeout_s: float | None = None
    hops: int = 0

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
    timeout_s: float | None = None,
) -> Envelope:
    return Envelope(
        type=type_, id=new_id(), src=src, dst=dst, payload=payload or {}, timeout_s=timeout_s
    )


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
    if env.timeout_s is not None:
        raw["timeout_s"] = env.timeout_s
    if env.hops:
        raw["hops"] = env.hops
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
    timeout_s = raw.get("timeout_s")
    if timeout_s is not None and not isinstance(timeout_s, (int, float)):
        raise ProtoError(ERR_BAD_REQUEST, "timeout_s должен быть числом")
    hops = raw.get("hops", 0)
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 0:
        raise ProtoError(ERR_BAD_REQUEST, "hops должен быть неотрицательным целым")

    return Envelope(
        type=msg_type,
        id=msg_id,
        v=v,
        src=Address.from_dict(raw.get("src")),
        dst=Address.from_dict(raw.get("dst")),
        payload=payload,
        ok=ok,
        error=error,
        timeout_s=float(timeout_s) if timeout_s is not None else None,
        hops=hops,
    )


# --- Описание службы (hello / describe) ---


@dataclass(frozen=True)
class ServiceInfo:
    """Кто ты: ответ на hello."""

    node: str
    service: str
    version: str
    # Тип машины ноды (server|workstation|vps) — едет уже в hello, чтобы рой
    # знал, ждать ли ноду всегда и можно ли её будить, без отдельного запроса.
    # Пусто — нода старой версии: см. node/kind.py::traits_for (консервативно).
    node_kind: str = ""
    # Ethernet-реквизиты для Wake-on-LAN (mac/ip/broadcast) — по той же
    # причине, что и node_kind выше: чтобы сосед знал, КАК будить, ещё до
    # того, как цель уснула и её уже не спросить. Живая находка 2026-07-27:
    # раньше это ехало только в get_state() и оседало в БД спрашивавшего —
    # у службы без своей истории опросов (tasks) кэш оставался пустым
    # навсегда, и разбудить она не могла никогда (см. wake_core.
    # resolve_wake_info). None — нода старой версии либо без Ethernet.
    wake: dict[str, str] | None = None
    # Все адреса, по которым к этой ноде можно прийти (этап 24), в порядке
    # предпочтения — сосед по ним ходит, а не по одному захардкоженному в его
    # конфиге. Без этого домашний рой не собирался без интернета: единственным
    # известным адресом был tailscale-адрес, а он появляется через 40-60 с
    # после загрузки и только при живой сети. Пусто — нода старой версии либо
    # без TCP-слушателя (только unix-сокет): тогда работает адрес из конфига.
    endpoints: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "service": self.service,
            "version": self.version,
            "node_kind": self.node_kind,
            "wake": self.wake,
            "endpoints": list(self.endpoints),
            "proto": PROTO_VERSION,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ServiceInfo:
        wake = payload.get("wake")
        raw_endpoints = payload.get("endpoints")
        try:
            return cls(
                node=str(payload["node"]),
                service=str(payload["service"]),
                version=str(payload["version"]),
                node_kind=str(payload.get("node_kind", "")),
                wake={str(k): str(v) for k, v in wake.items()} if isinstance(wake, dict) else None,
                endpoints=(
                    tuple(str(e) for e in raw_endpoints if e)
                    if isinstance(raw_endpoints, list)
                    else ()
                ),
            )
        except KeyError as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"hello без поля {exc}") from exc


@dataclass(frozen=True)
class ActionParam:
    """Параметр действия: имя, тип, обязательность.

    ``choices`` — допустимые значения (если конечны): фронтенд строит по ним
    UI (кнопка на значение), ничего не зная о семантике параметра.
    """

    name: str
    type: str = "string"  # string | int | float | bool
    required: bool = True
    title: str | None = None  # человекочитаемое имя для UI
    choices: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required}
        if self.title is not None:
            raw["title"] = self.title
        if self.choices is not None:
            raw["choices"] = list(self.choices)
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ActionParam:
        choices = raw.get("choices")
        return cls(
            name=str(raw["name"]),
            type=str(raw.get("type", "string")),
            required=bool(raw.get("required", True)),
            title=raw.get("title"),
            choices=tuple(str(c) for c in choices) if choices is not None else None,
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
