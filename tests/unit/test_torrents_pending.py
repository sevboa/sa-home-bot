"""PendingTorrents: эфемерное хранилище выбора директории между сообщением
и нажатием кнопки, с вытеснением самого старого при переполнении."""

from sa_home_bot.bot.torrent_pending import PendingTorrent, PendingTorrents


def test_add_then_pop_returns_item_once():
    pending = PendingTorrents()
    token = pending.add(PendingTorrent(source="magnet:?xt=urn:btih:abc", name="Foo"))
    item = pending.pop(token)
    assert item is not None
    assert item.source == "magnet:?xt=urn:btih:abc"
    assert item.name == "Foo"
    assert pending.pop(token) is None  # повторный pop — уже забрано


def test_pop_unknown_token_returns_none():
    assert PendingTorrents().pop("nope") is None


def test_overflow_evicts_oldest():
    pending = PendingTorrents(maxsize=2)
    t1 = pending.add(PendingTorrent(source="a", name="a"))
    pending.add(PendingTorrent(source="b", name="b"))
    pending.add(PendingTorrent(source="c", name="c"))  # переполнение — вытесняет t1
    assert pending.pop(t1) is None
