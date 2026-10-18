# Сеть сервера VPN, прокси и сборка бинарей

## awg-сервер на ноде с ufw (wooster, 39.0.8(f))
jeeves — чистый nftables без ufw: порт открыт в `/etc/nftables.conf`, forward/NAT поднимает
`PostUp` `awg0.conf` (`deploy/setup-awg-jeeves.sh`). На wooster firewall — ufw, а `ip filter`/`ip nat`
принадлежат tailscaled/ufw, и `iptables-nft PostUp` туда не пишет («chain FORWARD is incompatible»).
Поэтому нужны (1) правило ufw на UDP-порт (`awg-ufw-port`) и (2) systemd-юнит, переигрывающий
forward+NAT+MSS-clamp через nft при каждом старте (`awg-forward-persist`; MSS = MTU − 40, path MTU
из РФ, см. 39.0.3). Оба применимы ТОЛЬКО при активном ufw: на jeeves их нет и ничего не трогается.
Если статус ufw не прочитался (нет беспарольного sudo), но ufw установлен, `awg-ufw-port` считается
нужным (check скажет «не применено», apply спросит sudo); ufw при этом НЕ включается.
Порт страницы подписки Hiddify и `80/tcp` под http-01 Let's Encrypt (57.10) открывает
`sub-ufw-ports`; на jeeves эти строки вносятся в `/etc/nftables.conf` руками (`deploy/sub-cert.sh`).
Прокси mtg/microsocks и sudoers awg нужны только ноде со службой vpn и транспортом awg
(`needs.vpn_awg`); нода с одним reality (xray ходит по gRPC API без sudo) их не касается.

## nft: только точечные правила, никогда `nft -f`
Инцидент 2026-08-17 (VPN jeeves, NAT стёрт перезагрузкой nftables): полный `nft -f
/etc/nftables.conf` стирает ВСЕ таблицы, включая iptables-nft-таблицы, которые `awg-quick`
добавляет через PostUp — NAT/FORWARD у AmneziaWG пропадают без ошибки, VPN хендшейкается, но
интернета нет. Поэтому живой ruleset правится только `nft add`/`insert`; персистентный файл
правится отдельно и применяется только при следующем чистом бутстрапе systemd.
Ловушка jeeves (см. память «nftables forward-hook ловушка»): пустая policy-drop chain `forward`
глушит VPN-трафик; `restart tailscaled` стирает NAT-правила awg-quick — после него нужен
`restart awg-quick@awg0`.

## Счётчики трафика прокси (`proxy-firewall`)
Именованные счётчики `mtg_bytes`/`socks_bytes` в `inet filter`, правила в цепочке `input`.
- Баг 2026-08-17 (найден 2026-08-21): `check()` проверял только существование объектов-счётчиков
  (`nft add counter`), строки `nft insert rule` после создания счётчиков не выполнялись — счётчики
  существовали, но `bytes: 0` навсегда. Проверять надо ПРАВИЛА, ссылающиеся на счётчик
  (`_rule_counter_names`). В `nft -j` ссылка на именованный счётчик в `expr` правила — голая строка
  (`{"counter": "mtg_bytes"}`), объектная форма `{"name": …}` — только у определения (проверено на
  jeeves 2026-08-21 в изолированной scratch-таблице).
- `nft insert rule` без позиции добавляет в НАЧАЛО цепочки (проверено на jeeves 2026-08-17): новое
  правило со счётчиком получает пакет первым; старые правила не трогаем и не удаляем.
- Инцидент 2026-08-31: счётчик-ОБЪЕКТ объявили внутри `chain`, а правило вставили ДО строки `type …
  hook …` — `nft -f` падал на следующем ребуте, nftables.service не стартовал, VPN оставался без
  NAT. Теперь объекты объявляются на уровне таблицы (после `table inet filter {`), правила — после
  строки `type filter hook input`, результат прогоняется через `nft -c -f` ДО установки; файл
  `0755`, структура файла не та, что ожидалась — отказ с просьбой дописать вручную.
- Фикс нужен только там, где есть `inet filter` (wooster — `ip filter` от iptables-nft, трогать не
  велено); если `nft list tables` не прочитался — считается «таблица есть», чтобы не прятать фикс
  от jeeves.
- sudoers `proxy-nft`: только `nft -j list counter inet filter mtg_bytes|socks_bytes`, не весь nft.

## mtg + microsocks (`proxy-units`, 2026-08-17)
Кодифицирует то, что 2026-08-13 было поднято руками по SSH: ссылку, секрет и учёт трафика отдаёт
бот через службу vpn (`vpn/service.py::_proxy_link`), фикс только воспроизводит установку демонов.
Юниты user-уровня (`~/.config/systemd/user/{mtg,microsocks}.service`), существующие не
перезаписываются. Секрет mtg — `vpn/protocol.py::PROXY_SECRET_SEED` (тот же литерал сидирует БД
при первом старте службы); дальнейшая смена — только через `proxy_rotate_secret` (правит и БД, и
юнит). mtg ставится из релизного архива с проверкой sha256 (`MTG_SHA256`) и `setcap
cap_net_bind_service=+ep`; microsocks — пакетом. `[vpn].socks_host` (tailscale-адрес jeeves)
обязателен.

## Сборка amneziawg-tools и установка xray
- amneziawg-tools/amneziawg-go нет в apt Debian (проверено на alfred 2026-08-17) и апстрим не
  публикует свой репозиторий — только сборка из исходников, как `deploy/setup-awg-jeeves.sh`:
  официальный тарбол Go (системный слишком старый: нужен go ≥ 1.25) в каталог сборки +
  `git clone`/`make install` в `/usr/local/bin`. Системный Go не трогаем. `awg-quick` сам находит
  `amneziawg-go` в PATH как userspace-фолбэк, когда нет модуля ядра.
- xray-core (клиент reality-пробника, 39.0.7(e)) ставится готовым бинарём версии `XRAY_VERSION` в
  `/usr/local/bin/xray`: архив + `.dgst` рядом, сверка sha256 до распаковки — тот же приём, что
  `deploy/setup-reality-server.sh` на серверной стороне; клиент ставится на ЛЮБУЮ ноду с `vpn_check`.
