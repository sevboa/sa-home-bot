# node-fixups: `nodectl fix`, `nodectl`, `sa-home-bot init`

Зона: пакет `src/sa_home_bot/node/fixups/`, `nodectl.py`, `setup_wizard.py`.

- [fixups.md](fixups.md) — устройство пакета, инварианты, семантика `check()` каждого фикса, общие примитивы.
- [vpn-probes.md](vpn-probes.md) — netns-пробники `vpn_check`: зачем изоляция, scaffold/conf/sudoers/state, forward/NAT и переживание ребута, устаревший конфиг, legacy-пробник.
- [network-and-proxy.md](network-and-proxy.md) — awg-сервер с ufw, ip_forward, nft-ловушки, счётчики прокси, mtg/microsocks, сборка amneziawg-tools и установка xray.
- [cli.md](cli.md) — команды `nodectl`, рабочий каталог ноды, мастер `sa-home-bot init`.
