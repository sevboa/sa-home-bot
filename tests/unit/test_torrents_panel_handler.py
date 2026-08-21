"""Обработчики панели /torrents (bot/handlers/torrents_panel.py): список →
карточка раздачи, пауза/старт, лимит скорости — всё редрайвом одного
сообщения (edit_text), по образцу test_swarm_panel_handler.py."""

from sa_home_bot.bot import commands
from sa_home_bot.bot.handlers.torrents_panel import cmd_torrents, on_torrents_screen
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.torrents_view import UNAVAILABLE_TEXT
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.edit_keyboards: list = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.edit_keyboards.append(reply_markup)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.id = "cb-1"
        self.message = FakeMessage()
        self.answered: list = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


def _torrent(**overrides):
    base = {
        "name": "Foo.S01",
        "state": "downloading",
        "progress_pct": 42,
        "dlspeed_bytes_s": 1048576,
        "paused": False,
        "eta_s": 3600,
        "seeds": 5,
        "peers": 2,
        "hash": "abc123",
    }
    base.update(overrides)
    return base


class FakeTorrentsLink:
    display_name = "закачки"
    connected = True

    def __init__(self, torrents=(), speed_limit_mbps=0, unavailable=False, fail_with=None):
        self._torrents = list(torrents)
        self._speed_limit_mbps = speed_limit_mbps
        self._unavailable = unavailable
        self._fail_with = fail_with
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append((action, args or {}))
        if self._unavailable:
            raise ServiceUnavailableError("нет связи")
        # fail_with бьёт по самому действию (pause/resume/speed_limit), а не
        # по вспомогательному list, которым хендлер сначала находит раздачу —
        # иначе до проверяемого вызова дело бы не доходило вовсе.
        if self._fail_with is not None and action != "list":
            raise self._fail_with
        if action == "list":
            torrents = self._torrents
            if not (args or {}).get("with_hash"):
                torrents = [{k: v for k, v in t.items() if k != "hash"} for t in torrents]
            return {
                "torrents": torrents,
                "count": len(torrents),
                "speed_limit_mbps": self._speed_limit_mbps,
            }
        if action == "pause":
            name = args["name"]
            for t in self._torrents:
                if t.get("hash") == name:
                    t["paused"] = True
            return {"paused": [name], "count": 1}
        if action == "resume":
            name = args["name"]
            for t in self._torrents:
                if t.get("hash") == name:
                    t["paused"] = False
            return {"resumed": [name], "count": 1}
        if action == "speed_limit":
            self._speed_limit_mbps = args["mbps"]
            return {"speed_limit_mbps": args["mbps"]}
        raise AssertionError(f"unexpected action: {action}")


def _codes(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


# --- /torrents ---------------------------------------------------------


async def test_cmd_torrents_renders_list_in_one_message():
    message = FakeMessage()
    link = FakeTorrentsLink(torrents=[_torrent()])
    await cmd_torrents(message, torrents_link=link)
    assert len(message.answers) == 1
    assert "Закачки" in message.answers[0]
    assert link.commands == [("list", {"with_hash": True})]


async def test_cmd_torrents_reports_unavailable_service():
    message = FakeMessage()
    await cmd_torrents(message, torrents_link=FakeTorrentsLink(unavailable=True))
    assert message.answers == [UNAVAILABLE_TEXT]


# --- t_list --------------------------------------------------------------


async def test_t_list_redraws_in_place():
    callback = FakeCallback(f"st:{commands.TORRENTS_LIST_CODE}:0")
    link = FakeTorrentsLink(torrents=[_torrent()])
    await on_torrents_screen(callback, torrents_link=link)
    assert len(callback.message.edits) == 1
    assert callback.message.answers == []


# --- t_card ----------------------------------------------------------------


async def test_t_card_shows_torrent_details():
    callback = FakeCallback(f"st:{commands.TORRENT_CARD_CODE}:abc123")
    link = FakeTorrentsLink(torrents=[_torrent(hash="abc123", seeds=9, peers=4)])
    await on_torrents_screen(callback, torrents_link=link)
    assert "Foo.S01" in callback.message.edits[0]
    assert "9 сидов, 4 личей" in callback.message.edits[0]


async def test_t_card_missing_hash_shows_alert():
    callback = FakeCallback(f"st:{commands.TORRENT_CARD_CODE}:gone")
    link = FakeTorrentsLink(torrents=[_torrent(hash="abc123")])
    await on_torrents_screen(callback, torrents_link=link)
    assert callback.message.edits == []
    assert callback.answered[0][1].get("show_alert") is True


# --- t_toggle ----------------------------------------------------------------


async def test_t_toggle_pauses_running_torrent_and_redraws_card():
    callback = FakeCallback(f"st:{commands.TORRENT_TOGGLE_CODE}:abc123")
    link = FakeTorrentsLink(torrents=[_torrent(hash="abc123", paused=False)])
    await on_torrents_screen(callback, torrents_link=link)
    assert ("pause", {"name": "abc123"}) in link.commands
    texts = [b.text for row in callback.message.edit_keyboards[-1].inline_keyboard for b in row]
    assert any("Запустить" in t for t in texts)


async def test_t_toggle_resumes_paused_torrent():
    callback = FakeCallback(f"st:{commands.TORRENT_TOGGLE_CODE}:abc123")
    link = FakeTorrentsLink(torrents=[_torrent(hash="abc123", paused=True)])
    await on_torrents_screen(callback, torrents_link=link)
    assert ("resume", {"name": "abc123"}) in link.commands
    texts = [b.text for row in callback.message.edit_keyboards[-1].inline_keyboard for b in row]
    assert any("Остановить" in t for t in texts)


async def test_t_toggle_reports_proto_error():
    callback = FakeCallback(f"st:{commands.TORRENT_TOGGLE_CODE}:abc123")
    link = FakeTorrentsLink(
        torrents=[_torrent(hash="abc123")], fail_with=ProtoError(ERR_BAD_REQUEST, "нет такой")
    )
    await on_torrents_screen(callback, torrents_link=link)
    assert callback.answered[0][1].get("show_alert") is True


# --- t_speed -----------------------------------------------------------------


async def test_t_speed_applies_limit_and_keeps_offset():
    from sa_home_bot.bot.torrents_view import TORRENTS_PAGE_SIZE

    many = [_torrent(name=f"T{i}", hash=f"h{i}") for i in range(TORRENTS_PAGE_SIZE + 2)]
    callback = FakeCallback(f"st:{commands.TORRENT_SPEED_CODE}:2:8")
    link = FakeTorrentsLink(torrents=many, speed_limit_mbps=0)
    await on_torrents_screen(callback, torrents_link=link)
    assert ("speed_limit", {"mbps": 2}) in link.commands
    assert "2 МБ/с" in callback.message.edits[-1]
    codes = _codes(callback.message.edit_keyboards[-1])
    assert "st:t_speed:5:8" in codes  # пресеты сохранили offset страницы
