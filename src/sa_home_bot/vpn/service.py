from __future__ import annotations

import asyncio
import socket
from datetime import datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.backup.snapshot import TRIGGER_ACTIONS as SNAPSHOT_TRIGGER_ACTIONS
from sa_home_bot.backup.snapshot import SnapshotBackup
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import undeclared_action
from sa_home_bot.reality.xray import XrayBackend
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.apk_service import ApkMixin
from sa_home_bot.vpn.awg import AwgBackend
from sa_home_bot.vpn.checks import ChecksMixin
from sa_home_bot.vpn.client_configs import ClientConfigsMixin
from sa_home_bot.vpn.peers import PeersMixin
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
    GB,
    PROBE_CHAT_ID,
    SERVICE_NAME,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
)
from sa_home_bot.vpn.proxy import ProxyMixin
from sa_home_bot.vpn.proxy_backend import ProxyBackend, RealProxyBackend
from sa_home_bot.vpn.quota import QuotaMixin
from sa_home_bot.vpn.reconcile import ReconcileMixin
from sa_home_bot.vpn.service_common import (
    CHECK_STALE_FACTOR,
    EmitFn,
    _random_device_label,
    log,
)
from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin
from sa_home_bot.vpn.usage import UsageMixin

__all__ = [
    "CHECK_STALE_FACTOR",
    "GB",
    "PROBE_CHAT_ID",
    "VpnService",
    "_random_device_label",
]


class VpnService(
    PeersMixin,
    ReconcileMixin,
    QuotaMixin,
    ClientConfigsMixin,
    SubscriptionLinksMixin,
    UsageMixin,
    ProxyMixin,
    ChecksMixin,
    ApkMixin,
):
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
        self._node_link = node_link
        self.backup: SnapshotBackup | None = None
        self._issue_lock = asyncio.Lock()
        self._node_id = settings.node.id or socket.gethostname()
        self._sub_secret = subs.derive_secret(self._cfg.sub_secret, settings.swarm.token)
        self.sub_web: Any = None
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
        if self._reality_enabled:
            capabilities += [ACTION_GET_VLESS]
            actions += [
                ActionSpec(
                    id=ACTION_GET_VLESS,
                    title="🔗 Ссылка VLESS",
                    params=(chat_id_param, device_param),
                ),
            ]
        if self._reality_enabled:
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
        capabilities += [ACTION_CHECK_NOW, ACTION_CHECK_STATUS, ACTION_TELEGRAM_EGRESS]
        actions += [
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
            ActionSpec(
                id=ACTION_TELEGRAM_EGRESS,
                title="📡 Маршрут до Telegram через эту ноду",
                params=(ActionParam(name="observer", type="string", title="Нода бота"),),
            ),
        ]
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
        if self.backup is not None:
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
        server_pubkey: str | None = None
        if self._has(TRANSPORT_AWG):
            try:
                server_pubkey = await self._server_public_key()
            except Exception:
                log.warning("vpn: не удалось прочитать публичный ключ awg-сервера", exc_info=True)
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "active_peers": row["n"] if row else 0,
            "label": self._cfg.location,
            "server_public_key": server_pubkey,
            "transports": list(self._transports),
            "check": await self._check_rollup(server=self._node),
            "access_control": True,
        }

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — доступ выдаётся человеку")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.backup is not None:
            own = await self.backup.handle(action, args)
            if own is not None:
                return own
        result = await self._run_command(action, args)
        if self.backup is not None and action in SNAPSHOT_TRIGGER_ACTIONS:
            self.backup.touch()
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
            label = self._device_label(args)
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
        raise undeclared_action(action)
