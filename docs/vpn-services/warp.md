# Cloudflare WARP для выбранных доменов (fixup `vpn-warp`)

## Зачем
2026-11-21 на ion (Молдова, хостер AVA HOST) YouTube через Reality рвал видео через 1–2 с:
«Этот контент недоступен». Это антибот YouTube на IP дата-центра; на wooster тот же класс IP
даёт «Sign in to confirm you're not a bot». Выход YouTube/Google через Cloudflare WARP это
вылечил (владелец подтвердил). Сначала правка была ручной на ion, теперь она в коде.

## Настройка
Необязательная секция `[vpn.warp]` в конфиге VPN-ноды (с Reality): `keys` (1..4, по умолчанию 2),
`domains` (по умолчанию YouTube и Google: youtube.com, youtu.be, googlevideo.com, ytimg.com,
ggpht.com, youtube-nocookie.com, youtubei/youtube.googleapis.com, yt.be, youtubekids.com,
withyoutube.com, googleapis.com, gstatic.com, google.com, googleusercontent.com, gvt1.com,
gvt2.com, 1e100.net). Без секции xray не трогается. Пример закомментирован в `config.example.toml`.

## Что делает `nodectl fix` (`node/fixups/vpn_warp.py`, fixup `vpn-warp`)
- Ключи: файлы `~/.config/xray/warp-<n>.json` (0600), n = 1..keys. Ключ WireGuard создаётся
  на самой ноде (`awg genkey`/`awg pubkey`) и не покидает её; регистрация в
  `api.cloudflareclient.com/v0a2158/reg` (Android-клиент). Регистрируются только недостающие
  файлы. Ручной `warp.json` принимается как `warp-1.json` (переименование, без перевыпуска).
  Ключи и ответы API не логируются.
- Конфиг xray (чистые функции `with_warp`, `warp_applied`, идемпотентны): outbounds
  `warp-1..warp-N` (wireguard, mtu 1280, `domainStrategy` ForceIPv4, `reserved` из `client_id`,
  endpoint на порт 2408); `routing.balancers` с `warp` (`selector ["warp-"]`, `leastPing`);
  `burstObservatory` (`https://www.google.com/generate_204`, интервал 5 м, таймаут 10 с):
  мёртвый ключ выпадает сам; правило `domain:<d>` для клиентских входов с `balancerTag: warp`
  сразу ПОСЛЕ правил private-block (`server-isolation.md`).
- Сниффинг клиентских входов обязан содержать `quic`, иначе QUIC YouTube идёт мимо правила;
  фикс-ап добавляет его там, где сниффинг включён.
- Запись через `write_xray_config_with_test`, затем рестарт xray и ноды (как у
  `xray-private-block`: клиенты xray не хранятся в файле, их возвращает сверка при старте
  службы `vpn`).

## Ограничения и риски
- Google видит выход как WARP-IP с геолокацией RU, но видео играют.
- Бесплатно, без лимита трафика. ToS WARP рассчитаны на личное использование, ключ могут
  забанить; поэтому несколько ключей и observatory.
- AWG не покрывается: туннель AWG идёт без имён доменов, правило по `domain` к нему не применимо.
- Перевыпуск ключа: удалить `~/.config/xray/warp-<n>.json` и выполнить `nodectl fix`.

## Проба
Пока не сделана. Живость ключей знает только xray observatory внутри процесса. Варианты в отчёте
задачи: (а) `xray api` статус outbound, (б) локальный socks-inbound с правилом в balancer;
оба требуют решения, куда слать итог (самопроверка сервера в `vpn_check` исключена).
