from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 0

MSG_AUTH = "auth"
MSG_HELLO = "hello"
MSG_DESCRIBE = "describe"
MSG_GET_STATE = "get_state"
MSG_COMMAND = "command"
MSG_RESPONSE = "response"
MSG_EVENT = "event"

REQUEST_TYPES = frozenset({MSG_AUTH, MSG_HELLO, MSG_DESCRIBE, MSG_GET_STATE, MSG_COMMAND})

ERR_BAD_REQUEST = "bad_request"
ERR_UNSUPPORTED_PROTO = "unsupported_proto"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_UNAUTHORIZED = "unauthorized"
ERR_UNKNOWN_DST = "unknown_dst"
ERR_UNAVAILABLE = "unavailable"
ERR_TIMEOUT = "timeout"
ERR_INTERNAL = "internal"
ERR_NEEDS_PRIVILEGE = "needs_privilege"

MAX_MESSAGE_BYTES = 1 * 1024 * 1024


class ProtoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Address:
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
    type: str
    id: str
    v: int = PROTO_VERSION
    src: Address | None = None
    dst: Address | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    error: dict[str, Any] | None = None
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


@dataclass(frozen=True)
class ServiceInfo:
    node: str
    service: str
    version: str
    node_kind: str = ""
    wake: dict[str, str] | None = None
    endpoints: tuple[str, ...] = ()
    links: dict[str, dict[str, Any]] | None = None
    busy: bool = False

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "node": self.node,
            "service": self.service,
            "version": self.version,
            "node_kind": self.node_kind,
            "wake": self.wake,
            "endpoints": list(self.endpoints),
            "proto": PROTO_VERSION,
        }
        if self.links is not None:
            payload["links"] = self.links
        if self.busy:
            payload["busy"] = True
        return payload

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
                links=_parse_links(payload.get("links")),
                busy=payload.get("busy") is True,
            )
        except KeyError as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"hello без поля {exc}") from exc


def _parse_links(raw: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    links: dict[str, dict[str, Any]] = {}
    for peer, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        rtt = entry.get("rtt_ms")
        loss = entry.get("loss")
        links[str(peer)] = {
            "rtt_ms": float(rtt) if _is_number(rtt) else None,
            "loss": float(loss) if _is_number(loss) else 0.0,
            "alive": entry.get("alive") is True,
        }
    return links


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class ActionParam:
    name: str
    type: str = "string"
    required: bool = True
    title: str | None = None
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
    info: ServiceInfo
    capabilities: tuple[str, ...] = ()
    actions: tuple[ActionSpec, ...] = ()
    model_profile: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = self.info.to_payload()
        payload["capabilities"] = list(self.capabilities)
        payload["actions"] = [a.to_dict() for a in self.actions]
        if self.model_profile is not None:
            payload["model_profile"] = self.model_profile
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ServiceDescription:
        try:
            profile = payload.get("model_profile")
            return cls(
                info=ServiceInfo.from_payload(payload),
                capabilities=tuple(str(c) for c in payload.get("capabilities", [])),
                actions=tuple(ActionSpec.from_dict(a) for a in payload.get("actions", [])),
                model_profile=dict(profile) if isinstance(profile, dict) else None,
            )
        except (KeyError, TypeError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"невалидный describe: {exc}") from exc

    def find_action(self, action_id: str) -> ActionSpec | None:
        for action in self.actions:
            if action.id == action_id:
                return action
        return None
