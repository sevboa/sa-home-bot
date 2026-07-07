"""Сериализация и версионирование сообщений протокола v0."""

import json

import pytest

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_UNSUPPORTED_PROTO,
    MSG_GET_STATE,
    PROTO_VERSION,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    decode,
    encode,
    make_error_response,
    make_event,
    make_request,
    make_response,
)


def test_request_roundtrip():
    env = make_request(
        MSG_GET_STATE,
        src=Address(service="telegram-bot"),
        dst=Address(node="alfred", service="monitor"),
    )
    decoded = decode(encode(env))
    assert decoded == env
    assert decoded.v == PROTO_VERSION
    assert decoded.dst.node == "alfred"


def test_response_roundtrip_ok_and_error():
    request = make_request(MSG_GET_STATE)
    ok = decode(encode(make_response(request, {"cpu": 42.0})))
    assert ok.id == request.id
    assert ok.ok is True
    assert ok.payload == {"cpu": 42.0}

    err = decode(encode(make_error_response(request.id, "unknown_action", "нет")))
    assert err.ok is False
    assert err.error_code() == "unknown_action"
    assert err.error_message() == "нет"


def test_event_roundtrip():
    env = make_event("overheat_started", {"component_id": "cpu:package"})
    decoded = decode(encode(env))
    assert decoded.payload["event"] == "overheat_started"
    assert decoded.payload["data"] == {"component_id": "cpu:package"}


def test_encode_is_single_ndjson_line():
    line = encode(make_request(MSG_GET_STATE))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    json.loads(line)  # валидный JSON


def test_decode_rejects_foreign_version():
    raw = json.loads(encode(make_request(MSG_GET_STATE)))
    raw["v"] = PROTO_VERSION + 1
    with pytest.raises(ProtoError) as exc_info:
        decode(json.dumps(raw).encode())
    assert exc_info.value.code == ERR_UNSUPPORTED_PROTO


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b"[1,2,3]",
        b'{"id": "x", "type": "get_state"}',  # нет версии
        b'{"v": 0, "type": "get_state"}',  # нет id
        b'{"v": 0, "id": "x"}',  # нет типа
        b'{"v": 0, "id": "x", "type": "get_state", "payload": []}',  # payload не объект
    ],
)
def test_decode_rejects_garbage(line):
    with pytest.raises(ProtoError) as exc_info:
        decode(line)
    assert exc_info.value.code == ERR_BAD_REQUEST


def test_service_description_roundtrip():
    desc = ServiceDescription(
        info=ServiceInfo(node="alfred", service="monitor", version="0.7.0"),
        capabilities=("temperature", "smart"),
        actions=(
            ActionSpec(
                id="scan_now",
                title="Запустить скан",
                params=(ActionParam(name="force", type="bool", required=False),),
            ),
            ActionSpec(
                id="restart",
                title="Перезапустить",
                params=(
                    ActionParam(name="name", choices=("monitor", "telegram-bot")),
                ),
            ),
        ),
    )
    restored = ServiceDescription.from_payload(desc.to_payload())
    assert restored == desc
    assert restored.find_action("scan_now").params[0].name == "force"
    assert restored.find_action("scan_now").params[0].choices is None
    assert restored.find_action("restart").params[0].choices == ("monitor", "telegram-bot")
    assert restored.find_action("nope") is None


def test_hello_payload_requires_fields():
    with pytest.raises(ProtoError):
        ServiceInfo.from_payload({"node": "alfred"})
