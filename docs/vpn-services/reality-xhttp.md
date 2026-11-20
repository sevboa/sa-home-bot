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
- Клиенты на вход добавятся в следующей задаче.

## Ограничение
У XHTTP поле `flow` пустое: `xtls-rprx-vision` с XHTTP не работает.
