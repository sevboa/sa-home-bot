from types import SimpleNamespace

import pytest

from sa_home_bot.bot.handlers import torrents as torrents_handler
from sa_home_bot.proto.messages import ActionParam, ActionSpec

SAVE_DIRS = ("/mnt/data/torrents/complete", "/mnt/scratch/pr")


def _message_with_document(file_name: str | None, mime_type: str | None = None):
    has_doc = file_name or mime_type
    doc = SimpleNamespace(file_name=file_name, mime_type=mime_type) if has_doc else None
    return SimpleNamespace(document=doc)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message_with_document("ubuntu.torrent"), True),
        (_message_with_document("ubuntu.bin", mime_type="application/x-bittorrent"), True),
        (_message_with_document("photo.jpg"), False),
        (SimpleNamespace(document=None), False),
    ],
)
def test_is_torrent_document(message, expected):
    assert torrents_handler._is_torrent_document(message) is expected


@pytest.mark.parametrize(
    ("magnet", "expected"),
    [
        ("magnet:?xt=urn:btih:abc&dn=Ubuntu%2022.04&tr=udp://tracker", "Ubuntu 22.04"),
        ("magnet:?xt=urn:btih:abc", "magnet-ссылка"),
    ],
)
def test_magnet_name(magnet, expected):
    assert torrents_handler._magnet_name(magnet) == expected


class FakeLink:
    def __init__(self, actions):
        self._actions = actions

    async def actions(self):
        return self._actions


async def test_save_path_choices_reads_add_action_param():
    action = ActionSpec(
        id="add",
        title="Добавить",
        params=(
            ActionParam(name="source"),
            ActionParam(name="save_path", choices=SAVE_DIRS),
        ),
    )
    choices = await torrents_handler._save_path_choices(FakeLink([action]))
    assert choices == list(SAVE_DIRS)


async def test_save_path_choices_empty_when_action_missing():
    choices = await torrents_handler._save_path_choices(FakeLink([]))
    assert choices == []


def test_dir_keyboard_strips_mnt_prefix_and_encodes_index():
    keyboard = torrents_handler._dir_keyboard("tok1234", list(SAVE_DIRS))
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["data/torrents/complete", "scratch/pr"]
    assert [b.callback_data for b in buttons] == ["tor:tok1234:0", "tor:tok1234:1"]
