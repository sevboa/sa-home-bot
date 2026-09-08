#!/bin/sh
# Фаза A: быстрый локальный тест туннеля VLESS+Reality без телефона.
# Поднимает xray-клиент с SOCKS-инбаундом, гоняет curl напрямую и через
# туннель, меряет скорость, гасит клиент.
#
#   ./reality-smoke.sh <uuid> [label]
#
# Параметры сервера берутся из ~/.config/sa-home-reality-client.env
# (HOST/PORT/PBK/SID/SNI). Нужен бинарь xray: ищется в PATH, иначе качается
# с wooster (scp wooster:/usr/local/bin/xray). Запускать из корня репозитория.

set -eu

UUID="${1:?использование: $0 <uuid> [label]}"
LABEL="${2:-smoke}"
ENV_FILE="$HOME/.config/sa-home-reality-client.env"
SOCKS_PORT=10808
REPO="$(cd "$(dirname "$0")/.." && pwd)"

[ -f "$ENV_FILE" ] || { echo "нет $ENV_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
: "${HOST:?}" "${PORT:?}" "${PBK:?}" "${SID:?}" "${SNI:?}"

WORK="$(mktemp -d)"
trap 'kill "${XPID:-}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

XRAY="$(command -v xray || true)"
if [ -z "$XRAY" ]; then
    echo "xray не в PATH — качаю с wooster"
    scp -q wooster:/usr/local/bin/xray "$WORK/xray"
    chmod +x "$WORK/xray"
    XRAY="$WORK/xray"
fi

cat > "$WORK/client.json" <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    { "tag": "socks", "listen": "127.0.0.1", "port": ${SOCKS_PORT},
      "protocol": "socks", "settings": { "udp": true } }
  ],
  "outbounds": [
    { "tag": "proxy", "protocol": "vless",
      "settings": { "vnext": [ { "address": "${HOST}", "port": ${PORT},
        "users": [ { "id": "${UUID}", "flow": "xtls-rprx-vision", "encryption": "none" } ] } ] },
      "streamSettings": { "network": "tcp", "security": "reality",
        "realitySettings": { "serverName": "${SNI}", "fingerprint": "chrome",
          "publicKey": "${PBK}", "shortId": "${SID}" } } }
  ]
}
EOF

"$XRAY" run -test -c "$WORK/client.json" >/dev/null
"$XRAY" run -c "$WORK/client.json" >"$WORK/xray.log" 2>&1 &
XPID=$!
sleep 3

S="--socks5-hostname 127.0.0.1:${SOCKS_PORT}"
echo "клиент  : $LABEL ($UUID)"
echo -n "напрямую: "; curl -sS --max-time 10 https://api.ipify.org || echo "(нет ответа)"; echo
echo -n "туннель : "; TUN_IP="$(curl -sS --max-time 15 $S https://api.ipify.org || true)"; echo "$TUN_IP"
echo -n "скорость: "; curl -sS --max-time 30 $S -o /dev/null \
    -w 'down %{speed_download} B/s за %{time_total}s\n' \
    "https://speed.cloudflare.com/__down?bytes=10000000" || echo "(не докачал — путь КЗ↔США бывает нестабилен, IP-проверка ниже важнее)"

if [ "$TUN_IP" = "$HOST" ]; then
    echo "OK: выход через ${HOST}"
else
    echo "ВНИМАНИЕ: выходной IP через туннель ($TUN_IP) != сервер ($HOST)"
    echo "--- xray.log ---"; cat "$WORK/xray.log"
    exit 1
fi
