# Фиксы привилегий (`nodectl fix`)

## Инвариант
`nodectl fix` — короткоживущий процесс, не демон. Он читает конфиг локально, по назначениям ноды
(`needed(settings)`) выбирает фиксы и для каждого непройденного `check()` зовёт настоящий
интерактивный `sudo` (наследует TTY; пароль нигде не хранится и не передаётся по сети).
Долгоживущий процесс ноды (`node/service.py`) сам `sudo` не вызывает и прав не хранит —
фиксы этот инвариант не нарушают. Службы, которым нужен root, получают узкие sudoers-снипеты
(`NOPASSWD` на конкретный путь и конкретные аргументы, пути резолвятся заранее — защита от PATH-hijack).

Общий код с `sa-home-bot init`: после первой установки workstation-ноды мастер зовёт те же
`build_fixups`/`run_fixups` (`setup_wizard._apply_init_fixups`).

## Публичный API пакета `sa_home_bot.node.fixups`
`build_fixups(settings)`, `run_fixups(fixups)`, `Fixup`, `FixupError`, `SMARTCTL_WRAPPER_PATH`.
`backup/apply.py::SudoIO` работает через `node.fixups.privileged` напрямую (`read_text_if_exists`, `temp_file` + `install_from(mode="0600", make_parents=True)`, `unit_is_active`, `run_user`/`run_sudo`).

## Модули
| Модуль | Что внутри |
|---|---|
| `core.py` | `Fixup`, `FixupError`, `run_fixups` |
| `needs.py` | предикаты `needed` по назначениям/типу ноды |
| `privileged.py` | примитивы: `find_executable`, `run_sudo`, `run_user`, `path_exists`, `read_text`, `install_root_file`, `install_sudoers_snippet`, `install_package`, `replace_unit`, `enable_now`, `unit_is_active`, `download`, `file_sha256` |
| `netfilter.py` | nft-обёртки (`sudo -n nft`), `NftChain`, ufw-разбор, `ip_forward`, маршрут по умолчанию |
| `host.py` | smartmontools, sudoers+обёртка smartctl, PATH юнита ноды, группа journal, sudoers приложений, polkit power, Wake-on-LAN |
| `vpn_server.py` | sudoers awg, ufw-порты awg и подписки Hiddify, forward/NAT awg-сервера под ufw |
| `probe_net.py`, `probe_tunnel.py` | netns-пробники `vpn_check` (см. [vpn-probes.md](vpn-probes.md)) |
| `proxy.py` | mtg + microsocks, счётчики трафика прокси в nftables |
| `devlab.py` | мастерская devlab (Этап 60.1): podman, пользователь `devlab`, subuid/subgid, linger, обёртка контейнеров + sudoers |
| `toolchain.py` | сборка amneziawg-tools, xray-core, mtg |
| `build.py` | `build_fixups`: порядок фиксов |

## Семантика `check()` — менять нельзя
Различие «файл есть» и «содержимое равно» осмысленное: фикс с проверкой «есть» не перезаписывает
файлы, поставленные вручную на живых нодах (wooster, jeeves). Унификация, сводящая всё к одному
варианту, заставила бы `nodectl fix` перетирать рабочее.

| Фикс | `check()` |
|---|---|
| `install-smartmontools` | `smartctl` находится (PATH + sbin-каталоги) |
| `smartctl-sudoers` | существуют и sudoers-снипет, и обёртка `~/.local/bin/smartctl` (содержимое не сверяется) |
| `node-unit-smartctl-path` | юнита нет — «применено» (PATH допишет `init`); иначе первый каталог `Environment=PATH=` — `~/.local/bin` |
| `journalctl-group` | пользователь в группе `systemd-journal` |
| `apps-unit-sudoers-<id>`, `awg-sudoers`, `power-control-polkit`, `wol-enable` | файл существует (содержимое не сверяется) |
| `awg-ufw-port`, `sub-ufw-ports` | ufw активен и все порты разрешены |
| `awg-forward-persist` | юнит есть и активен; содержимое НЕ сверяется (на wooster поставлен руками, скрипт чуть отличается); `apply` существующие файлы не перетирает |
| `vpn-probe-legacy-cleanup` | нет юнита/netns/veth прежней схемы |
| `vpn-check-probe-forwarding` | `ip_forward=1` и в ruleset есть veth и подсеть каждого слота |
| `vpn-check-probe-forwarding-persist` | скрипт есть, юнит РАВЕН ожидаемому и активен |
| `vpn-probe-scaffold-*` | юнит равен ожидаемому и активен |
| `vpn-probe-conf-*` | awg: файл есть, нет строки `Table`, ключ пира совпадает с ключом сервера; reality: файл есть и несёт ожидаемый `socks_port` |
| `vpn-check-probe-sudoers`, `vpn-check-probe-state` | содержимое равно |
| `proxy-units` | бинарь mtg, оба user-юнита, оба активны |
| `proxy-firewall` | на счётчики ссылаются ПРАВИЛА цепочки `input` (не только существуют объекты-счётчики) |
| `install-podman` | находятся `podman` и `newuidmap` |
| `devlab-user` | `getent passwd devlab` успешен |
| `devlab-subids` | у `devlab` есть строки и в `/etc/subuid`, и в `/etc/subgid` |
| `devlab-linger` | есть `/var/lib/systemd/linger/devlab` |
| `devlab-sudoers` | обёртка и sudoers-снипет РАВНЫ ожидаемым |

`run_fixups` терпит падение самого `check()` (`FixupError` — например `sudo cat` без TTY/кэша):
проверка считается отрицательной и пробуется `apply()`. Живой краш 2026-08-17: необработанный
`FixupError` из `check()` останавливал все оставшиеся фиксы.

## Живые находки
- **2026-08-01, mycraft**: apt «уже установлен», а `check()` на голом `shutil.which` не видел
  `smartctl` в `/usr/sbin` — фикс вечно висел «команда прошла, проверка отрицательна».
  Отсюда `find_executable` с фолбэком на `/usr/local/sbin`, `/usr/sbin`, `/sbin`: интерактивный
  PATH по SSH (в отличие от PATH юнита ноды) их не содержит.
- **2026-08-01, mycraft**: минимальная серверная установка не тянет polkit — каталога
  `/etc/polkit-1` нет, `install` падал с ENOENT. Пакет `polkitd` (на Debian/Ubuntu
  `policykit-1` — пустой переходный) ставится, если `rules.d` нет; затем `install -d`.
- **2026-08-08**: баг `init` мог развернуть путь исполняемого файла до каталога pipx-venv вместо
  `~/.local/bin`; юнит оказывался без обёртки smartctl в PATH. Чинит `node-unit-smartctl-path`
  (`rewrite_unit_path_line`: каталог обёртки первым, дубли убираются).
- **Обёртка smartctl**: sudo без пароля сам по себе ничего не даёт — код зовёт голое `smartctl`.
  Юнит ноды кладёт `~/.local/bin` первым в PATH ради скрипта-обёртки `exec sudo -n <настоящий
  smartctl> "$@"`. `real_smartctl_path` исключает каталог обёртки из PATH, иначе повторный запуск
  поставил бы sudoers-снипет на сам скрипт.
- **Приватный каталог**: `/etc/amnezia/amneziawg` создаёт `setup-awg-jeeves.sh` под `umask 077`;
  голый `stat` падает `PermissionError` даже для файла, которого там нет. `path_exists` в этом
  случае переспрашивает через `sudo test -e`, иначе `apply()` решил бы, что конфига нет, и
  выпросил бы новый поверх рабочего.
- **Узкий sudoers awg** (этап 33): разрешены `awg show *` и `awg set <iface> peer *`. НЕ
  `awg-quick` целиком (исполняет произвольные PostUp = root) и НЕ `awg set … private-key/listen-port`.
- Генерируемые sudoers/юниты/скрипты проверялись побайтно до/после рефакторинга одноразовым
  golden-сравнением (последовательность подпроцессов и содержимое устанавливаемых файлов) — менять
  их текст без причины нельзя: `check()` ряда фиксов сравнивает содержимое с ожидаемым.

## devlab (Этап 60.1, 2026-10-31)
Фиксы нужны ноде, которой назначена служба `devlab` или `devlab_models` (`needs.devlab`).
Конфиг `[devlab]`: `root` (по умолчанию `/models/sa-home/devlab`), `user` (`devlab`).
- Дом `devlab` — `<root>/home`, а не сам `<root>`: в `<root>` от владельца лежат стенд 60.2,
  модели и загрузки, `useradd --create-home` в существующий чужой каталог не сменил бы владельца.
  `/home` на mycraft почти полон, хранилище podman (`~/.local/share/containers`) живёт в доме.
- Пользователь системный (`--system`, оболочка `nologin`) — такому useradd не выдаёт subuid сам,
  поэтому отдельный `devlab-subids`: диапазон 65536 начинается сразу после максимального занятого
  в `/etc/subuid`/`/etc/subgid` (на mycraft `sevboa` и `davidr` заняли до 231071).
- Обёртка `/usr/local/libexec/sa-home-devlab` (root:root 0755, редактировать её `devlab` не может)
  исполняется ОТ `devlab`, не от root: снипет `<пользователь ноды> ALL=(devlab) NOPASSWD: <обёртка> *`,
  вызов `sudo -n -u devlab <обёртка> …`. Root исполнителю не нужен — podman rootless.
- Подкоманды обёртки: `sync-in <id>` (tar со stdin в `~/work/<id>`, каталог пересоздаётся),
  `sync-out <id>` (tar рабочей копии в stdout), `clean <id>`, `build <id> <образ>` (Containerfile
  со stdin, контекст — рабочая копия, сеть при сборке есть), `create <id> <образ> <cpus> <память>`,
  `start|stop|rm <id>`, `exec <id> <таймаут> <команда>`, `ps`, `images`, `prune`.
  Имена — только `[a-z0-9-]`, не с дефиса; числа и размеры проверяются; контейнеры `devlab-<id>`,
  образы `devlab/<имя>`.
- Контейнер задачи создаётся с `--network none`, квотами CPU/памяти и `--pids-limit 4096`;
  рабочая копия монтируется в `/work`. Рабочие копии передаются tar-потоком, чтобы `devlab` не
  требовалось читать каталоги пользователя ноды.
- До Этапа 60.5 служба `devlab` не зарегистрирована в `services/registry.py`: супервизор на
  назначение `devlab` пишет «Неизвестное назначение … — пропускаю». Это ожидаемо, назначение
  нужно только чтобы `nodectl fix` выбрал фиксы.

## Известное отличие после рефакторинга
`awg-ufw-port.apply` теперь, как и `sub-ufw-ports`, перед `ufw allow` читает `ufw status` и не
добавляет уже разрешённое (раньше добавлял безусловно). Достижимых состояний это не меняет:
`apply` вызывается только когда `check()` отрицателен.
