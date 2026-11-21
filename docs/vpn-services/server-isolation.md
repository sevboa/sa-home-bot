# Изоляция сервера от клиентов VPN

## Находка 2026-11-21
В конфиге xray на jeeves и wooster маршрутизация состояла из одного правила для `api-in`, всё
остальное уходило в `freedom`. Клиент Reality или XHTTP мог открыть соединение к любому адресу,
который видит сам сервер: `127.0.0.1` (локальные службы и API xray), Tailscale-сеть `100.64.0.0/10`
(alfred, mycraft, хаб devlab) и домашние подсети. Проверено пробником с vanka: через туннель
отвечал SSH alfred по Tailscale-адресу. В тот же день на обоих серверах закрыто вручную, затем
фикс-апом.

AWG (`awg0`, `awg1`) этой дыры нет: через туннель не отвечают ни Tailscale-адреса, ни SSH сервера.
Отвечает только страница подписки на `10.9.x.1:8444`, она и так публичная.

## Что закрыто
Фикс-ап `xray-private-block` (`node/fixups/vpn_server.py`, `nodectl fix`):
- `routing.domainStrategy = "IPIfNonMatch"`: домены без доменного правила резолвятся и проверяются
  IP-правилами, иначе запрос по имени (`localhost`, `*.ts.net`) обходит IP-запрет;
- для всех входов, кроме `api-in`, в `block` идут `XRAY_BLOCKED_DEST_DOMAINS` и
  `XRAY_BLOCKED_DEST_IPS`: локальные, частные, CGNAT (Tailscale), link-local, multicast и их IPv6;
- правила стоят сразу за правилом `api-in`, `blackhole` с тегом `block` добавляется, если его нет.

Применение пишет конфиг через `xray run -test`, перезапускает `xray` и `sa-home-node`: клиенты
живут в xray только через API, и вернуть их может только сверка службы `vpn` при старте.
Новый вход (например, `reality-xhttp`) делает проверку ложной, поэтому фикс-ап стоит после
фикс-апов входов и переприменяется сам.

## Проверка
После любой правки конфига xray, нового входа или нового сервера:

1. `nodectl -n <сервер> fix` показывает `xray-private-block` как «уже применено».
2. Живая проба через туннель с ноды-пробника (vanka, jeeves или wooster; конфиг пробника
   `/etc/sa-home-bot/vpn-probe/vpn-probe-<сервер>-<транспорт>.json`, локальный SOCKS из его `inbounds`):
   ```
   xray run -c <конфиг пробника> &
   X="--socks5-hostname 127.0.0.1:<порт>"
   curl $X -sk -m 8 -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8444/   # ждём 000
   curl $X -s -m 8 telnet://<tailscale-адрес alfred>:22 </dev/null | head -c 20         # ждём пусто
   curl $X -s -m 8 -o /dev/null -w '%{http_code}\n' https://www.google.com/generate_204  # ждём 204
   ```
   Через `netns exec` на ноде с пробником то же самое, но сначала поднять туннель: для AWG
   `awg-quick up <iface>` внутри netns, иначе трафик уйдёт мимо туннеля через veth и проба соврёт.
3. Для AWG: в netns пробника `awg-quick up`, затем `bash -c 'head -c 20 </dev/tcp/<tailscale-адрес>/22'`
   должно молчать.

Тесты: `tests/unit/test_fixups_xray_private_block.py`.
