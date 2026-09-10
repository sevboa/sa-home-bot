"""Юнит-тесты обёртки над ``xray api`` (транспорт reality службы vpn).

``RealXrayBackend`` — единственное место, где reality-транспорт зовёт внешний
процесс. Живого xray в CI нет, поэтому подменяем ``_run`` и проверяем форму
сниппета ``adu`` и разбор ответов ``statsquery``/``inbounduser``.

Регрессия 2026-09-10 (v0.102.1): сниппет ``adu`` без ``port`` и
``decryption:"none"`` xray 26.3.x отвергает («failed to build config»), но
``adu`` при этом выходит с кодом 0 — служба выдавала гостю нерабочий конфиг.
"""

from __future__ import annotations

import json

import pytest

from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.reality.xray import RealXrayBackend, _parse_clients, _parse_stats


class _Backend(RealXrayBackend):
    """RealXrayBackend с перехваченным ``_run``: копит вызовы, отдаёт скрипт."""

    def __init__(self, replies: list[str] | None = None) -> None:
        super().__init__("127.0.0.1:10085", "reality-in", 8443)
        self.calls: list[tuple[str, ...]] = []
        self.written: list[dict] = []
        self._replies = list(replies or [])

    async def _run(self, *args: str, stdin: bytes | None = None) -> str:
        self.calls.append(args)
        # последний аргумент adu — путь к temp-файлу сниппета; читаем его до
        # того, как add_client его удалит (finally отрабатывает после _run).
        if args and args[0] == "adu":
            with open(args[-1], encoding="utf-8") as fh:
                self.written.append(json.load(fh))
        return self._replies.pop(0) if self._replies else ""


@pytest.mark.asyncio
async def test_add_client_snippet_has_port_and_decryption() -> None:
    be = _Backend(["add user: c1-phone\nresult: ok\nAdded 1 user(s) in total."])
    await be.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")

    inbound = be.written[0]["inbounds"][0]
    assert inbound["tag"] == "reality-in"
    assert inbound["port"] == 8443
    assert inbound["settings"]["decryption"] == "none"
    assert inbound["settings"]["clients"] == [
        {"id": "uuid-1", "email": "c1-phone", "flow": "xtls-rprx-vision"}
    ]
    assert be.calls[0][0] == "adu"


@pytest.mark.asyncio
async def test_add_client_raises_on_silent_zero_added() -> None:
    # adu вышел с кодом 0, но ничего не добавил — прежде это молча «удавалось».
    be = _Backend(
        [
            "processing inbound: reality-in\n"
            'failed to build config: infra/conf: VLESS settings: please add/set '
            '"decryption":"none"\nAdded 0 user(s) in total.'
        ]
    )
    with pytest.raises(ProtoError):
        await be.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")


@pytest.mark.asyncio
async def test_remove_client_swallows_not_found() -> None:
    class _NF(_Backend):
        async def _run(self, *args: str, stdin: bytes | None = None) -> str:
            raise ProtoError("INTERNAL", "xray api rmu завершился ошибкой: User not found.")

    await _NF().remove_client("c1-gone")  # не бросает


@pytest.mark.asyncio
async def test_list_clients_parses_tag_query() -> None:
    payload = json.dumps(
        {
            "users": [
                {"email": "c1-phone", "account": {"id": "u1", "flow": "xtls-rprx-vision"}},
                {"email": "c2-laptop", "account": {"id": "u2"}},
            ]
        }
    )
    be = _Backend([payload])
    assert await be.list_clients() == {"c1-phone", "c2-laptop"}


def test_parse_clients_empty_and_placeholder() -> None:
    assert _parse_clients("{}") == set()
    assert _parse_clients('{"users": [{}]}') == set()


def test_parse_stats_pairs_up_and_down() -> None:
    payload = json.dumps(
        {
            "stat": [
                {"name": "user>>>c1-phone>>>traffic>>>uplink", "value": 10},
                {"name": "user>>>c1-phone>>>traffic>>>downlink", "value": 200},
                {"name": "user>>>c2-laptop>>>traffic>>>uplink", "value": 5},
                {"name": "inbound>>>reality-in>>>traffic>>>uplink", "value": 999},
            ]
        }
    )
    assert _parse_stats(payload) == {"c1-phone": (10, 200), "c2-laptop": (5, 0)}
