"""57.5: перевыпуск по выбранным подключениям — маска в callback_data, выбрать все,
N=0, частичный отказ, изменение списка."""

from __future__ import annotations

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_settings as vs
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_expert import (
    KEY,
    Link,
    _all_buttons,
    _conn,
    _press,
    _servers,
    _texts,
)
from .test_vpn_expert import _fanout as _fanout  # noqa: F401  (autouse)


def _sig():
    device = vd.build_devices(_servers())[0]
    return vs.list_signature(vs.reissue_connections(device))


def _screen(mask, sig=None):
    return f"act:vpn:vpn_card:r{KEY}-{sig or _sig()}-{mask:x}"


def _run(mask, sig=None):
    return f"act:vpn:reissue:~m{KEY}-{sig or _sig()}-{mask:x}"


class VlessLink(Link):
    async def command(self, action, args=None, dst=None, *, timeout=None):
        if action == vpn_protocol.ACTION_GET_VLESS:
            self.calls.append((action, args or {}))
            self.dsts.append(dst)
            return {
                "config_text": "cfg",
                "share_url": f"vless://x@{dst.node}",
                "qr_png_b64": None,
                "location": dst.node,
            }
        return await super().command(action, args, dst)


def _reissues(link):
    return [
        (d.node, a["transport"])
        for (act, a), d in zip(link.calls, link.dsts, strict=True)
        if act == vpn_protocol.ACTION_REISSUE
    ]


async def test_toggle_marks_checkbox_and_shows_count_button():
    cb, _, _ = await _press(_screen(0b101))
    texts = _texts(cb.message.edit_markups[-1])
    assert texts[:3] == ["☑️ 🇳🇱 VLESS · Hiddify", "☐ 🇺🇸 VLESS · Hiddify", "☑️ 🇺🇸 AmneziaWG"]
    assert "🔄 Перевыпустить (2)" in texts
    assert "Выбрать все" in texts
    # нажатие на отмеченную снимает её (маска XOR)
    buttons = {b.text: b.callback_data for b in _all_buttons(cb.message.edit_markups[-1])}
    assert buttons["☑️ 🇳🇱 VLESS · Hiddify"].endswith(f"-{_sig()}-4")
    assert buttons["☐ 🇺🇸 VLESS · Hiddify"].endswith(f"-{_sig()}-7")


async def test_select_all_and_nothing_selected_hides_run_button():
    cb, _, _ = await _press(_screen(0))
    markup = cb.message.edit_markups[-1]
    texts = _texts(markup)
    assert not any("Перевыпустить (" in t for t in texts)
    all_btn = next(b for b in _all_buttons(markup) if b.text == "Выбрать все")
    assert all_btn.callback_data.endswith(f"-{_sig()}-7")
    cb, _, _ = await _press(_screen(7))
    texts = _texts(cb.message.edit_markups[-1])
    assert "Выбрать все" not in texts and "🔄 Перевыпустить (3)" in texts
    assert texts[-1] == "Отмена"


async def test_run_with_zero_mask_does_nothing():
    cb, link, _ = await _press(_run(0), link=VlessLink(None))
    assert not _reissues(link)
    assert cb.answered[0][1].get("show_alert")


async def test_run_reissues_selected_pairs_and_delivers_new_settings():
    cb, link, notifier = await _press(_run(0b101), link=VlessLink(None))
    assert sorted(_reissues(link)) == [("jeeves", "reality"), ("wooster", "awg")]
    # AmneziaWG — файл .conf + инструкция с «Обязательно переименуйте»
    assert notifier.sent_documents
    assert any("Обязательно переименуйте" in t for _, t in notifier.sent_direct)
    # VLESS — сообщение «VLESS · Hiddify» с пометкой про автообновление
    vless = [t for _, t in notifier.sent_direct if "VLESS · Hiddify" in t]
    assert vless and "Hiddify обновит подключение сам" in vless[0]
    final = cb.message.edits[-1]
    assert "✅ Перевыпущено: 🇳🇱 VLESS · Hiddify, 🇺🇸 AmneziaWG." in final
    assert "Не удалось" not in final


async def test_partial_failure_reports_done_and_failed():
    class Flaky(VlessLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == vpn_protocol.ACTION_REISSUE and dst.node == "wooster":
                self.calls.append((action, args or {}))
                self.dsts.append(dst)
                raise ProtoError("bad_request", "нода занята")
            return await super().command(action, args, dst)

    cb, _, notifier = await _press(_run(0b111), link=Flaky(None))
    final = cb.message.edits[-1]
    assert "✅ Перевыпущено: 🇳🇱 VLESS · Hiddify." in final
    assert "🇺🇸 VLESS · Hiddify — нода занята" in final
    assert "🇺🇸 AmneziaWG — нода занята" in final
    assert not notifier.sent_documents  # AmneziaWG не выпущен — файла нет


async def test_list_changed_between_presses_resets_selection(_fanout):
    stale = _sig()
    # у США пропал AmneziaWG — список изменился
    _fanout["servers"] = _servers()
    _fanout["servers"][1]["device_usage"][0]["connections"] = [_conn("reality")]
    cb, _, _ = await _press(_screen(0b101, stale))
    assert cb.message.edits[-1].startswith(vs.LIST_CHANGED_TEXT)
    texts = _texts(cb.message.edit_markups[-1])
    assert texts[:2] == ["☐ 🇳🇱 VLESS · Hiddify", "☐ 🇺🇸 VLESS · Hiddify"]
    # запуск по устаревшей маске ничего не перевыпускает
    cb, link, _ = await _press(_run(0b101, stale), link=VlessLink(None))
    assert not _reissues(link)
    assert cb.message.edits[-1].startswith(vs.LIST_CHANGED_TEXT)


async def test_run_in_group_chat_refused():
    cb, link, _ = await _press(_run(1), link=VlessLink(None), chat_id=-100)
    assert not _reissues(link)


def test_callbacks_fit_64_bytes_for_every_mask():
    device = vd.build_devices(_servers())[0]
    conns = vs.reissue_connections(device)
    countries = vd.countries_of(_servers())
    for mask in range(vs.all_mask(conns) + 1):
        markup = vs.reissue_select_keyboard(device, conns, countries, mask)
        for button in _all_buttons(markup):
            assert len(button.callback_data.encode()) <= 64, button.callback_data
    assert len(vs.reissue_run_cb(device.key, conns, 7).encode()) <= 64
    # даже при 16 битах (потолок маски)
    big = [vd.Connection("n", "awg", "active", None, None, False, 0)] * vs.MAX_REISSUE_BITS
    assert len(vs.reissue_run_cb(device.key, big, 0xFFFF).encode()) <= 64


def test_parse_selection():
    assert vs.parse_selection("abc") == ("abc", None, 0)
    assert vs.parse_selection("abc-1f2e-5") == ("abc", "1f2e", 5)
    assert vs.parse_selection("abc-1f2e-zz") == ("abc", "1f2e", 0)
