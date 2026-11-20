# Третий вход Reality с транспортом XHTTP (63.3.3а)

## Зачем
XHTTP прячет долгий туннель за отдельными HTTP-запросами, что мешает детектированию по
длительным сессиям. Официальный sing-box и AmneziaVPN XHTTP не умеют, Hiddify ненадёжен,
клиенты на xray-core — v2RayTun, Happ, v2rayNG — умеют.

## Настройка
Секция `[vpn.reality.xhttp]` в конфиге сервера: `port` (по умолчанию 2087), `sni`,
`dest` (пусто: `<sni>:443`), `inbound_tag` (по умолчанию `reality-xhttp`), `path`
(обязательно, начинается с `/`, длина ≥ 2), `mode` (`auto` по умолчанию, также
`packet-up`, `stream-up`, `stream-one`). Порт и тег не должны совпадать с основным
входом или с `alt` (валидатор `RealityTransportConfig`). Без секции поведение и вывод
прежние байт в байт.

## Сервер
- `nodectl fix`, fixup `reality-xhttp-inbound` (`node/fixups/vpn_server.py`): дописывает вход в
  `~/.config/xray/config.json` (копия входа `reality-in` с `network = "xhttp"`,
  `xhttpSettings` с `path` и `mode`, подменой `dest` и `serverNames` в `realitySettings`,
  те же ключи и `shortIds`, клиенты пустые, права файла сохраняются) и добавляет его в живой
  xray через `xray api adi`. Наличие в живом xray определяется по `xray api lsi --isOnlyTags=true`.
  Запись файла вынесена в `write_xray_config_with_test`, её зовут и fixup `reality-alt-inbound`.
  Ключи не логируются.
- fixup `reality-xhttp-ufw-port` открывает порт в ufw.
- Клиенты: при старте службы `vpn` сверка `reconcile_clients` наполнит вход (см. «Клиенты»).

## Клиенты
- Секция `[vpn.reality.xhttp]` в конфиге: служба `vpn` (`vpn/app.py`) создаёт третий
  `RealXrayBackend` с `inbound_tag` и `port` секции и передаёт его в `DualXrayBackend`
  (`reality/xray.py`) — в том числе когда `alt` нет.
- `add_client` добавляет на xhttp те же email и UUID с пустым `flow` (Vision с XHTTP не
  работает); сбои `ProtoError` на xhttp только в журнал, как у alt, добавление не роняет.
- `remove_client` удаляет на всех входах, сбои alt/xhttp в журнал.
- `list_clients` и `stats` — только основной вход.
- `reconcile_clients` при старте сверяет xhttp отдельно с пустым `flow`, сбой в журнал;
  сверка наполнит вход после включения секции. Служба `vpn` после включения перезапускается
  (не xray).
- Без секции поведение прежнее байт в байт.

## Ограничение
У XHTTP поле `flow` пустое: `xtls-rprx-vision` с XHTTP не работает.

## Ссылка
При выдаче Reality-устройства (2026-11-20) ответ содержит `xhttp_share_url`, если задана
секция `[vpn.reality.xhttp]`: ссылка `vless://…?…&type=xhttp&path=<закодированный
path>&mode=<mode>#<метка устройства + " · XHTTP">` на вход XHTTP, параметр `flow` не
выводится (пустой). Ссылку понимают xray-клиенты (v2RayTun, Happ) и её же разбирает
`parse_vless_url` (`type` по умолчанию `tcp`, `path` раскодируется, `mode` по умолчанию
пустой, `flow` при `type=xhttp` по умолчанию пустой). Параметры собираются в
`RealityParams.xhttp_from_config` (port/sni из секции, `network="xhttp"`, path/mode из
секции), выдача — `peers.py::_reality_artifacts` (и для обычных чатов, и для пробника),
sing-box конфиги XHTTP не поддерживают и на такие параметры бросают
`ValueError("sing-box не поддерживает XHTTP")`. Без секции ответ и все ссылки прежние
байт в байт.
