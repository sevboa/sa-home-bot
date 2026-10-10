"""VpnService — ServiceHandler службы vpn: доступ к обходу на ноде роя с
белым IP, выдаваемый и учитываемый через бота.

Два транспорта под ОБЩЕЙ квотой (подэтап 39.0.x, решение пользователя
2026-09-10 — одна квота на гостя на сервер, независимо от транспорта):

* ``awg`` — AmneziaWG (UDP + обфускация), исходный (Этап 33). Пир = ключ
  WireGuard + адрес в подсети, учёт из ``awg show <iface> transfer``.
* ``reality`` — VLESS+Reality через xray-core (TCP/443), для РФ. Пир = UUID
  клиента xray + его email (переиспользуют колонки ``public_key``/``address``
  в ``vpn_peers``), учёт из ``xray api statsquery``, бэкенд —
  ``reality/xray.py`` (без sudo и без рестарта).

Какие транспорты держит нода — ``[vpn].transports`` (+ секция ``[vpn.reality]``
для второго). jeeves: ``["awg"]``; wooster: ``["reality"]``; можно оба.

Реконсайлер, а не разрозненные add/remove (решение из плана этапа 33):
``reconcile`` сравнивает желаемое состояние сервера (активные пиры
незаблокированных чатов — из БД, БД источник истины) с фактическим — по
каждому транспорту отдельно. Лишних снимает, недостающих добавляет.
Вызывается при старте (после рестарта сервера список пиров/юзеров пуст, БД
помнит всех) и на каждом тике сэмплера — тем же ходом закрывает и месячную
разблокировку 1-го числа, и снятие пира при блокировке по квоте, и
самовосстановление после ручных правок на сервере.

Приватность (решение плана): служба хранит только объёмы и время последнего
хендшейка. Ни адресов назначения, ни DNS-запросов, ни логов соединений —
adress пира это внутренний IP в VPN-подсети, не то, куда он ходит.

Деньги/трафик считаются в ГБ = 10^9 байт (десятичные, как у провайдеров),
в БД — байты.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import logging
import random
import socket
import time
import uuid as uuidlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.backup.snapshot import TRIGGER_ACTIONS as SNAPSHOT_TRIGGER_ACTIONS
from sa_home_bot.backup.snapshot import SnapshotBackup
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.domain.vpn_check import (
    ALERTING as CHECK_ALERTING,
)
from sa_home_bot.domain.vpn_check import (
    OK as CHECK_OK,
)
from sa_home_bot.domain.vpn_check import (
    CheckResult,
    KnownCheckState,
    reconcile_vpn_check,
    rollup_status,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.reality.client_config import (
    RealityParams,
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)
from sa_home_bot.reality.xray import XrayBackend
from sa_home_bot.vpn import apk as apk_client
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.awg import AwgBackend
from sa_home_bot.vpn.protocol import (
    ACTION_APK_CHUNK,
    ACTION_APK_INFO,
    ACTION_APK_SET_FILE_ID,
    ACTION_CHECK_NOW,
    ACTION_CHECK_STATUS,
    ACTION_GET_SUBSCRIPTION,
    ACTION_GET_VLESS,
    ACTION_GRANT_EXTRA,
    ACTION_ISSUE,
    ACTION_PEERS,
    ACTION_PROXY_LINK,
    ACTION_PROXY_ROTATE_SECRET,
    ACTION_PROXY_USAGE,
    ACTION_REISSUE,
    ACTION_REPORT_CHECK,
    ACTION_REQUEST_EXTRA,
    ACTION_RESOLVE_REQUEST,
    ACTION_REVOKE,
    ACTION_SET_ACCESS,
    ACTION_SET_QUOTA,
    ACTION_SUB_GEN,
    ACTION_SUB_LINKS,
    ACTION_TELEGRAM_EGRESS,
    ACTION_USAGE,
    ERR_QUOTA_CEILING,
    EVENT_VPN_ACCESS_RESTORED,
    EVENT_VPN_CHECK_FAILED,
    EVENT_VPN_CHECK_RECOVERED,
    EVENT_VPN_EXTRA_REQUESTED,
    EVENT_VPN_EXTRA_RESOLVED,
    EVENT_VPN_NODE_QUOTA_WARNING,
    EVENT_VPN_PEER_BLOCKED,
    EVENT_VPN_PEER_ISSUED,
    EVENT_VPN_QUOTA_EXCEEDED,
    EVENT_VPN_QUOTA_WARNING,
    EVENT_VPN_SERVER_RESTORED,
    PROXY_SECRET_SEED,
    SERVICE_NAME,
    TELEGRAM_EGRESS_TARGET,
    TELEGRAM_EGRESS_TRANSPORT,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
    TRANSPORTS,
    country_flag,
)
from sa_home_bot.vpn.proxy_backend import ProxyBackend, RealProxyBackend
from sa_home_bot.vpn.subweb import public_host as sub_public_host
from sa_home_bot.vpn_check import protocol as vpn_check_protocol

log = logging.getLogger(__name__)

# Байты в гигабайте — десятичный (10^9), как считают провайдеры трафика, а
# не 2^30 (гибибайт): пользователю обещают «500 ГБ», это должно совпадать.
GB = 1_000_000_000
# Предел имени устройства, заданного ботом (этап 57.1).
MAX_DEVICE_LABEL_LEN = 64
# chat_id пробника vpn_check (node/fixups.py::VPN_PROBE_CHAT_ID) — не гость.
PROBE_CHAT_ID = 0

# Сентинельный chat_id для учёта состояния "весь канал ноды" в тех же
# таблицах, что и учёт по чатам (vpn_quota_state) — реальный Telegram
# chat_id никогда не бывает 0, коллизии не будет.
NODE_SENTINEL_CHAT_ID = 0

# Локальные копии констант сервиса node (node/service.py::SERVICE_NAME,
# ACTION_TRIGGER_PEERS) — тот же приём, что уже используют node/peers.py,
# node/lease.py, wake_core.py и др. (свой NODE_SERVICE = "node" в каждом),
# чтобы не тянуть в лёгкую службу vpn тяжёлые зависимости node/service.py
# (Supervisor, LeaseManager и т.п.) ради двух строковых констант.
NODE_SERVICE = "node"
ACTION_TRIGGER_PEERS = "trigger_peers"

# Сколько циклов проверки строка vpn_check_states считается свежей (39.0.7(f)).
# Наблюдатель может замолчать не сказав ни слова — его нода умерла, ушла из
# роя или потеряла интернет; последняя запись при этом остаётся в таблице
# навсегда. Держать её в индикаторе /vpn нельзя: зелёный от мертвеца хуже,
# чем отсутствие индикатора. Три интервала, а не один: пропущенный тик —
# обычный джиттер (пробник поднимает тоннель на время цикла), мигать из-за
# него незачем.
CHECK_STALE_FACTOR = 3

# Имя устройства (решение пользователя 2026-08-04) больше не вводит ни
# человек, ни модель — служба сама выбирает случайное слово из этого пула
# при выдаче. Только английские буквы и не длиннее 8 символов: имя файла
# .conf = "<слово>_<таймстамп>" не должно превышать 15 символов — предел
# имени тоннеля wireguard-android (NAME_PATTERN, см. bot/handlers/vpn.py и
# bot/tools.py, где имя файла реально собирается).
_FLOWER_NAMES = (
    "Rose",
    "Lily",
    "Iris",
    "Aster",
    "Poppy",
    "Daisy",
    "Tulip",
    "Lotus",
    "Phlox",
    "Pansy",
    "Sedum",
    "Canna",
    "Hosta",
    "Orchid",
    "Violet",
    "Dahlia",
    "Azalea",
    "Camellia",
    "Jasmine",
    "Lilac",
    "Peony",
    "Zinnia",
    "Yarrow",
    "Crocus",
    "Freesia",
    "Begonia",
    "Petunia",
    "Gerbera",
    "Mallow",
    "Cosmos",
)


def _random_device_label(used: set[str]) -> str:
    """Случайное имя, по возможности не повторяющее уже активные устройства
    этого гостя (не критично для работы — только чтобы список в /vpn не
    путал одинаковыми именами; настоящая уникальность пира — public_key)."""
    pool = [name for name in _FLOWER_NAMES if name not in used] or list(_FLOWER_NAMES)
    return random.choice(pool)


# Сколько держать ответ apk_info без повторного похода на GitHub — гость,
# нажавший кнопку дважды подряд, не должен провоцировать два запроса к API.
APK_INFO_MEMO_S = 60.0
APK_API_TIMEOUT_S = 15.0
APK_DOWNLOAD_TIMEOUT_S = 90.0
# Кусок APK на одно сообщение протокола роя — с запасом от MAX_MESSAGE_BYTES
# (1 МиБ, proto/messages.py): base64 раздувает байты на треть, плюс сам
# конверт JSON.
APK_CHUNK_BYTES = 700 * 1024

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _allocate_address(subnet: str, used: set[str]) -> str:
    """Наименьший свободный адрес подсети — первый хост (обычно .1)
    зарезервирован под сам сервер и в ``used`` не участвует."""
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = list(network.hosts())
    for host in hosts[1:]:
        addr = str(host)
        if addr not in used:
            return addr
    raise ProtoError(ERR_BAD_REQUEST, "подсеть VPN исчерпана — нет свободных адресов")


def _render_client_conf(cfg: Any, private_key: str, address: str, server_public_key: str) -> str:
    endpoint = f"{cfg.endpoint_host}:{cfg.endpoint_port}"
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address}/32\n"
        f"DNS = {cfg.dns}\n"
        f"MTU = {cfg.mtu}\n"
        f"Jc = {cfg.jc}\n"
        f"Jmin = {cfg.jmin}\n"
        f"Jmax = {cfg.jmax}\n"
        f"S1 = {cfg.s1}\n"
        f"S2 = {cfg.s2}\n"
        f"H1 = {cfg.h1}\n"
        f"H2 = {cfg.h2}\n"
        f"H3 = {cfg.h3}\n"
        f"H4 = {cfg.h4}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_public_key}\n"
        f"Endpoint = {endpoint}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )


def _reality_email(chat_id: int, device_label: str) -> str:
    """email клиента xray — человекочитаемый ярлык для ``statsquery`` на
    сервере, уникальный среди активных (у чата не бывает двух активных
    устройств с одним именем). Для reality-пиров хранится в
    ``vpn_peers.address`` (у awg-пиров там IP подсети)."""
    return f"c{chat_id}-{device_label}"


def _render_qr_png_b64(text: str) -> str:
    import segno

    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=4, border=2)
    return base64.b64encode(buf.getvalue()).decode()


class VpnService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        backend: AwgBackend,
        emit: EmitFn,
        *,
        node_link: ServiceLink | None = None,
        proxy_backend: ProxyBackend | None = None,
        reality_backend: XrayBackend | None = None,
    ) -> None:
        self._cfg = settings.vpn
        self._db = db
        self._backend = backend
        self._reality = reality_backend
        self._reality_cfg = settings.vpn.reality
        # Транспорты, реально доступные на этой ноде: из [vpn].transports, но
        # reality — только если бэкенд xray собран (есть секция [vpn.reality]).
        # awg доступен всегда (бэкенд конструируется без побочных эффектов).
        wanted = list(settings.vpn.transports or [TRANSPORT_AWG])
        self._transports: tuple[str, ...] = tuple(
            t
            for t in wanted
            if t == TRANSPORT_AWG or (t == TRANSPORT_REALITY and reality_backend is not None)
        )
        self._proxy_backend = proxy_backend or RealProxyBackend()
        self._emit = emit
        self._node = socket.gethostname()
        self._server_pubkey: str | None = None
        self._apk_checked_at: datetime | None = None
        # Клиент к своей же локальной ноде — для рассылки проверок
        # доступности VPN (см. check_loop/_dispatch_checks). None в тестах,
        # которые конструируют службу напрямую — тогда рассылка тихо
        # логирует предупреждение и ничего не делает.
        self._node_link = node_link
        # Бэкап снапшота БД напарнику (39.0.8(c), backup/snapshot.py); ставит app.py.
        self.backup: SnapshotBackup | None = None
        # Проверка «пара (chat, label, transport) свободна» и INSERT — одним
        # куском: уникальность активной пары держится кодом (индекс на старых
        # базах с возможными дублями не создать), поэтому два параллельных
        # issue не должны проскочить мимо проверки друг друга.
        self._issue_lock = asyncio.Lock()
        # Подписка Hiddify (57.10): ключ подписи токенов, веб-сервер (ставит
        # app.py), кэши сборки и список соседних vpn-нод.
        self._node_id = settings.node.id or socket.gethostname()
        self._sub_secret = subs.derive_secret(self._cfg.sub_secret, settings.swarm.token)
        self.sub_web: Any = None  # vpn.subweb.SubscriptionWeb
        self._sub_cache: dict[str, tuple[float, subs.Subscription | None]] = {}
        self._sub_stale: dict[tuple[str, str], list[subs.SubEntry]] = {}
        self._peer_nodes_cache: tuple[float, list[str]] | None = None

    def _has(self, transport: str) -> bool:
        return transport in self._transports

    def describe(self) -> ServiceDescription:
        chat_id_param = ActionParam(name="chat_id", type="int", title="Чей это гость")
        device_param = ActionParam(name="device_label", type="string", title="Устройство")
        optional_device_param = ActionParam(
            name="device_label", type="string", required=False, title="Устройство (имя)"
        )
        # transport необязателен: если нода несёт один транспорт — берётся он;
        # если оба (awg + reality) — бот/модель указывают, какой под устройство.
        transport_param = ActionParam(
            name="transport", type="string", required=False, title="Транспорт (awg/reality)"
        )

        capabilities: list[str] = [
            ACTION_PEERS,
            ACTION_ISSUE,
            ACTION_REISSUE,
            ACTION_REVOKE,
            ACTION_USAGE,
            ACTION_SET_QUOTA,
            ACTION_SET_ACCESS,
            ACTION_GRANT_EXTRA,
            ACTION_REQUEST_EXTRA,
            ACTION_RESOLVE_REQUEST,
        ]
        actions: list[ActionSpec] = [
            ActionSpec(id=ACTION_PEERS, title="🔌 Все пиры"),
            ActionSpec(
                id=ACTION_ISSUE,
                # Имя устройства: без device_label служба выбирает сама
                # (случайный цветок, решение 2026-08-04); с device_label
                # (этап 57.1) — выдаёт под этим именем, повтор активной пары
                # (label, transport) — ошибка, не дубль.
                title="➕ Выдать доступ",
                params=(chat_id_param, transport_param, optional_device_param),
            ),
            ActionSpec(
                id=ACTION_REISSUE,
                title="🔄 Перевыпустить",
                params=(chat_id_param, device_param, transport_param),
            ),
            ActionSpec(
                id=ACTION_REVOKE,
                title="🚫 Отозвать",
                params=(chat_id_param, device_param, transport_param),
            ),
            ActionSpec(
                id=ACTION_USAGE,
                title="📊 Расход",
                params=(ActionParam(name="chat_id", type="int", required=False, title="Чей"),),
            ),
            ActionSpec(
                id=ACTION_SET_QUOTA,
                title="🎚 Задать квоту",
                params=(
                    chat_id_param,
                    ActionParam(name="bytes", type="int", title="Лимит месяца, байт"),
                ),
            ),
            ActionSpec(
                id=ACTION_SET_ACCESS,
                title="🎟 Допуск на сервер",
                params=(
                    chat_id_param,
                    ActionParam(name="allowed", type="bool", title="Допущен"),
                    ActionParam(
                        name="base_gb",
                        type="int",
                        required=False,
                        title="Личная база, ГБ (пусто — общая)",
                    ),
                ),
            ),
            ActionSpec(id=ACTION_GRANT_EXTRA, title="➕100 ГБ", params=(chat_id_param,)),
            ActionSpec(
                id=ACTION_REQUEST_EXTRA,
                title="✋ Заявка на трафик",
                params=(
                    chat_id_param,
                    ActionParam(name="bytes", type="int", required=False, title="Сколько"),
                ),
            ),
            ActionSpec(
                id=ACTION_RESOLVE_REQUEST,
                title="✅ Решить заявку",
                params=(
                    ActionParam(name="request_id", type="int", title="Номер заявки"),
                    ActionParam(name="approve", type="bool", title="Одобрить"),
                ),
            ),
        ]
        if self._reality is not None and self._reality_cfg is not None:
            capabilities += [ACTION_GET_VLESS]
            actions += [
                ActionSpec(
                    id=ACTION_GET_VLESS,
                    title="🔗 Ссылка VLESS",
                    params=(chat_id_param, device_param),
                ),
            ]
        # Подписка Hiddify (57.10): get_subscription — для бота; sub_links —
        # служебное между vpn-нодами (собрать VLESS устройства по токену).
        if self._reality is not None and self._reality_cfg is not None:
            capabilities += [ACTION_GET_SUBSCRIPTION, ACTION_SUB_LINKS, ACTION_SUB_GEN]
            actions += [
                ActionSpec(
                    id=ACTION_GET_SUBSCRIPTION,
                    title="🔌 Подписка Hiddify",
                    params=(chat_id_param, device_param),
                ),
                ActionSpec(
                    id=ACTION_SUB_LINKS,
                    title="🔗 VLESS по токену подписки",
                    params=(ActionParam(name="token", type="string", title="Токен"),),
                ),
                ActionSpec(
                    id=ACTION_SUB_GEN,
                    title="🔗 Поколение токена устройства",
                    params=(chat_id_param, device_param),
                ),
            ]
        # APK AmneziaWG — только там, где awg раздают: без него файл клиента
        # гостю не нужен.
        if self._has(TRANSPORT_AWG):
            capabilities += [ACTION_APK_INFO]
            actions += [
                ActionSpec(id=ACTION_APK_INFO, title="📱 Приложение"),
                ActionSpec(
                    id=ACTION_APK_CHUNK,
                    title="📦 Кусок APK",
                    params=(
                        ActionParam(name="offset", type="int", title="Смещение"),
                        ActionParam(name="length", type="int", required=False, title="Длина"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_APK_SET_FILE_ID,
                    title="🆔 Запомнить file_id",
                    params=(ActionParam(name="telegram_file_id", type="string", title="file_id"),),
                ),
            ]
        # Проверки доступности — на любой ноде со службой vpn: сама служба тут
        # только принимает и отдаёт строки vpn_check_states, туннель поднимает
        # пробник (vpn_check). Раньше висело на awg-гейте — и reality-only
        # нода не могла принять отчёт даже собственного пробника.
        capabilities += [ACTION_CHECK_NOW, ACTION_CHECK_STATUS, ACTION_TELEGRAM_EGRESS]
        actions += [
            # Служебное — зовёт только сама служба vpn_check, не для UI.
            ActionSpec(
                id=ACTION_REPORT_CHECK,
                title="📡 Отчёт проверки VPN",
                params=(
                    ActionParam(name="node", type="string", title="Нода"),
                    ActionParam(name="results", title="Результаты"),
                ),
            ),
            ActionSpec(id=ACTION_CHECK_NOW, title="🛰 Проверить сеть сейчас"),
            ActionSpec(id=ACTION_CHECK_STATUS, title="🛰 Статус проверок сети"),
            # Служебное чтение для бота (этап 52), не для UI.
            ActionSpec(
                id=ACTION_TELEGRAM_EGRESS,
                title="📡 Маршрут до Telegram через эту ноду",
                params=(ActionParam(name="observer", type="string", title="Нода бота"),),
            ),
        ]
        # Прокси Telegram (mtg/microsocks) живёт на VPS сам по себе и от
        # VPN-транспорта не зависит: на wooster он поднят при reality-only
        # раскладке (2026-09-06). Гейт — по факту настройки, как и в _proxy_link.
        if self._cfg.mtg_public_host:
            capabilities += [
                ACTION_PROXY_LINK,
                ACTION_PROXY_ROTATE_SECRET,
                ACTION_PROXY_USAGE,
            ]
            actions += [
                ActionSpec(id=ACTION_PROXY_LINK, title="🌐 Ссылка прокси"),
                ActionSpec(id=ACTION_PROXY_ROTATE_SECRET, title="🔁 Сменить секрет прокси"),
                ActionSpec(id=ACTION_PROXY_USAGE, title="📊 Расход прокси"),
            ]
        if self.backup is not None:  # служебное, не для UI (backup/snapshot.py)
            for spec in self.backup.action_specs():
                capabilities.append(spec.id)
                actions.append(spec)
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=tuple(capabilities),
            actions=tuple(actions),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM vpn_peers WHERE status = 'active'"
        )
        row = await cur.fetchone()
        # Публичный ключ awg-сервера (кэширован, меняется только с
        # пересборкой VPS). Не секрет — он и так лежит в конфиге каждого
        # гостя; нужен пробникам, чтобы заметить, что их конфиг протух
        # (node/fixups.py::_probe_conf_stale). Живой случай 2026-09-20:
        # jeeves переустановили 18.09, конфиг пробника на wooster остался с
        # 01.09 со старым ключом — фикс считал «файл на месте, всё применено»
        # и не перевыпускал, а awg wooster→jeeves молча не поднимался месяц.
        server_pubkey: str | None = None
        if self._has(TRANSPORT_AWG):
            # Ключ читается у живого интерфейса (awg show) — на ноде, где awg
            # почему-то не поднят, это не повод валить весь get_state: его
            # дёргает бот на каждый экран.
            try:
                server_pubkey = await self._server_public_key()
            except Exception:  # noqa: BLE001 — любая беда с awg тут не фатальна
                log.warning("vpn: не удалось прочитать публичный ключ awg-сервера", exc_info=True)
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "active_peers": row["n"] if row else 0,
            "label": self._cfg.location,
            "server_public_key": server_pubkey,
            # Транспорты этой ноды — бот по ним решает, предлагать ли выбор
            # (awg/reality) в карточке «➕ Новое устройство».
            "transports": list(self._transports),
            # То же, что в usage: индикатор по транспортам (39.0.7(f)) — здесь
            # он нужен пикерам локации/технологии, которые строятся из
            # get_state (bot/vpn_nodes.py::live_vpn_servers), а не из usage.
            "check": await self._check_rollup(server=self._node),
            # Умеет ли эта нода допуск (set_access). Флаг в get_state, а не
            # в describe: бот и так дёргает состояние на каждый экран
            # (bot/vpn_nodes.py::live_vpn_servers), а лишний describe ради
            # одного признака гонять по рою незачем. Старая нода поля не
            # шлёт — бот читает отсутствие как «допуск не ведёт, пущены все».
            "access_control": True,
        }

    # --- вспомогательное ---

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — доступ выдаётся человеку")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

    async def _server_public_key(self) -> str:
        if self._server_pubkey is None:
            self._server_pubkey = await self._backend.server_public_key()
        return self._server_pubkey

    async def _active_addresses(self) -> set[str]:
        # Только awg-пиры: у reality-пира в колонке address лежит email
        # ("c<chat>-<label>"), а не IP подсети — в пул адресов он не входит.
        cur = await self._db.conn.execute(
            "SELECT address FROM vpn_peers WHERE status = 'active' AND transport = ?",
            (TRANSPORT_AWG,),
        )
        return {row["address"] for row in await cur.fetchall()}

    async def _blocked_chats(self, month: str) -> set[int]:
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM vpn_quota_state WHERE month = ? AND blocked_at IS NOT NULL",
            (month,),
        )
        return {row["chat_id"] for row in await cur.fetchall()}

    async def _allowed_chats(self) -> set[int]:
        cur = await self._db.conn.execute("SELECT chat_id FROM vpn_chat_access WHERE allowed = 1")
        # Сентинель ноды — всегда свой (см. _access): пробник vpn_check не
        # гость, снимать его с интерфейса реконсайлером нельзя.
        return {row["chat_id"] for row in await cur.fetchall()} | {NODE_SENTINEL_CHAT_ID}

    async def _quota_state(self, chat_id: int, month: str) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT warned_limit_bytes, blocked_at FROM vpn_quota_state "
            "WHERE chat_id = ? AND month = ?",
            (chat_id, month),
        )
        row = await cur.fetchone()
        if row is None:
            return {"warned_limit_bytes": None, "blocked_at": None}
        return {"warned_limit_bytes": row["warned_limit_bytes"], "blocked_at": row["blocked_at"]}

    async def _set_quota_state(self, chat_id: int, month: str, **updates: Any) -> None:
        current = await self._quota_state(chat_id, month)
        current.update(updates)
        await self._db.conn.execute(
            "INSERT INTO vpn_quota_state (chat_id, month, warned_limit_bytes, blocked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, month) DO UPDATE SET "
            "warned_limit_bytes = excluded.warned_limit_bytes, blocked_at = excluded.blocked_at",
            (chat_id, month, current["warned_limit_bytes"], current["blocked_at"]),
        )
        await self._db.conn.commit()

    async def _used_bytes(self, chat_id: int, month: str) -> int:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(u.used_bytes), 0) AS total FROM vpn_peer_usage u "
            "JOIN vpn_peers p ON p.id = u.peer_id WHERE p.chat_id = ? AND u.month = ?",
            (chat_id, month),
        )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _granted_bytes(self, chat_id: int, month: str, *, source: str | None = None) -> int:
        if source is None:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM vpn_quota_grants "
                "WHERE chat_id = ? AND month = ?",
                (chat_id, month),
            )
        else:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM vpn_quota_grants "
                "WHERE chat_id = ? AND month = ? AND source = ?",
                (chat_id, month, source),
            )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _access(self, chat_id: int) -> tuple[bool, int | None]:
        """``(допущен, личная база в байтах)`` — нет строки значит «не допущен»
        (fail-closed: новый гость закрыт, пока владелец не откроет локацию).

        Сентинель ноды — исключение: под ``chat_id = 0`` ходит не гость, а сама
        нода (пробник vpn_check, node/fixups.py::VPN_PROBE_CHAT_ID выпускает
        его тем же ``issue``). Допуск — про людей, которых пускают на сервер;
        закрывать им собственную диагностику незачем, а на ноде, где пробника
        ещё не заводили, бэкфилл строки не создаст, и `nodectl fix` не смог бы
        его выпустить вовсе.
        """
        if chat_id == NODE_SENTINEL_CHAT_ID:
            return True, None
        cur = await self._db.conn.execute(
            "SELECT allowed, base_bytes FROM vpn_chat_access WHERE chat_id = ?", (chat_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return False, None
        return bool(row["allowed"]), row["base_bytes"]

    async def _base_bytes(self, chat_id: int) -> int:
        """База месяца: личная, если задана, иначе общая из конфига."""
        _allowed, base = await self._access(chat_id)
        return self._cfg.base_quota_gb * GB if base is None else int(base)

    async def _require_access(self, chat_id: int) -> None:
        allowed, _base = await self._access(chat_id)
        if allowed:
            return
        raise ProtoError(
            ERR_BAD_REQUEST,
            f"на сервере «{self._cfg.location or self._node}» доступ не открыт — "
            "его выдаёт владелец в /vpn → «👥 Все гости»",
        )

    async def _limit_bytes(self, chat_id: int, month: str) -> int:
        # Недопущенному лимит НЕ обнуляем: допуск — отдельная ось, а нулевой
        # лимит прочитался бы как «исчерпал квоту» (см. _check_thresholds).
        return await self._base_bytes(chat_id) + await self._granted_bytes(chat_id, month)

    async def _add_grant(
        self, chat_id: int, month: str, bytes_: int, *, source: str, request_id: int | None = None
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO vpn_quota_grants (chat_id, month, bytes, source, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, month, bytes_, source, request_id, _now().isoformat()),
        )
        await self._db.conn.commit()

    async def _device_usage(
        self, chat_id: int, month: str, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Трафик за месяц по устройствам и подключениям (этап 57.1).

        Устройство = device_label; в его трафик входят ВСЕ ключи этого имени,
        в том числе перевыпущенные (expired) и отозванные в этом месяце, —
        иначе перевыпуск обнулял бы цифру. Подключение = активная пара
        (label, transport); его ``used_bytes`` считается так же, по транспорту.
        Устройство без активного подключения в список не попадает.
        """
        cur = await self._db.conn.execute(
            "SELECT p.device_label AS label, COALESCE(p.transport, 'awg') AS transport, "
            "COALESCE(SUM(u.used_bytes), 0) AS used "
            "FROM vpn_peers p JOIN vpn_peer_usage u ON u.peer_id = p.id "
            "WHERE p.chat_id = ? AND u.month = ? GROUP BY p.device_label, 2",
            (chat_id, month),
        )
        by_pair = {(r["label"], r["transport"]): int(r["used"]) for r in await cur.fetchall()}
        result: dict[str, dict[str, Any]] = {}
        for dev in devices:
            label = dev["device_label"]
            transport = dev["transport"]
            entry = result.setdefault(label, {"device_label": label, "connections": []})
            entry["connections"].append(
                {
                    "transport": transport,
                    "status": dev["status"],
                    "last_handshake_at": dev["last_handshake_at"],
                    "created_at": dev["created_at"],
                    "broken": bool(dev.get("broken", False)),
                    "used_bytes": by_pair.get((label, transport), 0),
                }
            )
        for label, entry in result.items():
            # Трафик устройства — по всем транспортам имени, включая те, у кого
            # активного подключения уже нет (отозвали awg, остался vless).
            entry["used_bytes"] = sum(v for (lbl, _t), v in by_pair.items() if lbl == label)
        return list(result.values())

    async def _peers_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT device_label, transport, status, created_at, last_handshake_at, server "
            "FROM vpn_peers WHERE chat_id = ? AND status = 'active' ORDER BY created_at",
            (chat_id,),
        )
        return [
            {
                "device_label": row["device_label"],
                "transport": row["transport"] or TRANSPORT_AWG,
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]

    async def _mark_broken(
        self, chat_id: int, devices: list[dict[str, Any]], *, withheld: bool
    ) -> list[dict[str, Any]]:
        """``broken: True`` у устройств, которых реально нет на сервере (39.0.8(e)).

        Сверка с ФАКТОМ (интерфейс awg / клиенты xray), а не с БД. ``withheld`` —
        пиры гостя сняты за квоту или допуск: reconcile держит их вне интерфейса
        намеренно, это не поломка. Любая неясность (сбой чтения, пир без
        ключа) — не ломаем: ложное «перевыпустите» хуже молчания.
        """
        for device in devices:
            device["broken"] = False
        cur = await self._db.conn.execute(
            "SELECT device_label, transport, public_key, address, server_pubkey "
            "FROM vpn_peers WHERE chat_id = ? AND status = 'active'",
            (chat_id,),
        )
        rows = {
            (row["device_label"], row["transport"] or TRANSPORT_AWG): row
            for row in await cur.fetchall()
        }
        try:
            live_awg = (
                set((await self._backend.transfer()).keys()) if self._has(TRANSPORT_AWG) else None
            )
            live_r = (
                await self._reality.list_clients()
                if self._reality is not None and self._reality_cfg is not None
                else None
            )
            live_key = await self._server_public_key() if self._has(TRANSPORT_AWG) else None
        except Exception:  # noqa: BLE001 — не смогли прочитать факт: не судим
            log.warning("vpn: сверка пиров с сервером не удалась", exc_info=True)
            return devices
        for device in devices:
            row = rows.get((device["device_label"], device.get("transport") or TRANSPORT_AWG))
            if row is None:
                continue
            if (row["transport"] or TRANSPORT_AWG) == TRANSPORT_AWG:
                if live_awg is None:
                    continue
                stale_key = bool(row["server_pubkey"] and live_key) and (
                    row["server_pubkey"] != live_key
                )
                missing = not withheld and row["public_key"] not in live_awg
                device["broken"] = bool(stale_key or missing)
            elif live_r is not None and not withheld:
                device["broken"] = row["address"] not in live_r
        return devices

    # --- миграция данных ---

    async def backfill_server(self) -> None:
        """Пиры, выданные до этапа 39, не знают своего сервера — все они на
        этой ноде (единственной с `vpn` до появления второго сервера).
        Проставляем `[node].id` разово; на нодах со свежей БД строк нет."""
        cur = await self._db.conn.execute(
            "UPDATE vpn_peers SET server = ? WHERE server IS NULL", (self._node,)
        )
        if cur.rowcount:
            await self._db.conn.commit()
            log.info("vpn: проставлен server=%s у %d старых пиров", self._node, cur.rowcount)

    async def backfill_access(self) -> None:
        """Гости с живыми пирами были допущены де-факто — до vpn_chat_access
        допуска не существовало вовсе. Проставляем им ``allowed = 1`` с общей
        базой, чтобы обновление службы никого не выставило за дверь.

        ``DO NOTHING``, а не upsert: рестарт не должен воскрешать допуск,
        который владелец снял руками (пиры при снятии остаются ``active``,
        см. reconcile — их просто не поднимают на интерфейсе).
        """
        cur = await self._db.conn.execute(
            "INSERT INTO vpn_chat_access (chat_id, allowed, base_bytes, updated_at) "
            "SELECT DISTINCT chat_id, 1, NULL, ? FROM vpn_peers WHERE status = 'active' "
            "ON CONFLICT(chat_id) DO NOTHING",
            (_now().isoformat(),),
        )
        if cur.rowcount:
            await self._db.conn.commit()
            log.info("vpn: допуск проставлен %d гостям с активными пирами", cur.rowcount)
        # Гость, у которого все пиры отозваны, под бэкфилл не попадает и
        # сам себе новый конфиг уже не выпустит. Это осознанно (новые —
        # только по явной выдаче), но владельцу стоит знать, кого это
        # задело: иначе он узнает об этом из жалобы.
        cur = await self._db.conn.execute(
            "SELECT DISTINCT chat_id FROM vpn_peers "
            "WHERE chat_id NOT IN (SELECT chat_id FROM vpn_chat_access)"
        )
        orphans = [row["chat_id"] for row in await cur.fetchall()]
        if orphans:
            log.info("vpn: гости с историей, но без активных пиров — допуск не выдан: %s", orphans)

    # --- reconciler ---

    async def reconcile(self) -> None:
        month = _month_key(_now())
        blocked = await self._blocked_chats(month)
        allowed = await self._allowed_chats()
        cur = await self._db.conn.execute(
            "SELECT chat_id, transport, public_key, address FROM vpn_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        # На интерфейс идут допущенные и не исчерпавшие квоту. Строки в БД не
        # трогаем: пир остаётся `active`, поэтому возврат допуска не требует
        # перевыпуска конфига у гостя — тот же ключ поднимется обратно.
        active = [
            row for row in rows if row["chat_id"] in allowed and row["chat_id"] not in blocked
        ]

        if self._has(TRANSPORT_AWG):
            desired = {
                row["public_key"]: row["address"]
                for row in active
                if (row["transport"] or TRANSPORT_AWG) == TRANSPORT_AWG
            }
            current = set((await self._backend.transfer()).keys())
            for pubkey in current - desired.keys():
                await self._backend.remove_peer(pubkey)
            for pubkey, address in desired.items():
                if pubkey not in current:
                    await self._backend.add_peer(pubkey, address)

        if self._reality is not None and self._reality_cfg is not None:
            # reality: ключ — email (совпадает с ключом list_clients/statsquery
            # на сервере), значение — UUID клиента xray (колонка public_key).
            desired_r = {
                row["address"]: row["public_key"]
                for row in active
                if row["transport"] == TRANSPORT_REALITY
            }
            current_r = await self._reality.list_clients()
            for email in current_r - desired_r.keys():
                await self._reality.remove_client(email)
            for email, client_uuid in desired_r.items():
                if email not in current_r:
                    await self._reality.add_client(client_uuid, email, self._reality_cfg.flow)

    # --- сравнение после восстановления (этап 57.1) ---

    async def _meta_get(self, key: str) -> str | None:
        cur = await self._db.conn.execute("SELECT value FROM vpn_meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def _meta_set(self, key: str, value: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO vpn_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, _now().isoformat()),
        )
        await self._db.conn.commit()

    async def check_restore(self) -> list[dict[str, Any]]:
        """Звать на старте ПОСЛЕ reconcile(): не сменился ли ключ сервера.

        Задет тот, чей ключ сервера в БД (``vpn_peers.server_pubkey``) не равен
        текущему, а если на этой ноде запомненный отпечаток ключа транспорта
        (``vpn_meta``) сменился — все активные пиры этого транспорта. Отсутствие
        пира на интерфейсе само по себе НЕ признак: после перезагрузки сервера
        интерфейс пуст, reconcile() возвращает пиров, и при прежнем ключе у гостей
        всё продолжает работать. Событие шлётся один раз на смену (отпечаток
        запоминается), пробник chat_id=0 исключён. Возвращает список задетых.
        """
        fingerprints: dict[str, str] = {}
        try:
            if self._has(TRANSPORT_AWG):
                fingerprints[TRANSPORT_AWG] = await self._server_public_key()
        except Exception:  # noqa: BLE001 — не смогли прочитать ключ: не судим
            log.warning("vpn: сверка ключа awg-сервера после старта не удалась", exc_info=True)
        if (
            self._reality is not None
            and self._reality_cfg is not None
            and self._reality_cfg.server_public_key
        ):
            fingerprints[TRANSPORT_REALITY] = self._reality_cfg.server_public_key
        affected: dict[tuple[int, str, str], None] = {}
        key_changed = False
        for transport, current in fingerprints.items():
            meta_key = f"server_key:{transport}"
            recorded = await self._meta_get(meta_key)
            if recorded == current:
                continue
            key_changed = key_changed or recorded is not None
            cur = await self._db.conn.execute(
                "SELECT chat_id, device_label, server_pubkey FROM vpn_peers "
                "WHERE status = 'active' AND chat_id != ? "
                "AND COALESCE(transport, 'awg') = ? ORDER BY chat_id, created_at",
                (PROBE_CHAT_ID, transport),
            )
            for row in await cur.fetchall():
                stale = bool(row["server_pubkey"]) and row["server_pubkey"] != current
                if stale or recorded is not None:
                    affected[(row["chat_id"], row["device_label"], transport)] = None
            await self._meta_set(meta_key, current)
        items = [
            {"chat_id": c, "device_label": label, "transport": t} for (c, label, t) in affected
        ]
        if items or key_changed:
            # Ключ сменился, а задетых нет — событие всё равно уходит: бот скажет
            # владельцу одну строку (57.7); гостям при пустом списке не пишет.
            log.warning("vpn: ключ сервера сменился, задето подключений: %d", len(items))
            await self._emit(
                EVENT_VPN_SERVER_RESTORED,
                {"node": self._node, "location": self._cfg.location, "affected": items},
            )
        return items

    # --- issue/reissue/revoke ---

    async def _active_labels(self, chat_id: int) -> set[str]:
        cur = await self._db.conn.execute(
            "SELECT device_label FROM vpn_peers WHERE chat_id = ? AND status = 'active'",
            (chat_id,),
        )
        return {row["device_label"] for row in await cur.fetchall()}

    async def _active_row(self, chat_id: int, label: str, transport: str | None):
        """Активная запись пира по (chat, label[, transport]). Без transport —
        старое поведение: любая активная запись под этим именем (самая ранняя)."""
        sql = (
            "SELECT id, public_key, address, transport FROM vpn_peers "
            "WHERE chat_id = ? AND device_label = ? AND status = 'active'"
        )
        params: list[Any] = [chat_id, label]
        if transport:
            # transport у старых строк мог быть NULL до миграции — трактуем как awg.
            sql += " AND COALESCE(transport, 'awg') = ?"
            params.append(transport)
        cur = await self._db.conn.execute(sql + " ORDER BY created_at, id", params)
        return await cur.fetchone()

    @staticmethod
    def _optional_transport(args: dict[str, Any]) -> str | None:
        """transport для reissue/revoke/get_vless: пусто — старое поведение."""
        raw = str(args.get("transport") or "").strip().lower()
        if not raw:
            return None
        if raw not in TRANSPORTS:
            raise ProtoError(ERR_BAD_REQUEST, f"неизвестный транспорт {raw!r}")
        return raw

    @staticmethod
    def _explicit_label(args: dict[str, Any]) -> str | None:
        """Заданное ботом имя устройства (этап 57.1) или None — тогда случайное."""
        label = str(args.get("device_label") or "").strip()
        if not label:
            return None
        if len(label) > MAX_DEVICE_LABEL_LEN or any(ord(c) < 32 for c in label):
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"имя устройства не длиннее {MAX_DEVICE_LABEL_LEN} символов и без управляющих",
            )
        return label

    def _resolve_transport(self, args: dict[str, Any]) -> str:
        """Какой транспорт выдавать. Явный ``transport`` в args валидируется
        против списка этой ноды; без него — единственный транспорт ноды, а
        если их несколько — ошибка «уточните transport»."""
        raw = str(args.get("transport") or "").strip().lower()
        if raw:
            if raw not in self._transports:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"транспорт {raw!r} на сервере {self._node} недоступен "
                    f"(есть: {', '.join(self._transports) or '—'})",
                )
            return raw
        if len(self._transports) == 1:
            return self._transports[0]
        if not self._transports:
            raise ProtoError(
                ERR_BAD_REQUEST, f"на сервере {self._node} нет ни одного транспорта VPN"
            )
        raise ProtoError(
            ERR_BAD_REQUEST,
            f"сервер {self._node} несёт несколько транспортов "
            f"({', '.join(self._transports)}) — укажите transport",
        )

    async def _issue(
        self,
        args: dict[str, Any],
        *,
        forced_label: str | None = None,
        forced_transport: str | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        transport = forced_transport or self._resolve_transport(args)
        # Проверка ДО генерации ключа и выделения адреса: у отказа не должно
        # быть следов — ни пира в БД, ни занятого адреса в подсети.
        await self._require_access(chat_id)
        # Число устройств на гостя намеренно не ограничено (решение
        # пользователя 2026-08-03) — реальный потолок стоимости уже задаёт
        # трафик (base_quota_gb/self_ceiling_gb), отдельный счётчик устройств
        # был бы лишней строгостью.
        #
        # Имя устройства больше не спрашиваем (решение пользователя
        # 2026-08-04) — гость и модель путались, что тут вообще вводить;
        # ``forced_label`` — только для _reissue ниже: у СУЩЕСТВУЮЩЕГО
        # устройства имя не меняется при перевыпуске ключа.
        existing_labels = await self._active_labels(chat_id)
        explicit = None if forced_label else self._explicit_label(args)
        device_label = forced_label or explicit or _random_device_label(existing_labels)
        now = _now().isoformat()

        self._sub_cache.clear()
        async with self._issue_lock:
            if explicit is not None and await self._active_row(chat_id, explicit, transport):
                # Не дубль и не тихий перевыпуск: перевыпуск — отдельное явное
                # действие (reissue по (label, transport)).
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"устройство «{explicit}» ({transport}) уже выдано — "
                    "для нового ключа используйте перевыпуск",
                )
            artifacts = await self._issue_locked(chat_id, device_label, transport, now)

        await self._emit(
            EVENT_VPN_PEER_ISSUED,
            {
                "chat_id": chat_id,
                "device_label": device_label,
                "transport": transport,
                **self._where(),
            },
        )
        return {
            **artifacts,
            "transport": transport,
            "device_label": device_label,
            # Откуда конфиг — бот ставит страну в имя файла, чтобы гость с
            # несколькими серверами не путал, какой откуда.
            "location": self._cfg.location,
            # Число устройств чата ДО этой выдачи — bot/handlers/vpn.py и
            # bot/tools.py::tool_vpn выбирают по нему, что показать первым
            # (решение пользователя 2026-08-04): 0 — это первое устройство
            # чата, скорее всего настраивается прямо с этого же телефона →
            # удобнее файл; иначе — вероятно, для ДРУГОГО устройства → QR.
            "prior_device_count": len(existing_labels),
        }

    def _reality_artifacts(self, client_uuid: str, device_label: str) -> dict[str, Any]:
        """Конфиг/ссылка/QR reality-устройства из хранимого UUID — общее для
        issue и get_vless (ссылку можно отдать снова без перевыпуска)."""
        assert self._reality_cfg is not None
        # sing-box-конфиг несёт все правила маршрутизации (Hiddify),
        # vless://-ссылка — только быстрый импорт/QR. self._reality_cfg
        # (RealityTransportConfig) несёт поля с теми же именами, что
        # client_config.RealityParams — render_* берут его по duck-typing.
        config_text = render_singbox_config(self._reality_cfg, client_uuid)
        # Имя профиля в Hiddify — то, что видит гость в списке подключений.
        # Со страной, иначе два сервера в списке не отличить друг от друга.
        flag = country_flag(self._cfg.location)
        profile_label = f"{flag} {device_label}" if flag else device_label
        share_url = render_vless_url(self._reality_cfg, client_uuid, profile_label)
        return {
            "config_text": config_text,
            "share_url": share_url,
            "deep_link": render_deep_link(share_url),
            "qr_png_b64": _render_qr_png_b64(share_url),
        }

    async def _issue_locked(
        self, chat_id: int, device_label: str, transport: str, now: str
    ) -> dict[str, Any]:
        if transport == TRANSPORT_AWG:
            private_key, public_key = await self._backend.generate_keypair()
            address = _allocate_address(self._cfg.subnet, await self._active_addresses())
            server_pub = await self._server_public_key()
            await self._db.conn.execute(
                "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
                "status, created_at, server, server_pubkey) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    chat_id,
                    device_label,
                    TRANSPORT_AWG,
                    public_key,
                    address,
                    now,
                    self._node,
                    server_pub,
                ),
            )
            await self._db.conn.commit()
            await self._backend.add_peer(public_key, address)
            conf = _render_client_conf(self._cfg, private_key, address, server_pub)
            artifacts: dict[str, Any] = {
                "config_text": conf,
                "qr_png_b64": _render_qr_png_b64(conf),
                "address": address,
            }
        else:  # TRANSPORT_REALITY
            assert self._reality is not None and self._reality_cfg is not None
            client_uuid = str(uuidlib.uuid4())
            email = _reality_email(chat_id, device_label)
            # server_pubkey у reality — публичный ключ Reality-сервера на момент
            # выдачи (57.1): по нему видно «сервер переустановлен». У старых
            # reality-пиров NULL — их по ключу не судим.
            await self._db.conn.execute(
                "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
                "status, created_at, server, server_pubkey) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    chat_id,
                    device_label,
                    TRANSPORT_REALITY,
                    client_uuid,
                    email,
                    now,
                    self._node,
                    self._reality_cfg.server_public_key or None,
                ),
            )
            await self._db.conn.commit()
            await self._reality.add_client(client_uuid, email, self._reality_cfg.flow)
            artifacts = self._reality_artifacts(client_uuid, device_label)
        return artifacts

    async def _remove_from_backend(self, transport: str, public_key: str, address: str) -> None:
        """Снять пир с сервера нужным транспортом: awg — по pubkey, reality —
        по email (в колонке address)."""
        if transport == TRANSPORT_REALITY:
            if self._reality is not None:
                await self._reality.remove_client(address)
        else:
            await self._backend.remove_peer(public_key)

    async def _reissue(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        if not device_label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        # Проверка здесь, а не только в _issue: перевыпуск снимает старый пир
        # ДО выдачи нового, и недопущенный остался бы вообще без устройства.
        self._sub_cache.clear()
        await self._require_access(chat_id)
        # transport (57.1) адресует конкретное подключение устройства; без него —
        # старое поведение (старый бот его не шлёт).
        wanted = self._optional_transport(args)
        row = await self._active_row(chat_id, device_label, wanted)
        transport = row["transport"] or TRANSPORT_AWG if row is not None else None
        if row is not None:
            await self._db.conn.execute(
                "UPDATE vpn_peers SET status = 'expired', revoked_at = ? WHERE public_key = ?",
                (_now().isoformat(), row["public_key"]),
            )
            await self._db.conn.commit()
            await self._remove_from_backend(transport, row["public_key"], row["address"])
        # Перевыпуск сохраняет транспорт устройства (если пир нашёлся); нового
        # пира без исходного — как обычный issue (транспорт из args/дефолт).
        try:
            return await self._issue(args, forced_label=device_label, forced_transport=transport)
        finally:
            self._sub_cache.clear()

    async def _revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sub_cache.clear()
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        row = await self._active_row(chat_id, device_label, self._optional_transport(args))
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет активного устройства «{device_label}»")
        await self._db.conn.execute(
            "UPDATE vpn_peers SET status = 'revoked', revoked_at = ? WHERE public_key = ?",
            (_now().isoformat(), row["public_key"]),
        )
        await self._db.conn.commit()
        await self._remove_from_backend(
            row["transport"] or TRANSPORT_AWG, row["public_key"], row["address"]
        )
        return {
            "revoked": True,
            "device_label": device_label,
            "transport": row["transport"] or TRANSPORT_AWG,
        }

    async def _get_vless(self, args: dict[str, Any]) -> dict[str, Any]:
        """Ссылка/sing-box конфиг/QR VLESS существующего устройства без
        перевыпуска: UUID клиента лежит в vpn_peers.public_key."""
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        if not device_label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        if self._reality is None or self._reality_cfg is None:
            raise ProtoError(ERR_BAD_REQUEST, f"на сервере {self._node} нет VLESS")
        await self._require_access(chat_id)
        row = await self._active_row(chat_id, device_label, TRANSPORT_REALITY)
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"у «{device_label}» нет активного VLESS-подключения")
        return {
            **self._reality_artifacts(row["public_key"], device_label),
            "transport": TRANSPORT_REALITY,
            "device_label": device_label,
            "location": self._cfg.location,
        }

    # --- подписка Hiddify (57.10) ---

    async def _get_subscription(self, args: dict[str, Any]) -> dict[str, Any]:
        """Адреса страницы и подписки устройства на ЭТОЙ ноде. Токен из них
        годится на любой vpn-ноде; адрес привязан к ноде, ответившей боту."""
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        if not device_label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        if self._reality is None or self._reality_cfg is None:
            raise ProtoError(ERR_BAD_REQUEST, f"на сервере {self._node} нет VLESS")
        if self.sub_web is None or not self.sub_web.listening:
            raise ProtoError(ERR_BAD_REQUEST, f"страница подписки на {self._node} не запущена")
        await self._require_access(chat_id)
        row = await self._active_row(chat_id, device_label, TRANSPORT_REALITY)
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"у «{device_label}» нет активного VLESS-подключения")
        gen = await self._device_gen(chat_id, device_label)
        for peer_gen in await self._peer_gens(chat_id, device_label):
            gen = max(gen, peer_gen)
        token = subs.make_token(self._sub_secret, chat_id, device_label, gen)
        return {
            "page_url": self.sub_web.page_url(token),
            "sub_url": self.sub_web.sub_url(token),
            "singbox_url": self.sub_web.sub_url(token) + "?format=singbox",
            "device_label": device_label,
            "node": self._node_id,
            "https": bool(self.sub_web.tls),
        }

    def _reality_params(self) -> RealityParams:
        assert self._reality_cfg is not None
        cfg = self._reality_cfg
        return RealityParams(
            endpoint_host=cfg.endpoint_host,
            port=cfg.port,
            server_public_key=cfg.server_public_key,
            short_id=cfg.short_id,
            sni=cfg.sni,
            flow=cfg.flow,
        )

    def _sub_entry_name(self) -> str:
        return self._cfg.location or self._node_id

    async def _device_gen(self, chat_id: int, device_label: str) -> int:
        """«Поколение» токена на ЭТОЙ ноде: unix-время последнего перевыпуска или
        отзыва VLESS-ключа устройства (``revoked_at`` снятых строк vpn_peers).
        Выпуск новой страны ничего не меняет — ссылка на телефоне остаётся живой.
        Живёт в vpn_peers, поэтому переживает переустановку ноды из бэкапа."""
        cur = await self._db.conn.execute(
            "SELECT revoked_at FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
            "AND COALESCE(transport, 'awg') = ? AND status != 'active' AND revoked_at IS NOT NULL",
            (chat_id, device_label, TRANSPORT_REALITY),
        )
        best = 0
        for row in await cur.fetchall():
            try:
                best = max(best, int(datetime.fromisoformat(row["revoked_at"]).timestamp()))
            except ValueError:
                continue
        return best

    async def _peer_gens(self, chat_id: int, device_label: str) -> list[int]:
        """Поколения токена на соседних vpn-нодах (недоступные пропускаем)."""
        if self._node_link is None:
            return []
        out: list[int] = []
        for node in await self._sub_peer_nodes():
            try:
                reply = await self._node_link.command(
                    ACTION_SUB_GEN,
                    {"chat_id": chat_id, "device_label": device_label},
                    dst=Address(node=node, service=SERVICE_NAME),
                    timeout=6.0,
                )
                out.append(int(reply.get("gen") or 0))
            except (ServiceUnavailableError, ProtoError, TimeoutError, OSError, ValueError):
                continue
        return out

    async def _sub_local(self, token: str) -> tuple[int, str, str] | None:
        """``(chat_id, label, uuid)`` активного VLESS-ключа ЭТОЙ ноды с таким
        токеном: MAC пересчитывается по каждому ключу (их единицы), без хранилища."""
        if self._reality is None or self._reality_cfg is None:
            return None
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, public_key FROM vpn_peers "
            "WHERE status = 'active' AND COALESCE(transport, 'awg') = ? AND chat_id != ?",
            (TRANSPORT_REALITY, NODE_SENTINEL_CHAT_ID),
        )
        for row in await cur.fetchall():
            if subs.token_matches(self._sub_secret, token, row["chat_id"], row["device_label"]):
                allowed, _base = await self._access(row["chat_id"])
                if not allowed:
                    return None
                return row["chat_id"], row["device_label"], row["public_key"]
        return None

    def _node_ip(self) -> str:
        if self._reality_cfg is not None and self._reality_cfg.endpoint_host:
            return self._reality_cfg.endpoint_host
        return self._cfg.endpoint_host

    def _sub_base(self) -> str:
        if self._cfg.sub_port <= 0 or self._reality_cfg is None:
            return ""
        return f"https://{sub_public_host(self._cfg)}:{self._cfg.sub_port}"

    async def _sub_health(self) -> str:
        """«ok» / «bad» / «» — проверки VLESS этой страны (иначе любого транспорта)."""
        rows = [r for r in await self._check_rollup(server=self._node) if r.get("status")]
        mine = [r for r in rows if r.get("transport") == TRANSPORT_REALITY] or rows
        if not mine:
            return ""
        return "ok" if all(r["status"] == CHECK_OK for r in mine) else "bad"

    async def _sub_links(self, args: dict[str, Any]) -> dict[str, Any]:
        """Служебное между vpn-нодами: есть ли здесь VLESS устройства с этим
        токеном; если есть — сервер, UUID, расход и лимит гостя за месяц, поколение
        токена и сведения о ноде для страницы (адрес выхода, здоровье, AmneziaWG)."""
        token = str(args.get("token") or "")
        if not subs.valid_token_shape(token):
            return {"found": False}
        base = {
            "node": self._node_id,
            "name": self._sub_entry_name(),
            "ip": self._node_ip(),
            "awg": self._has(TRANSPORT_AWG),
            # Адрес страницы этой ноды: по нему браузер спрашивает «трафик идёт через вас?».
            "base": self._sub_base(),
        }
        found = await self._sub_local(token)
        if found is None:
            return {"found": False, "info": base}
        chat_id, label, client_uuid = found
        month = _month_key(_now())
        entry = subs.SubEntry(
            node=self._node_id,
            name=self._sub_entry_name(),
            uuid=client_uuid,
            params=self._reality_params(),
        )
        awg_key = await self._active_row(chat_id, label, TRANSPORT_AWG) is not None
        return {
            "found": True,
            "device_label": label,
            "chat_id": chat_id,
            "gen": await self._device_gen(chat_id, label),
            "entry": entry.to_wire(),
            "info": {**base, "health": await self._sub_health(), "awg_key": awg_key},
            "used_bytes": await self._used_bytes(chat_id, month),
            "limit_bytes": await self._limit_bytes(chat_id, month),
        }

    async def _sub_peer_nodes(self) -> list[str]:
        """Остальные живые vpn-ноды роя (кэш на минуту)."""
        if self._node_link is None:
            return []
        now = time.monotonic()
        if self._peer_nodes_cache and now - self._peer_nodes_cache[0] < 60:
            return self._peer_nodes_cache[1]
        from sa_home_bot.bot import vpn_nodes

        nodes = [n for n in await vpn_nodes.live_vpn_nodes(self._node_link) if n != self._node_id]
        self._peer_nodes_cache = (now, nodes)
        return nodes

    async def _sub_ask_peer(self, node: str, token: str) -> dict[str, Any] | None:
        """Ответ соседа или ``None``, если он недоступен (тогда берём старое
        из ``_sub_stale``: страна не должна пропадать из подписки на время сбоя)."""
        assert self._node_link is not None
        try:
            return await self._node_link.command(
                ACTION_SUB_LINKS,
                {"token": token},
                dst=Address(node=node, service=SERVICE_NAME),
                timeout=6.0,
            )
        except ServiceUnavailableError:
            return None
        except (ProtoError, TimeoutError, OSError):
            return {"found": False}

    async def resolve_subscription(self, token: str) -> subs.Subscription | None:
        """Подписка по токену со всех нод; ``None`` — токен неизвестен или
        устройство отозвано везде (веб отвечает 404)."""
        if not subs.valid_token_shape(token):
            return None
        now = time.monotonic()
        cached = self._sub_cache.get(token)
        if cached is not None and now - cached[0] < (20 if cached[1] else 30):
            return cached[1]
        entries: list[subs.SubEntry] = []
        nodes: list[subs.NodeInfo] = []
        label = ""
        chat_id = 0
        newest_gen = 0
        used = total = 0
        local = await self._sub_links({"token": token})
        answers: list[tuple[str, dict[str, Any] | None]] = [(self._node_id, local)]
        peers = await self._sub_peer_nodes()
        if peers:
            replies = await asyncio.gather(*(self._sub_ask_peer(n, token) for n in peers))
            answers += list(zip(peers, replies, strict=True))
        for node, reply in answers:
            if reply is None:  # нода не ответила: держим последнюю известную страну
                entries += self._sub_stale.get((token, node), [])
                continue
            try:
                if reply.get("info"):
                    info = subs.NodeInfo.from_wire(reply["info"], local=node == self._node_id)
                    nodes.append(info)
            except (KeyError, TypeError, ValueError):
                pass
            if not reply.get("found"):
                self._sub_stale.pop((token, node), None)
                continue
            try:
                entry = subs.SubEntry.from_wire(reply["entry"])
            except (KeyError, TypeError, ValueError):
                continue
            entries.append(entry)
            self._sub_stale[(token, node)] = [entry]
            label = label or str(reply.get("device_label") or "")
            chat_id = chat_id or int(reply.get("chat_id") or 0)
            newest_gen = max(newest_gen, int(reply.get("gen") or 0))
            used += int(reply.get("used_bytes") or 0)
            total += int(reply.get("limit_bytes") or 0)
        result: subs.Subscription | None = None
        # Токен старого поколения (VLESS перевыпускали на любой ноде) — как неизвестный.
        if entries and (subs.token_gen(token) or 0) >= newest_gen:
            if not label:
                label = "устройство"
            nxt = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            nxt = (nxt + timedelta(days=32)).replace(day=1)
            result = subs.Subscription(
                device_label=label,
                entries=tuple(subs.sort_entries(entries)),
                used_bytes=used,
                total_bytes=total,
                expire_ts=int(nxt.timestamp()),
                chat_id=chat_id,
                nodes=tuple(sorted(nodes, key=lambda n: (n.name, n.node))),
            )
        if len(self._sub_cache) > 2000:
            self._sub_cache.clear()
        self._sub_cache[token] = (now, result)
        return result

    async def web_issue_awg(
        self, chat_id: int, device_label: str, node: str, *, replace: bool
    ) -> dict[str, Any]:
        """Выдача AmneziaWG со страницы устройства: выпуск (label+awg) или, если
        ключ в стране уже есть и ``replace``, перевыпуск. Чужая страна — вызовом
        к соседней ноде, как sub_links. Возвращает ответ issue/reissue."""
        action = ACTION_REISSUE if replace else ACTION_ISSUE
        args = {"chat_id": chat_id, "device_label": device_label, "transport": TRANSPORT_AWG}
        self._sub_cache.clear()
        if node == self._node_id:
            return await self.run_command(action, args)
        if self._node_link is None:
            raise ProtoError(ERR_BAD_REQUEST, "нода недоступна")
        return await self._node_link.command(
            action, args, dst=Address(node=node, service=SERVICE_NAME), timeout=20.0
        )

    async def _peers(self, _args: dict[str, Any]) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, transport, address, status, created_at, "
            "last_handshake_at, server FROM vpn_peers ORDER BY chat_id, created_at"
        )
        peers = [
            {
                "chat_id": row["chat_id"],
                "device_label": row["device_label"],
                "transport": row["transport"] or TRANSPORT_AWG,
                # awg — IP в подсети; reality — email клиента xray.
                "address": row["address"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]
        return {"peers": peers}

    # --- квоты ---

    async def _usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = _month_key(_now())
        raw_chat_id = args.get("chat_id")
        if raw_chat_id is not None:
            chat_id = self._chat_id(args)
            used = await self._used_bytes(chat_id, month)
            limit = await self._limit_bytes(chat_id, month)
            state = await self._quota_state(chat_id, month)
            allowed, base_bytes = await self._access(chat_id)
            devices = await self._mark_broken(
                chat_id,
                await self._peers_for_chat(chat_id),
                withheld=state["blocked_at"] is not None or not allowed,
            )
            return {
                "chat_id": chat_id,
                "month": month,
                "node": self._node,
                # Как назвать этот сервер человеку в карточке /vpn, когда их
                # несколько. Пусто — бот покажет голый id ноды.
                "label": self._cfg.location,
                "used_bytes": used,
                "limit_bytes": limit,
                "remaining_bytes": max(limit - used, 0),
                # `blocked` — строго про исчерпанную квоту, `allowed` — про
                # допуск на эту локацию. Две разные причины, и бот показывает
                # их по-разному: недопущенную локацию он просто не рисует.
                "blocked": state["blocked_at"] is not None,
                "allowed": allowed,
                "base_limit_bytes": (
                    self._cfg.base_quota_gb * GB if base_bytes is None else int(base_bytes)
                ),
                "personal_base": base_bytes is not None,
                # `broken` — пира нет на живом сервере (39.0.8(e)); для
                # снятого за квоту/допуск гостя не выставляется.
                "devices": devices,
                # Этап 57.1: трафик месяца по устройствам + подключения
                # (transport, status, last_handshake_at, created_at). Новое
                # поле рядом со старыми — старый бот его не читает.
                "device_usage": await self._device_usage(chat_id, month, devices),
                # Транспорты этой ноды — карточка /vpn по ним решает, показывать
                # ли выбор технологии при «➕ Новое устройство».
                "transports": list(self._transports),
                # Индикатор доступности этого сервера по транспортам
                # (39.0.7(f)): едет вместе с расходом, чтобы карточка /vpn
                # не делала ради него отдельный RPC — она и так фанаутит
                # usage по всем живым серверам. Каждый сервер отчитывается
                # про СЕБЯ: запись в vpn_check_states реплицирована на все
                # живые инстансы, так что своя БД знает, как эту ноду видят
                # чужие наблюдатели.
                "check": await self._check_rollup(server=self._node),
                "proxy_available": bool(self._cfg.mtg_public_host),
            }
        # Сводка для админа. «Резервируют» трафик ноды все допущенные — даже
        # те, кто ещё не завёл ни одного устройства: обещание уже дано. Гость
        # с активными пирами, у которого допуск сняли, тоже должен быть виден
        # (иначе он молча исчезнет из сводки вместе со своим расходом).
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM vpn_peers WHERE status = 'active' "
            "UNION SELECT chat_id FROM vpn_chat_access WHERE allowed = 1"
        )
        active_chat_ids = [row["chat_id"] for row in await cur.fetchall()]
        chats = []
        reserved_bytes = 0
        for cid in active_chat_ids:
            limit = await self._limit_bytes(cid, month)
            allowed, _base = await self._access(cid)
            chats.append(
                {
                    "chat_id": cid,
                    "used_bytes": await self._used_bytes(cid, month),
                    "limit_bytes": limit,
                    "allowed": allowed,
                    "device_count": len(await self._peers_for_chat(cid)),
                }
            )
            reserved_bytes += limit
        node_limit_bytes = self._cfg.node_limit_gb * GB
        return {
            "month": month,
            "chats": chats,
            # "Резерв" — сумма ЛИМИТОВ (не факта потребления) гостей из
            # списка выше: сколько канала занято обещаниями, даже если
            # реально ещё не потрачено — решение пользователя 2026-08-03,
            # чтобы видеть риск перерасхода тарифа ДО того, как он случится.
            "node": {
                "limit_bytes": node_limit_bytes,
                "reserved_bytes": reserved_bytes,
                "free_bytes": max(node_limit_bytes - reserved_bytes, 0),
            },
        }

    async def _set_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        raw_bytes = args.get("bytes")
        if raw_bytes is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан bytes — целевой лимит месяца")
        target = int(raw_bytes)
        month = _month_key(_now())
        current_limit = await self._limit_bytes(chat_id, month)
        delta = target - current_limit
        if delta:
            await self._add_grant(chat_id, month, delta, source="admin")
        await self._check_thresholds(chat_id, month)
        return await self._usage({"chat_id": chat_id})

    async def _set_access(self, args: dict[str, Any]) -> dict[str, Any]:
        """Допуск гостя на ЭТОТ сервер и его постоянная личная база.

        Отличие от ``set_quota``: тот задаёт целевой лимит ТЕКУЩЕГО месяца
        компенсирующим грантом (1-го числа сбрасывается), а ``base_gb`` —
        саму базу, с которой каждый месяц начинается заново.
        """
        chat_id = self._chat_id(args)
        if "allowed" not in args:
            raise ProtoError(ERR_BAD_REQUEST, "не указан allowed — допущен ли гость на сервер")
        allowed = bool(args["allowed"])

        # base_gb не передали — личную базу не трогаем (частый случай «просто
        # открой/закрой доступ»); передали явный null — сбрасываем на общую.
        _was_allowed, base_bytes = await self._access(chat_id)
        if "base_gb" in args:
            raw = args["base_gb"]
            if raw is None:
                base_bytes = None
            else:
                gb = int(raw)
                if gb <= 0:
                    raise ProtoError(
                        ERR_BAD_REQUEST,
                        "base_gb должен быть больше нуля — чтобы закрыть доступ, передайте "
                        "allowed=false (нулевой лимит читался бы как исчерпанная квота и слал "
                        "бы гостю ⛔️-уведомления)",
                    )
                base_bytes = gb * GB

        await self._db.conn.execute(
            "INSERT INTO vpn_chat_access (chat_id, allowed, base_bytes, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET allowed = excluded.allowed, "
            "base_bytes = excluded.base_bytes, updated_at = excluded.updated_at",
            (chat_id, int(allowed), base_bytes, _now().isoformat()),
        )
        await self._db.conn.commit()

        # Сначала пороги: вернувшийся допуск мог застать blocked_at от старой
        # квоты — при поднятом лимите он сам снимется. Затем реконсайл уже на
        # сам переключатель: пиры поднимаются/снимаются сразу, а не через тик
        # сэмплера.
        month = _month_key(_now())
        await self._check_thresholds(chat_id, month)
        await self.reconcile()
        return await self._usage({"chat_id": chat_id})

    async def _grant_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        month = _month_key(_now())
        # Самообслуживание открыто только когда трафик РЕАЛЬНО заканчивается
        # (решение пользователя 2026-08-03) — гость с почти полной квотой не
        # может докупить впрок; порог тот же, что у предупреждения
        # (warn_remaining_gb), чтобы «пришло предупреждение» и «можно
        # докупить» совпадали для гостя по смыслу.
        used = await self._used_bytes(chat_id, month)
        limit = await self._limit_bytes(chat_id, month)
        remaining = limit - used
        threshold = self._cfg.warn_remaining_gb * GB
        if remaining > threshold:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "самообслуживание доступно только когда остаётся меньше "
                f"{self._cfg.warn_remaining_gb} ГБ трафика — сейчас остаётся "
                f"{remaining / GB:.1f} ГБ, попробуйте позже",
            )
        self_granted = await self._granted_bytes(chat_id, month, source="self")
        step = self._cfg.extra_step_gb * GB
        ceiling = self._cfg.self_ceiling_gb * GB
        # База — личная, если владелец её задал: иначе гость с личными 50 ГБ
        # доливал бы себе до потолка, посчитанного от чужих 500.
        base = await self._base_bytes(chat_id)
        if base + self_granted + step > ceiling:
            raise ProtoError(
                ERR_QUOTA_CEILING,
                "достигнут потолок самообслуживания — нужна заявка админу",
            )
        await self._add_grant(chat_id, month, step, source="self")
        await self._check_thresholds(chat_id, month)
        return await self._usage({"chat_id": chat_id})

    async def _request_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        raw_bytes = args.get("bytes")
        bytes_ = int(raw_bytes) if raw_bytes is not None else self._cfg.extra_step_gb * GB
        cur = await self._db.conn.execute(
            "INSERT INTO vpn_requests (chat_id, bytes, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (chat_id, bytes_, _now().isoformat()),
        )
        await self._db.conn.commit()
        request_id = cur.lastrowid
        await self._emit(
            EVENT_VPN_EXTRA_REQUESTED,
            {"request_id": request_id, "chat_id": chat_id, "bytes": bytes_, **self._where()},
        )
        return {"request_id": request_id, "status": "pending"}

    async def _resolve_request(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_id = args.get("request_id")
        if raw_id is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан request_id")
        request_id = int(raw_id)
        approve = bool(args.get("approve"))
        cur = await self._db.conn.execute(
            "SELECT chat_id, bytes, status FROM vpn_requests WHERE id = ?", (request_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет заявки №{request_id}")
        if row["status"] != "pending":
            raise ProtoError(ERR_BAD_REQUEST, f"заявка №{request_id} уже решена")
        status = "approved" if approve else "denied"
        await self._db.conn.execute(
            "UPDATE vpn_requests SET status = ?, decided_at = ? WHERE id = ?",
            (status, _now().isoformat(), request_id),
        )
        await self._db.conn.commit()
        chat_id = row["chat_id"]
        if approve:
            month = _month_key(_now())
            await self._add_grant(
                chat_id, month, row["bytes"], source="admin", request_id=request_id
            )
            await self._check_thresholds(chat_id, month)
        await self._emit(
            EVENT_VPN_EXTRA_RESOLVED,
            {
                "request_id": request_id,
                "chat_id": chat_id,
                "approved": approve,
                "bytes": row["bytes"],
                **self._where(),
            },
        )
        return {"request_id": request_id, "status": status}

    def _where(self) -> dict[str, str]:
        """Откуда событие: бот ставит в текст страну сервера (57.7)."""
        return {"node": self._node, "location": self._cfg.location}

    async def _check_thresholds(self, chat_id: int, month: str) -> None:
        allowed, _base = await self._access(chat_id)
        if not allowed:
            # Недопущенного квота не касается вовсе: трафика он не набирает
            # (реконсайлер не поднимает его пиры), а blocked_at значит
            # «исчерпал», а не «не пущен». Без этого возврата владелец, задав
            # квоту до открытия доступа, слал бы гостю ⛔️ ни за что.
            return
        used = await self._used_bytes(chat_id, month)
        limit = await self._limit_bytes(chat_id, month)
        remaining = limit - used
        state = await self._quota_state(chat_id, month)
        warn_threshold = self._cfg.warn_remaining_gb * GB

        if used >= limit:
            if state["blocked_at"] is None:
                await self._set_quota_state(chat_id, month, blocked_at=_now().isoformat())
                await self.reconcile()
                await self._emit(EVENT_VPN_QUOTA_EXCEEDED, {"chat_id": chat_id, **self._where()})
                await self._emit(EVENT_VPN_PEER_BLOCKED, {"chat_id": chat_id})
            return
        if state["blocked_at"] is not None:
            await self._set_quota_state(chat_id, month, blocked_at=None)
            await self.reconcile()
            await self._emit(EVENT_VPN_ACCESS_RESTORED, {"chat_id": chat_id, **self._where()})
        if remaining <= warn_threshold and state["warned_limit_bytes"] != limit:
            await self._set_quota_state(chat_id, month, warned_limit_bytes=limit)
            await self._emit(
                EVENT_VPN_QUOTA_WARNING,
                {"chat_id": chat_id, "remaining_bytes": max(remaining, 0), **self._where()},
            )

    async def _check_node_limit(self, month: str) -> None:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(used_bytes), 0) AS total FROM vpn_peer_usage WHERE month = ?",
            (month,),
        )
        row = await cur.fetchone()
        total = int(row["total"] or 0)
        limit = self._cfg.node_limit_gb * GB
        threshold = limit - self._cfg.warn_remaining_gb * GB
        if total < threshold:
            return
        state = await self._quota_state(NODE_SENTINEL_CHAT_ID, month)
        if state["warned_limit_bytes"] == limit:
            return
        await self._set_quota_state(NODE_SENTINEL_CHAT_ID, month, warned_limit_bytes=limit)
        await self._emit(
            EVENT_VPN_NODE_QUOTA_WARNING,
            {"used_bytes": total, "limit_bytes": limit, **self._where()},
        )

    # --- прокси (mtg/microsocks на jeeves) ---
    #
    # Общий секрет/ссылка на всех гостей — не per-guest (см. vpn/protocol.py
    # для обоснования). Байты считаются тем же дельта-механизмом, что и у
    # awg-пиров (vpn_counters по псевдо-ключам, vpn_peer_usage по
    # отрицательным peer_id — реальные пиры получают id из AUTOINCREMENT,
    # он всегда положителен, коллизии не будет), и попадают в ТОТ ЖЕ
    # общий счётчик node_limit_gb без единой правки _check_node_limit.

    PROXY_PEER_IDS = {"mtg": -1, "socks": -2}

    async def _proxy_secret(self) -> str:
        cur = await self._db.conn.execute("SELECT mtg_secret FROM proxy_state WHERE id = 1")
        row = await cur.fetchone()
        if row is not None:
            return str(row["mtg_secret"])
        # Первый запуск после деплоя — сидируем тем же бутстрап-секретом,
        # что и node/fixups.py::make_proxy_units_fixup пишет в юнит mtg,
        # если тот ещё не создан (см. PROXY_SECRET_SEED), чтобы оба места
        # сошлись без похода друг к другу.
        await self._set_proxy_secret(PROXY_SECRET_SEED)
        return PROXY_SECRET_SEED

    async def _set_proxy_secret(self, secret: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO proxy_state (id, mtg_secret, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET mtg_secret = excluded.mtg_secret, "
            "updated_at = excluded.updated_at",
            (secret, _now().isoformat()),
        )
        await self._db.conn.commit()

    async def _proxy_link(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.mtg_public_host:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "не настроен [vpn].mtg_public_host — впишите публичный IP/домен jeeves",
            )
        secret = await self._proxy_secret()
        query = f"server={self._cfg.mtg_public_host}&port={self._cfg.mtg_port}&secret={secret}"
        tg_link = f"tg://proxy?{query}"
        return {
            "tg_link": tg_link,
            "t_me_link": f"https://t.me/proxy?{query}",
            "qr_png_b64": _render_qr_png_b64(tg_link),
            "host": self._cfg.mtg_public_host,
            "port": self._cfg.mtg_port,
            "secret": secret,
            "socks_host": self._cfg.socks_host,
            "socks_port": self._cfg.socks_port,
            # Нода/метка локации — чтобы вызывающий (fanout по нескольким
            # серверам, bot/vpn_nodes.py) мог подписать, чей это прокси:
            # секрет у каждой ноды свой (своя vpn.sqlite:proxy_state).
            "node": self._node,
            "label": self._cfg.location,
        }

    async def _proxy_rotate_secret(self, args: dict[str, Any]) -> dict[str, Any]:
        new_secret = await self._proxy_backend.generate_secret(self._cfg.mtg_domain)
        await self._proxy_backend.rotate_secret(new_secret)
        await self._set_proxy_secret(new_secret)
        return await self._proxy_link(args)

    async def _proxy_usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = _month_key(_now())
        used: dict[str, int] = {}
        for key, peer_id in self.PROXY_PEER_IDS.items():
            cur = await self._db.conn.execute(
                "SELECT used_bytes FROM vpn_peer_usage WHERE peer_id = ? AND month = ?",
                (peer_id, month),
            )
            row = await cur.fetchone()
            used[key] = int(row["used_bytes"]) if row else 0
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(used_bytes), 0) AS total FROM vpn_peer_usage WHERE month = ?",
            (month,),
        )
        row = await cur.fetchone()
        return {
            "mtg_bytes": used["mtg"],
            "socks_bytes": used["socks"],
            "node_used_bytes": int(row["total"] or 0),
            "node_limit_bytes": self._cfg.node_limit_gb * GB,
        }

    async def _sample_proxy(self, month: str) -> None:
        try:
            counters = await self._proxy_backend.counters()
        except ProtoError:
            # sudoers ещё не поставлен (nodectl fix не прогнан) — не рушим
            # остальной тик сэмплера ради этого, просто пропускаем прокси.
            return
        now_iso = _now().isoformat()
        for key, total in counters.items():
            pubkey = f"proxy-{key}"
            peer_id = self.PROXY_PEER_IDS[key]
            cur = await self._db.conn.execute(
                "SELECT last_rx FROM vpn_counters WHERE public_key = ?", (pubkey,)
            )
            prev = await cur.fetchone()
            delta = total if prev is None else max(total - prev["last_rx"], 0)
            if delta:
                await self._db.conn.execute(
                    "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (?, ?, ?) "
                    "ON CONFLICT(peer_id, month) DO UPDATE SET "
                    "used_bytes = used_bytes + excluded.used_bytes",
                    (peer_id, month, delta),
                )
            await self._db.conn.execute(
                "INSERT INTO vpn_counters (public_key, last_rx, last_tx, updated_at) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "last_rx = excluded.last_rx, updated_at = excluded.updated_at",
                (pubkey, total, now_iso),
            )
        await self._db.conn.commit()

    # --- сэмплер ---

    async def sample_once(self) -> None:
        now = _now()
        month = _month_key(now)
        # awg: (rx, tx) с момента поднятия интерфейса + unix-ts хендшейков.
        transfer = await self._backend.transfer() if self._has(TRANSPORT_AWG) else {}
        handshakes = await self._backend.latest_handshakes() if self._has(TRANSPORT_AWG) else {}
        # reality: email → (uplink, downlink) с момента старта xray.
        reality_stats = await self._reality.stats() if self._reality is not None else {}

        cur = await self._db.conn.execute(
            "SELECT id, chat_id, transport, public_key, address FROM vpn_peers "
            "WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        touched_chats: set[int] = set()
        for row in rows:
            transport = row["transport"] or TRANSPORT_AWG
            # counter_key — по чему ведём вчерашний срез в vpn_counters:
            # для обоих транспортов это public_key (awg-pubkey либо xray-UUID).
            counter_key = row["public_key"]
            handshake_ts = 0
            if transport == TRANSPORT_AWG:
                if row["public_key"] not in transfer:
                    continue
                a, b = transfer[row["public_key"]]  # rx, tx
                handshake_ts = handshakes.get(row["public_key"], 0)
            else:  # reality — ключ статистики xray это email (колонка address)
                if row["address"] not in reality_stats:
                    continue
                a, b = reality_stats[row["address"]]  # uplink, downlink
            total = a + b
            cur2 = await self._db.conn.execute(
                "SELECT last_rx, last_tx FROM vpn_counters WHERE public_key = ?", (counter_key,)
            )
            prev = await cur2.fetchone()
            if prev is None:
                # Первое наблюдение этого пира: он либо только что выдан
                # (счётчики сервера стартуют с нуля), либо это первый тик
                # сэмплера после его появления — в обоих случаях база 0.
                delta = total
            else:
                prev_total = prev["last_rx"] + prev["last_tx"]
                delta = total - prev_total if total >= prev_total else total
            if delta:
                await self._db.conn.execute(
                    "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (?, ?, ?) "
                    "ON CONFLICT(peer_id, month) DO UPDATE SET "
                    "used_bytes = used_bytes + excluded.used_bytes",
                    (row["id"], month, delta),
                )
                touched_chats.add(row["chat_id"])
                if transport == TRANSPORT_REALITY:
                    # У reality нет отдельного «хендшейка» — «было на связи»
                    # ведём по факту прироста трафика (как reality/service.py).
                    await self._db.conn.execute(
                        "UPDATE vpn_peers SET last_handshake_at = ? WHERE id = ?",
                        (now.isoformat(), row["id"]),
                    )
            await self._db.conn.execute(
                "INSERT INTO vpn_counters (public_key, last_rx, last_tx, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "last_rx = excluded.last_rx, last_tx = excluded.last_tx, "
                "updated_at = excluded.updated_at",
                (counter_key, a, b, now.isoformat()),
            )
            if handshake_ts:
                await self._db.conn.execute(
                    "UPDATE vpn_peers SET last_handshake_at = ? WHERE id = ?",
                    (datetime.fromtimestamp(handshake_ts, tz=UTC).isoformat(), row["id"]),
                )
        await self._db.conn.commit()
        if self._cfg.mtg_public_host:
            await self._sample_proxy(month)
        for chat_id in touched_chats:
            await self._check_thresholds(chat_id, month)
        await self._check_node_limit(month)
        await self.reconcile()

    async def usage_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.sample_interval_s)
            try:
                await self.sample_once()
            except Exception:  # noqa: BLE001 — сбой одного тика не должен ронять цикл
                log.exception("vpn: сбой сэмплера трафика")

    # --- мониторинг доступности (vpn_check) ---
    #
    # Pull/push, а не синхронный сбор ответов (решение пользователя
    # 2026-08-17): эта служба только РАССЫЛАЕТ команду "проверь" через
    # generic fan-out node-сервиса (node/service.py::ACTION_TRIGGER_PEERS)
    # и не ждёт результатов синхронно — каждая vpn_check-служба на своей
    # ноде сама, отдельным вызовом, пушит результат обратно сюда
    # (ACTION_REPORT_CHECK). Мут повторных алертов даёт не отдельный
    # флаг, а сама гистерезис-функция domain/vpn_check.py::reconcile_vpn_check
    # — событие эмитится только на переходе статуса, не на каждый неуспешный
    # тик (см. её докстринг).

    async def _dispatch_checks(self) -> dict[str, Any]:
        # 39.0.7 (2026-09-18): каждый инстанс vpn просит проверить ТОЛЬКО
        # себя (``server=self._node``), не общий плоский список целей на
        # всю матрицу серверов — иначе при N живых серверах дублируется
        # диспетчеризация N раз за интервал (было так, пока сервер был
        # один — не бросалось в глаза). Заодно это даёт бесплатный
        # heartbeat: если этот инстанс vpn целиком упал, его check_loop не
        # бежит, и «проверьте меня» просто не уходит — лишних попыток
        # дёргать мёртвую цель нет. Смерть хоста как таковая не теряется:
        # её отдельно и мгновенно видно через live_vpn_nodes()/monitor —
        # vpn_check отвечает за более узкий вопрос («сервер жив на уровне
        # роя, но реально ли через него идёт трафик именно этого
        # транспорта именно оттуда»), не за то же самое дважды.
        if self._node_link is None:
            log.warning("vpn: node_link не настроен — проверки доступности не разосланы")
            return {"dispatched_to": [], "unreachable": [], "skipped": []}
        try:
            result = await self._node_link.command(
                ACTION_TRIGGER_PEERS,
                {
                    "service": vpn_check_protocol.SERVICE_NAME,
                    "action": vpn_check_protocol.ACTION_CHECK,
                    "args": {"server": self._node, "targets": list(self._cfg.check_targets)},
                    "timeout_s": self._cfg.check_dispatch_timeout_s,
                },
                dst=Address(node=self._node, service=NODE_SERVICE),
                timeout=self._cfg.check_dispatch_timeout_s + 3.0,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
            log.warning("vpn: не удалось разослать проверки доступности: %s", exc)
            return {"dispatched_to": [], "unreachable": [], "skipped": [], "error": str(exc)}
        return {
            "dispatched_to": result.get("dispatched", []),
            "unreachable": result.get("unreachable", []),
            "skipped": result.get("skipped", []),
        }

    async def check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.check_interval_s)
            try:
                await self._dispatch_checks()
            except Exception:  # noqa: BLE001 — сбой одного тика не должен ронять цикл
                log.exception("vpn: сбой цикла проверок доступности")

    async def _report_check(self, args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node", "")).strip()
        results = args.get("results")
        if not node or not isinstance(results, list) or not results:
            raise ProtoError(ERR_BAD_REQUEST, "нужны node и непустой список results")
        now = _now()
        # Прогон списка сайтов через один туннель (сервер + транспорт) — одно
        # событие на вид (упал/поднялся) со списком сайтов, а не событие на
        # каждый сайт: общий сбой туннеля раньше сыпал сообщением на сайт.
        runs: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in results:
            if not isinstance(raw, dict):
                continue
            server = str(raw.get("server", "")).strip()
            transport = str(raw.get("transport", "")).strip()
            target = raw.get("target")
            if not server or not transport or not target:
                continue
            ms_raw = raw.get("ms")
            ms = int(ms_raw) if isinstance(ms_raw, int | float) else None
            error_raw = raw.get("error")
            ok = bool(raw.get("ok"))
            error = str(error_raw) if error_raw else None
            run = runs.setdefault(
                (server, transport), {"failed": [], "recovered": [], "total": 0, "bad": 0}
            )
            run["total"] += 1
            run["bad"] += not ok
            to_status = await self._apply_check_result(
                node, server, transport, str(target), ok, ms, error, now
            )
            if to_status == CHECK_ALERTING:
                run["failed"].append({"target": str(target), "error": error})
            elif to_status == CHECK_OK:
                run["recovered"].append(str(target))
        for (server, transport), run in runs.items():
            base = {"node": node, "server": server, "transport": transport, "total": run["total"]}
            if run["failed"]:
                await self._emit(
                    EVENT_VPN_CHECK_FAILED,
                    {
                        **base,
                        "targets": run["failed"],
                        "all_failed": run["bad"] == run["total"],
                        "consecutive": self._cfg.check_fail_threshold,
                    },
                )
            if run["recovered"]:
                await self._emit(
                    EVENT_VPN_CHECK_RECOVERED,
                    {**base, "targets": run["recovered"], "all_ok": run["bad"] == 0},
                )
        # Пробник сам фанаутит report_check на все живые vpn-инстансы (не
        # только на одну ноду через resolve_vpn_dst) — см.
        # vpn_check/service.py::_run_and_report. Эта служба здесь просто
        # применяет то, что до неё долетело; повторного вещания дальше не
        # делает (иначе легко словить каскад). Из-за этого каждый инстанс
        # прогоняет reconcile_vpn_check по СВОЕЙ копии данных, независимо —
        # если один инстанс на миг пропустил отчёт (был недоступен),
        # его гистерезис-счётчик может на 1-2 тика разойтись с другими;
        # самовыравнивается за пару циклов. Ценой этого может задвоиться
        # событие EVENT_VPN_CHECK_FAILED/RECOVERED (два инстанса перейдут
        # порог не в один и тот же тик) — принято осознанно: дешевле, чем
        # городить координацию «кто один имеет право эмитить».
        return {"accepted": True}

    async def _apply_check_result(
        self,
        node: str,
        server: str,
        transport: str,
        target: str,
        ok: bool,
        latency_ms: int | None,
        error: str | None,
        now: datetime,
    ) -> str | None:
        """Записать результат; вернуть статус, в который строка ПЕРЕШЛА
        (``None`` — перехода не было). Событие шлёт вызывающий, пачкой."""
        cur = await self._db.conn.execute(
            "SELECT status, consecutive_count, alerting_since, first_seen_at, "
            "notified_alert_at, notified_cleared_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (node, server, transport, target),
        )
        row = await cur.fetchone()
        known = None
        notified_alert_at = None
        notified_cleared_at = None
        first_seen_at = now.isoformat()
        if row is not None:
            alerting_since = (
                datetime.fromisoformat(row["alerting_since"]) if row["alerting_since"] else None
            )
            known = KnownCheckState(
                status=row["status"],
                consecutive_count=row["consecutive_count"],
                alerting_since=alerting_since,
            )
            notified_alert_at = row["notified_alert_at"]
            notified_cleared_at = row["notified_cleared_at"]
            first_seen_at = row["first_seen_at"]

        result = CheckResult(
            node=node,
            server=server,
            transport=transport,
            target=target,
            ok=ok,
            latency_ms=latency_ms,
            error=error,
        )
        state, transition = reconcile_vpn_check(
            result,
            known,
            now,
            fail_threshold=self._cfg.check_fail_threshold,
            clear_threshold=self._cfg.check_clear_threshold,
        )
        if transition is not None and transition.to_status == CHECK_ALERTING:
            notified_alert_at, notified_cleared_at = now.isoformat(), None
        elif transition is not None and transition.to_status == CHECK_OK:
            notified_cleared_at = now.isoformat()

        await self._db.conn.execute(
            "INSERT INTO vpn_check_states ("
            "node, server, transport, target, status, last_ok, last_latency_ms, last_error, "
            "consecutive_count, alerting_since, first_seen_at, last_seen_at, "
            "notified_alert_at, notified_cleared_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(node, server, transport, target) DO UPDATE SET "
            "status = excluded.status, last_ok = excluded.last_ok, "
            "last_latency_ms = excluded.last_latency_ms, last_error = excluded.last_error, "
            "consecutive_count = excluded.consecutive_count, "
            "alerting_since = excluded.alerting_since, last_seen_at = excluded.last_seen_at, "
            "notified_alert_at = excluded.notified_alert_at, "
            "notified_cleared_at = excluded.notified_cleared_at",
            (
                node,
                server,
                transport,
                target,
                state.status,
                int(state.last_ok),
                state.last_latency_ms,
                state.last_error,
                state.consecutive_count,
                state.alerting_since.isoformat() if state.alerting_since else None,
                first_seen_at,
                now.isoformat(),
                notified_alert_at,
                notified_cleared_at,
            ),
        )
        await self._db.conn.commit()
        return transition.to_status if transition is not None else None

    async def _check_status(self) -> dict[str, Any]:
        # Не фанаутим на чтение: запись уже фанаутится на все живые
        # vpn-инстансы (vpn_check/service.py::_run_and_report), так что
        # любой живой инстанс, включая этот, уже держит (почти) полную
        # картину сам по себе — второй фанаут на чтение был бы двойной
        # работой ради того же результата. См. 39.0.7 в IMPLEMENTATION_PLAN.md.
        cur = await self._db.conn.execute(
            "SELECT node, server, transport, target, status, last_ok, last_latency_ms, "
            "last_error, consecutive_count, alerting_since, last_seen_at FROM vpn_check_states "
            "ORDER BY server, transport, node, target"
        )
        rows = await cur.fetchall()
        states = [
            {
                "node": r["node"],
                "server": r["server"],
                "transport": r["transport"],
                "target": r["target"],
                "status": r["status"],
                "last_ok": bool(r["last_ok"]),
                "last_latency_ms": r["last_latency_ms"],
                "last_error": r["last_error"],
                "consecutive_count": r["consecutive_count"],
                "alerting_since": r["alerting_since"],
                "last_seen_at": r["last_seen_at"],
            }
            for r in rows
        ]
        return {"states": states, "rollup": await self._check_rollup()}

    async def _telegram_egress(self, args: dict[str, Any]) -> dict[str, Any]:
        """Кандидат на маршрут до Telegram Bot API для бота на ноде ``observer``
        (этап 52): SOCKS5-адрес этой ноды и последняя проба api.telegram.org
        через её reality-VLESS, снятая этим наблюдателем (``vpn_check``).

        Только чтение, без QR. Протухшая по тому же правилу, что и в
        ``_check_rollup``, строка не прячется, а помечается ``stale`` — решать,
        что «неизвестно», будет бот. Нет строки — ``check`` = None.
        """
        observer = str(args.get("observer") or "").strip()
        if not observer:
            raise ProtoError(ERR_BAD_REQUEST, "не передан observer")
        cur = await self._db.conn.execute(
            "SELECT last_ok, last_latency_ms, last_seen_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (observer, self._node, TELEGRAM_EGRESS_TRANSPORT, TELEGRAM_EGRESS_TARGET),
        )
        row = await cur.fetchone()
        check: dict[str, Any] | None = None
        if row is not None:
            stale_before = (
                _now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
            ).isoformat()
            check = {
                "ok": bool(row["last_ok"]),
                "ms": row["last_latency_ms"],
                "seen_at": row["last_seen_at"],
                "stale": row["last_seen_at"] < stale_before,
            }
        socks_host = self._cfg.socks_host
        return {
            "node": self._node,
            "label": self._cfg.location,
            "socks": f"{socks_host}:{self._cfg.socks_port}" if socks_host else None,
            "check": check,
        }

    async def _check_rollup(self, *, server: str | None = None) -> list[dict[str, Any]]:
        """Сводка «как эти (сервер, транспорт) видят наблюдатели» — для
        индикатора в /vpn (39.0.7(f)).

        ``server`` сужает выборку до одной ноды: карточка спрашивает у
        каждого живого сервера про него самого (поле ``check`` в ответах
        ``usage``/``get_state``), и лишние чужие пары ей там ни к чему.

        Протухшие строки отбрасываются (``CHECK_STALE_FACTOR``), поэтому
        пара, про которую давно никто не отчитывался, из сводки просто
        исчезает — бот покажет её без индикатора, а не выдуманным цветом.
        """
        stale_before = (
            _now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
        ).isoformat()
        sql = "SELECT node, server, transport, status FROM vpn_check_states WHERE last_seen_at >= ?"
        params: list[Any] = [stale_before]
        if server is not None:
            sql += " AND server = ?"
            params.append(server)
        cur = await self._db.conn.execute(sql, params)
        by_pair: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in await cur.fetchall():
            by_pair.setdefault((row["server"], row["transport"]), []).append(
                (row["node"], row["status"])
            )
        return [
            {
                "server": pair_server,
                "transport": transport,
                # Цвет — по ВСЕМ строкам пары: недостижимая цель у одного
                # наблюдателя уже делает картину неоднородной (partial), даже
                # если вторая цель у него же отвечает.
                "status": rollup_status([status for _node, status in rows]),
                # А вот считаем именно НАБЛЮДАТЕЛЕЙ, а не строки: у каждого их
                # столько, сколько целей в check_targets, и «observers: 4» при
                # двух живых наблюдателях (живая находка на деплое 0.109.0)
                # вводит в заблуждение кого угодно, включая будущего себя.
                "observers": len({node for node, _status in rows}),
            }
            for (pair_server, transport), rows in sorted(by_pair.items())
        ]

    # --- APK ---

    async def _apk_row(self) -> dict[str, Any] | None:
        cur = await self._db.conn.execute(
            "SELECT version, file_name, size, sha256, path, telegram_file_id, checked_at "
            "FROM vpn_apk WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            return None
        return {
            "version": row["version"],
            "file_name": row["file_name"],
            "size": row["size"],
            "sha256": row["sha256"],
            "telegram_file_id": row["telegram_file_id"],
            "checked_at": row["checked_at"],
        }

    async def _download_apk(self, tag: str, asset: dict[str, Any]) -> None:
        url = str(asset.get("browser_download_url") or "")
        if not url:
            raise ProtoError(ERR_INTERNAL, "у релиза нет ссылки на APK")
        name = str(asset.get("name") or "amneziawg.apk")
        expected_size = int(asset.get("size") or 0)
        expected_sha = apk_client.asset_sha256(asset)

        cache_dir = self._cfg.apk_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / name
        tmp = cache_dir / f".{name}.part"
        await asyncio.to_thread(apk_client.download_sync, url, tmp, APK_DOWNLOAD_TIMEOUT_S)
        actual_size = tmp.stat().st_size
        if expected_size and actual_size != expected_size:
            tmp.unlink(missing_ok=True)
            raise ProtoError(
                ERR_INTERNAL,
                f"скачанный APK не совпал по размеру ({actual_size} != {expected_size})",
            )
        actual_sha = await asyncio.to_thread(apk_client.sha256_file, tmp)
        if expected_sha and expected_sha != actual_sha:
            tmp.unlink(missing_ok=True)
            raise ProtoError(ERR_INTERNAL, "скачанный APK не совпал по sha256")
        tmp.replace(dest)
        now = _now().isoformat()
        await self._db.conn.execute(
            "INSERT INTO vpn_apk (id, version, file_name, size, sha256, path, "
            "telegram_file_id, checked_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "file_name = excluded.file_name, size = excluded.size, sha256 = excluded.sha256, "
            "path = excluded.path, telegram_file_id = NULL, checked_at = excluded.checked_at, "
            "updated_at = excluded.updated_at",
            (tag, name, actual_size, actual_sha, str(dest), now, now),
        )
        await self._db.conn.commit()

    async def _apk_info(self, _args: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        if (
            self._apk_checked_at is not None
            and (now - self._apk_checked_at).total_seconds() < APK_INFO_MEMO_S
        ):
            row = await self._apk_row()
            if row is not None:
                return {**row, "stale": False}
        try:
            release = await asyncio.to_thread(
                apk_client.latest_release_sync, self._cfg.apk_repo, APK_API_TIMEOUT_S
            )
        except apk_client.ApkFetchError:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(
                    ERR_INTERNAL, "APK ещё ни разу не скачан, а GitHub сейчас недоступен"
                ) from None
            return {**row, "stale": True}
        self._apk_checked_at = now
        tag = str(release.get("tag_name") or "")
        asset = apk_client.pick_apk_asset(release.get("assets") or [])
        if asset is None:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(ERR_INTERNAL, "в последнем релизе не нашлось APK")
            return {**row, "stale": True}
        row = await self._apk_row()
        if row is not None and row.get("version") == tag and row.get("size") == asset.get("size"):
            return {**row, "stale": False}
        await self._download_apk(tag, asset)
        row = await self._apk_row()
        assert row is not None
        return {**row, "stale": False}

    async def _apk_chunk(self, args: dict[str, Any]) -> dict[str, Any]:
        offset = int(args.get("offset") or 0)
        length = int(args.get("length") or APK_CHUNK_BYTES)
        cur = await self._db.conn.execute("SELECT path, sha256, size FROM vpn_apk WHERE id = 1")
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            raise ProtoError(ERR_BAD_REQUEST, "APK ещё не скачан — сначала apk_info")
        data = await asyncio.to_thread(apk_client.read_chunk, Path(row["path"]), offset, length)
        eof = offset + len(data) >= int(row["size"] or 0)
        return {
            "data_b64": base64.b64encode(data).decode(),
            "offset": offset,
            "eof": eof,
            "sha256": row["sha256"],
        }

    async def _apk_set_file_id(self, args: dict[str, Any]) -> dict[str, Any]:
        file_id = str(args.get("telegram_file_id") or "").strip()
        if not file_id:
            raise ProtoError(ERR_BAD_REQUEST, "не передан telegram_file_id")
        await self._db.conn.execute(
            "UPDATE vpn_apk SET telegram_file_id = ? WHERE id = 1", (file_id,)
        )
        await self._db.conn.commit()
        return {"ok": True}

    # --- диспетчер ---

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.backup is not None:
            own = await self.backup.handle(action, args)
            if own is not None:
                return own
        result = await self._run_command(action, args)
        if self.backup is not None and action in SNAPSHOT_TRIGGER_ACTIONS:
            self.backup.touch()  # БД изменилась — снапшот напарнику вне очереди
        return result

    async def _run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_PEERS:
            return await self._peers(args)
        if action == ACTION_ISSUE:
            return await self._issue(args)
        if action == ACTION_REISSUE:
            return await self._reissue(args)
        if action == ACTION_REVOKE:
            return await self._revoke(args)
        if action == ACTION_GET_VLESS:
            return await self._get_vless(args)
        if action == ACTION_GET_SUBSCRIPTION:
            return await self._get_subscription(args)
        if action == ACTION_SUB_LINKS:
            return await self._sub_links(args)
        if action == ACTION_SUB_GEN:
            label = str(args.get("device_label") or "").strip()
            return {"gen": await self._device_gen(self._chat_id(args), label)}
        if action == ACTION_USAGE:
            return await self._usage(args)
        if action == ACTION_SET_QUOTA:
            return await self._set_quota(args)
        if action == ACTION_SET_ACCESS:
            return await self._set_access(args)
        if action == ACTION_GRANT_EXTRA:
            return await self._grant_extra(args)
        if action == ACTION_REQUEST_EXTRA:
            return await self._request_extra(args)
        if action == ACTION_RESOLVE_REQUEST:
            return await self._resolve_request(args)
        if action == ACTION_APK_INFO:
            return await self._apk_info(args)
        if action == ACTION_APK_CHUNK:
            return await self._apk_chunk(args)
        if action == ACTION_APK_SET_FILE_ID:
            return await self._apk_set_file_id(args)
        if action == ACTION_REPORT_CHECK:
            return await self._report_check(args)
        if action == ACTION_CHECK_NOW:
            return await self._dispatch_checks()
        if action == ACTION_CHECK_STATUS:
            return await self._check_status()
        if action == ACTION_TELEGRAM_EGRESS:
            return await self._telegram_egress(args)
        if action == ACTION_PROXY_LINK:
            return await self._proxy_link(args)
        if action == ACTION_PROXY_ROTATE_SECRET:
            return await self._proxy_rotate_secret(args)
        if action == ACTION_PROXY_USAGE:
            return await self._proxy_usage(args)
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")
