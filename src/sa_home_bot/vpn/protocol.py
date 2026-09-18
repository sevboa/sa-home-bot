"""Константы протокола службы vpn — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как memory/protocol.py и
tasks/protocol.py.

Живут отдельно от vpn/service.py, чтобы bot/tools.py и bot/handlers/vpn.py
могли их импортировать, не утягивая саму службу (и её зависимости — работу
с awg-бинарником) в бота.

Служба выдаёт и учитывает доступ к AmneziaWG на ноде роя с белым IP — см.
«Этап 33» в IMPLEMENTATION_PLAN.md для дизайна в целом.

Этап 39: серверов может быть несколько (jeeves + wooster), поэтому адреса
ноды-держателя тут нет вовсе — все потоки ищут живую через
``bot/vpn_nodes.py`` (``resolve_vpn_dst``/``fanout``).
"""

from __future__ import annotations

import re as _re
import time as _time

SERVICE_NAME = "vpn"

# --- Транспорты (подэтап 39.0.x) ---
# У пира vpn один из двух транспортов; учёт трафика и месячная квота — ОБЩИЕ
# (одна квота на гостя на сервер, независимо от транспорта — решение
# пользователя 2026-09-10). Значение хранится в vpn_peers.transport.
# Reality-пир переиспользует колонки: public_key = UUID клиента xray,
# address = его email ("c<chat_id>-<устройство>").
TRANSPORT_AWG = "awg"  # AmneziaWG (UDP + обфускация) — исходный транспорт
TRANSPORT_REALITY = "reality"  # VLESS+Reality через xray-core (TCP/443) — для РФ
TRANSPORTS = (TRANSPORT_AWG, TRANSPORT_REALITY)

# Трёхбуквенные коды для имён выдаваемых файлов (гость держит конфиги с
# нескольких серверов сразу и по имени должен понимать, что откуда).
TRANSPORT_FILE_CODE = {TRANSPORT_AWG: "awg", TRANSPORT_REALITY: "vls"}

# --- Страна сервера ---
# Флаг-эмодзи состоит из двух regional indicator symbols, которые однозначно
# мапятся на буквы: 🇳🇱 = U+1F1F3 U+1F1F1 = "NL". Поэтому отдельного поля с
# кодом страны в конфиге ноды не нужно — хватает [vpn].location.
_FLAG_FIRST = 0x1F1E6
_FLAG_LAST = 0x1F1FF


def country_code(location: str) -> str:
    """«🇳🇱 Нидерланды» → «NL». Пусто, если флага в строке нет."""
    letters = [
        chr(ord(c) - _FLAG_FIRST + ord("A"))
        for c in location
        if _FLAG_FIRST <= ord(c) <= _FLAG_LAST
    ]
    return "".join(letters[:2])


def country_flag(location: str) -> str:
    """«🇳🇱 Нидерланды» → «🇳🇱». Пусто, если флага нет."""
    flag = [c for c in location if _FLAG_FIRST <= ord(c) <= _FLAG_LAST]
    return "".join(flag[:2])


# --- Имена выдаваемых гостю файлов ---
# Живут здесь, а не в bot/handlers/vpn.py: те же имена нужны tool_vpn
# (bot/tools.py), а он не может импортировать обработчик — тот тянет aiogram,
# а tools.py крутится ещё и в службе tasks.
_UNSAFE_FILENAME = _re.compile(r"[^a-z0-9]+")
_MAX_TUNNEL_NAME = 15  # NAME_PATTERN wireguard-android: [a-zA-Z0-9_=+.-]{1,15}


def _name_prefix(transport: str, location: str) -> str:
    """«awg_nl_» — тип настроек и страна сервера в начале имени файла: у гостя
    конфиги с нескольких серверов сразу, и по имени должно быть видно, что
    откуда (решение пользователя 2026-09-18). Без флага в ``[vpn].location``
    остаётся только тип."""
    code = TRANSPORT_FILE_CODE.get(transport, transport[:3])
    country = country_code(location).lower()
    return f"{code}_{country}_" if country else f"{code}_"


def secret_filename(transport: str, device_label: str, location: str = "") -> str:
    """Имя файла конфига для гостя: «awg_nl_rose_123.conf» / «vls_nl_orchid.json».

    У awg имя файла без расширения становится именем тоннеля в приложении и
    обязано влезть в 15 символов (NAME_PATTERN wireguard-android), поэтому
    вместе с префиксом метка времени режется до трёх цифр, а имя устройства —
    до остатка. Метка отличает разные выпуски одного устройства друг от друга
    (решение пользователя 2026-08-04). У sing-box-конфига для Hiddify такого
    лимита нет — там имя устройства не режем.
    """
    prefix = _name_prefix(transport, location)
    if transport == TRANSPORT_REALITY:
        slug = _UNSAFE_FILENAME.sub("", device_label.strip().lower()) or "vpn"
        return f"{prefix}{slug}.json"
    slug = _UNSAFE_FILENAME.sub("", device_label.strip().lower()) or "device"
    stamp = str(int(_time.time()))[-3:]
    budget = _MAX_TUNNEL_NAME - len(prefix) - len(stamp) - 1  # "_" перед меткой
    return f"{prefix}{slug[:budget]}_{stamp}.conf"

# --- Действия ---
ACTION_PEERS = "peers"  # админ: все пиры всех гостей
ACTION_ISSUE = "issue"  # выдать новый конфиг {chat_id, device_label}
ACTION_REISSUE = "reissue"  # перевыпустить: старый пир снимается, новый — тот же гость/label
ACTION_REVOKE = "revoke"  # отозвать {chat_id, device_label}
ACTION_USAGE = "usage"  # с chat_id — свой расход; без — сводка по всем (админ)
ACTION_SET_QUOTA = "set_quota"  # админ: прямой грант месяца {chat_id, bytes}
ACTION_GRANT_EXTRA = "grant_extra"  # гость сам себе +extra_step_gb, пока не упёрся в потолок
ACTION_REQUEST_EXTRA = "request_extra"  # заявка админу сверх потолка самообслуживания
ACTION_RESOLVE_REQUEST = "resolve_request"  # админ: {request_id, approve}
ACTION_APK_INFO = "apk_info"  # метаданные текущего кэша APK (проверяет свежесть)
ACTION_APK_CHUNK = "apk_chunk"  # {offset, length} — кусок файла для передачи через рой
ACTION_APK_SET_FILE_ID = "apk_set_file_id"  # бот сообщает id уже загруженного в Telegram файла

# Мониторинг доступности VPN (найдена нужда 2026-08-17 — см.
# vpn_check/protocol.py и domain/vpn_check.py).
ACTION_REPORT_CHECK = "report_check"  # vpn_check пушит {node, results: {target: {ok, ms, error}}}
ACTION_CHECK_NOW = "check_now"  # админ/nodectl: разослать проверки внеочередно, без ожидания тика
ACTION_CHECK_STATUS = "check_status"  # текущее состояние по всем (node, target)

# Прокси на jeeves (mtg — MTProto, microsocks — SOCKS5), 2026-08-17. Общий
# секрет/ссылка на всех — не per-guest (mtg принципиально не умеет
# несколько секретов в одном процессе), трафик агрегатный.
ACTION_PROXY_LINK = "proxy_link"  # tg://proxy + t.me ссылка, host/port/secret, SOCKS5-адрес
ACTION_PROXY_ROTATE_SECRET = "proxy_rotate_secret"  # админ: новый secret mtg, рестарт демона
ACTION_PROXY_USAGE = "proxy_usage"  # админ: агрегатный расход прокси этот месяц + node_limit_gb

# Секрет, развёрнутый вручную на jeeves 2026-08-13 (см. память
# telegram-bot-api-proxy-2026-08-13) — общий "бутстрап"-литерал для
# vpn/service.py::_proxy_secret (сидирует БД при первом старте после
# деплоя этого кода) И node/fixups.py::make_proxy_units_fixup (пишет тот
# же секрет в юнит mtg, если файла ещё нет) — гарантирует, что оба места
# сойдутся на одном и том же значении без похода друг к другу. Меняется
# только через proxy_rotate_secret, который правит и БД, и юнит разом.
PROXY_SECRET_SEED = "7p98I6xlxrC7ea7ysIxHCnR3d3cubWljcm9zb2Z0LmNvbQ"

# --- События ---
EVENT_VPN_PEER_ISSUED = "vpn_peer_issued"
EVENT_VPN_QUOTA_WARNING = "vpn_quota_warning"
EVENT_VPN_QUOTA_EXCEEDED = "vpn_quota_exceeded"
EVENT_VPN_PEER_BLOCKED = "vpn_peer_blocked"
EVENT_VPN_ACCESS_RESTORED = "vpn_access_restored"
EVENT_VPN_EXTRA_REQUESTED = "vpn_extra_requested"
EVENT_VPN_EXTRA_RESOLVED = "vpn_extra_resolved"
# Общий канал VDS приближается к лимиту тарифа (см. [vpn].node_limit_gb) —
# адресуется не гостю, а админам.
EVENT_VPN_NODE_QUOTA_WARNING = "vpn_node_quota_warning"
# Переход (node, target) в/из alerting — эмитится только на самом переходе
# (гистерезис domain/vpn_check.py), не на каждый неуспешный тик, это и есть
# "мут" повторных оповещений об одной и той же проблеме.
EVENT_VPN_CHECK_FAILED = "vpn_check_failed"
EVENT_VPN_CHECK_RECOVERED = "vpn_check_recovered"

# Код ошибки ProtoError: гость упёрся в потолок самообслуживания
# (self_ceiling_gb) — бот на этот код сам оформляет request_extra вместо
# grant_extra, без лишнего похода к пользователю.
ERR_QUOTA_CEILING = "vpn_quota_ceiling"
