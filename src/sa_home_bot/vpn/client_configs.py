from __future__ import annotations

from typing import Any

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
)
from sa_home_bot.reality.client_config import (
    RealityParams,
)
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import (
    TRANSPORT_REALITY,
)


class ClientConfigsMixin:
    async def _get_vless(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = self._required_device_label(args)
        if not self._reality_enabled:
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

    async def _get_subscription(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = self._required_device_label(args)
        if not self._reality_enabled:
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
        return RealityParams.from_config(self._reality_cfg)

    def _sub_entry_name(self) -> str:
        return self._cfg.location or self._node_id
