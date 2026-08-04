#!/bin/sh
# Разовая установка AmneziaWG-сервера на jeeves (Этап 33 IMPLEMENTATION_PLAN.md).
#
# Запускать вручную по SSH на jeeves, под пользователем с sudo (не как часть
# деплоя пакета sa-home-bot — служба vpn сама интерфейс не создаёт, только
# управляет пирами через узкий sudoers, см. node/fixups.py::AWG_SUDOERS).
#
# Использование:
#   sudo ./setup-awg-jeeves.sh <внешний-интерфейс> <порт-UDP>
# например:
#   sudo ./setup-awg-jeeves.sh eth0 51820
#
# После успешного прогона скрипт печатает блок [vpn] для config.toml ноды —
# jc/jmin/jmax/s1/s2/h1-h4 и endpoint_host ОБЯЗАНЫ дословно совпадать с тем,
# что скрипт прописал в /etc/amnezia/amneziawg/awg0.conf (это две независимые
# копии одних и тех же чисел — служба их не читает из awg0.conf сама).
#
# Идемпотентность: повторный запуск переустановит бинарники, но если
# awg0.conf уже существует — ключ сервера и obfuscation-параметры из него
# СОХРАНЯЮТСЯ, не генерируются заново (см. инцидент 2026-08-04: генерация
# новых ключей при повторном прогоне без пересборки живого интерфейса
# разошлась с тем, что реально было поднято, а следующий ребут jeeves поднял
# awg0 заново из файла — и разом отвалились все ранее выданные клиентские
# конфиги). Чтобы всё же сменить ключ/параметры намеренно — удалите или
# отредактируйте awg0.conf руками перед запуском, затем `awg-quick down &&
# awg-quick up`, предупредив всех текущих клиентов о разрыве.

set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Запускать через sudo (нужен root для сети/systemd)." >&2
    exit 1
fi

IFACE="${1:?использование: $0 <внешний-интерфейс> [порт-UDP=51820]}"
LISTEN_PORT="${2:-51820}"
WG_IFACE="awg0"
SUBNET="10.9.0.0/24"
SERVER_ADDR="10.9.0.1/24"
AWG_DIR="/etc/amnezia/amneziawg"
AWG_CONF="${AWG_DIR}/${WG_IFACE}.conf"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "==> Проверка окружения"
if ! command -v gcc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y build-essential
fi

# amneziawg-go требует свежий Go (go.mod требует много новее, чем есть в
# Debian oldstable/stable — на jeeves стоял golang-go 1.19, который даже не
# парсит директиву "go 1.25.0" в go.mod). Системный Go не трогаем — тянем
# официальный тарбол во временный каталог и используем только для сборки.
GO_BIN="$(command -v go || true)"
GO_OK=0
if [ -n "$GO_BIN" ]; then
    case "$(go version 2>/dev/null)" in
        *" go1.2"[1-9]*|*" go1.[3-9][0-9]"*) GO_OK=1 ;;
    esac
fi
if [ "$GO_OK" -ne 1 ]; then
    GO_VERSION="$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -1)"
    echo "    системный Go отсутствует/устарел — качаю официальный ${GO_VERSION} во временный каталог"
    curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-amd64.tar.gz" -o "$BUILD_DIR/go.tar.gz"
    tar -C "$BUILD_DIR" -xzf "$BUILD_DIR/go.tar.gz"
    GO_BIN="$BUILD_DIR/go/bin/go"
fi
PATH="$(dirname "$GO_BIN"):$PATH"
export PATH

echo "==> Сборка amneziawg-go (userspace WireGuard-имплементация с обфускацией)"
if ! command -v amneziawg-go >/dev/null 2>&1; then
    git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go "$BUILD_DIR/amneziawg-go"
    make -C "$BUILD_DIR/amneziawg-go"
    install -m 0755 "$BUILD_DIR/amneziawg-go/amneziawg-go" /usr/local/bin/amneziawg-go
fi

echo "==> Сборка amneziawg-tools (awg, awg-quick + systemd-юнит awg-quick@)"
if ! command -v awg >/dev/null 2>&1; then
    git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-tools "$BUILD_DIR/amneziawg-tools"
    make -C "$BUILD_DIR/amneziawg-tools/src"
    make -C "$BUILD_DIR/amneziawg-tools/src" install
fi

umask 077
mkdir -p "$AWG_DIR"

if [ -f "$AWG_CONF" ]; then
    echo "==> ${AWG_CONF} уже существует — сохраняю ключ сервера и obfuscation-параметры из него"
    SERVER_PRIVATE_KEY="$(sed -n 's/^PrivateKey = //p' "$AWG_CONF" | head -1)"
    JC="$(sed -n 's/^Jc = //p' "$AWG_CONF" | head -1)"
    JMIN="$(sed -n 's/^Jmin = //p' "$AWG_CONF" | head -1)"
    JMAX="$(sed -n 's/^Jmax = //p' "$AWG_CONF" | head -1)"
    S1="$(sed -n 's/^S1 = //p' "$AWG_CONF" | head -1)"
    S2="$(sed -n 's/^S2 = //p' "$AWG_CONF" | head -1)"
    H1="$(sed -n 's/^H1 = //p' "$AWG_CONF" | head -1)"
    H2="$(sed -n 's/^H2 = //p' "$AWG_CONF" | head -1)"
    H3="$(sed -n 's/^H3 = //p' "$AWG_CONF" | head -1)"
    H4="$(sed -n 's/^H4 = //p' "$AWG_CONF" | head -1)"
    : "${SERVER_PRIVATE_KEY:?не удалось прочитать PrivateKey из ${AWG_CONF} — файл повреждён, удалите его и запустите скрипт заново}"
else
    echo "==> Ключи сервера и параметры обфускации"
    SERVER_PRIVATE_KEY="$(awg genkey)"
    # Случайные различные параметры Jc/Jmin/Jmax/S1/S2/H1-H4 — подбираются
    # один раз при первой установке (не на лету), должны совпасть с [vpn] в
    # config.toml ноды. H1-H4 > 4 и попарно различны, как рекомендует Amnezia.
    _rand16() { od -An -N2 -tu2 /dev/urandom | tr -d ' '; }

    JC=$(( $(_rand16) % 5 + 4 ))
    JMIN=40
    JMAX=70
    S1=$(( $(_rand16) % 100 + 15 ))
    S2=$(( $(_rand16) % 100 + 15 ))
    H1=$(( $(_rand16) + 1000000 ))
    H2=$(( H1 + 1 + $(_rand16) % 1000 ))
    H3=$(( H2 + 1 + $(_rand16) % 1000 ))
    H4=$(( H3 + 1 + $(_rand16) % 1000 ))
fi
SERVER_PUBLIC_KEY="$(printf '%s' "$SERVER_PRIVATE_KEY" | awg pubkey)"

echo "==> Пишу ${AWG_CONF} (интерфейс без пиров — их держит служба vpn из своей БД)"
cat > "$AWG_CONF" <<EOF
[Interface]
Address = ${SERVER_ADDR}
ListenPort = ${LISTEN_PORT}
PrivateKey = ${SERVER_PRIVATE_KEY}
Jc = ${JC}
Jmin = ${JMIN}
Jmax = ${JMAX}
S1 = ${S1}
S2 = ${S2}
H1 = ${H1}
H2 = ${H2}
H3 = ${H3}
H4 = ${H4}
PostUp = iptables -A FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -A FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -A POSTROUTING -s ${SUBNET} -o ${IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -D FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -D POSTROUTING -s ${SUBNET} -o ${IFACE} -j MASQUERADE
EOF
chmod 600 "$AWG_CONF"

echo "==> Включаю IP-форвардинг"
if ! grep -q '^net.ipv4.ip_forward' /etc/sysctl.d/99-sa-home-vpn.conf 2>/dev/null; then
    echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-sa-home-vpn.conf
fi
sysctl -w net.ipv4.ip_forward=1 >/dev/null

echo "==> Поднимаю интерфейс ${WG_IFACE}"
systemctl daemon-reload
systemctl enable --now "awg-quick@${WG_IFACE}"

cat <<EOF

==================================================================
Готово. Проверьте: sudo awg show ${WG_IFACE}

Вставьте в [vpn] config.toml ноды jeeves (ЭТИ ЖЕ числа, дословно):

  interface = "${WG_IFACE}"
  subnet = "${SUBNET}"
  endpoint_host = "<белый IP jeeves>"
  endpoint_port = ${LISTEN_PORT}
  jc = ${JC}
  jmin = ${JMIN}
  jmax = ${JMAX}
  s1 = ${S1}
  s2 = ${S2}
  h1 = ${H1}
  h2 = ${H2}
  h3 = ${H3}
  h4 = ${H4}

Дальше на самой ноде:
  1. Добавить "vpn" в [node].assignments (или кнопкой в боте, право assign@node).
  2. nodectl fix   — поставит узкий sudoers на awg (node/fixups.py::AWG_SUDOERS).
  3. Перезапустить ноду — служба поднимется и сведёт БД с интерфейсом (пусто
     при первом запуске, дальше — по мере выдачи доступа через /vpn).
==================================================================
EOF
