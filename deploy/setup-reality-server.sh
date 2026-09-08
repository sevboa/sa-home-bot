#!/bin/sh
# Разовая установка VLESS+Reality сервера (xray-core) на VPS роя.
# Этап 39.0.x IMPLEMENTATION_PLAN.md, Фаза A (ручной прототип).
#
# Запускать вручную по SSH на самом VPS, под пользователем с sudo (НЕ как часть
# деплоя пакета sa-home-bot). В Фазе A клиентов держит сам config.json xray
# (добавляются deploy/reality-add-client.sh); в Фазе B — служба reality через
# gRPC API xray без рестарта.
#
# Использование:
#   sudo ./setup-reality-server.sh [порт-TCP=8443] [dest=www.microsoft.com:443]
# например:
#   sudo ./setup-reality-server.sh 8443
#
# Порт: 8443/tcp (443/tcp на wooster занят mtg). > 1024 → setcap не нужен.
# dest/SNI — реальный сторонний HTTPS-сайт с TLS 1.3 + X25519, достижимый из РФ.
#   ВАЖНО: у Reality жёсткий лимит буфера рукопожатия 8192 байта. Сайты с
#   толстой цепочкой сертификатов (www.microsoft.com за Akamai — Certificate
#   ~8.3 КБ) НЕ работают: "handshake did not complete successfully", хотя
#   auth Reality проходит. Проверено 2026-09-07. Годятся www.google.com
#   (~3.8 КБ), dl.google.com, www.cloudflare.com. Дефолт — www.google.com.
#
# Reality НЕ требует NAT/forward/ip_forward (в отличие от AmneziaWG): vless+reality
# inbound + freedom outbound, egress по обычной маршрутизации VPS.
#
# Идемпотентность: повторный запуск переставит бинарь xray если версия иная, но
# ключи Reality и short_id из /etc/sa-home-reality/reality.env СОХРАНЯЮТСЯ — их
# смена разом отвяжет все выданные клиентские конфиги. Чтобы сменить намеренно —
# удалите reality.env и перезапустите скрипт, предупредив клиентов.
#
# После прогона печатает блок [reality] для config.toml ноды (Фаза B).

set -eu

XRAY_VERSION="v26.3.27"
XRAY_ZIP_URL="https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip"

if [ "$(id -u)" -ne 0 ]; then
    echo "Запускать через sudo (нужен root для установки бинаря/ufw)." >&2
    exit 1
fi

PORT="${1:-8443}"
DEST="${2:-www.google.com:443}"
SNI="${DEST%%:*}"
API_PORT=10085
KEY_DIR="/etc/sa-home-reality"
KEY_ENV="${KEY_DIR}/reality.env"

RUN_USER="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
: "${USER_HOME:?не удалось определить домашний каталог пользователя ${RUN_USER}}"
XRAY_CONF="${USER_HOME}/.config/xray/config.json"
XRAY_UNIT="${USER_HOME}/.config/systemd/user/xray.service"

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

# --- бинарь xray ---
echo "==> Проверка xray"
if ! xray version 2>/dev/null | head -1 | grep -q "${XRAY_VERSION#v}"; then
    command -v unzip >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y unzip; }
    echo "    качаю xray-core ${XRAY_VERSION}"
    curl -fsSL "$XRAY_ZIP_URL" -o "$BUILD_DIR/xray.zip"
    curl -fsSL "${XRAY_ZIP_URL}.dgst" -o "$BUILD_DIR/xray.dgst"
    want="$(awk -F'= ' '/^SHA2-256=/{print $2}' "$BUILD_DIR/xray.dgst")"
    got="$(sha256sum "$BUILD_DIR/xray.zip" | awk '{print $1}')"
    : "${want:?не удалось прочитать SHA2-256 из .dgst}"
    if [ "$want" != "$got" ]; then
        echo "!! sha256 архива xray не совпал: ждали $want, получили $got" >&2
        exit 1
    fi
    unzip -o "$BUILD_DIR/xray.zip" xray -d "$BUILD_DIR" >/dev/null
    install -m 0755 "$BUILD_DIR/xray" /usr/local/bin/xray
    echo "    установлен $(xray version | head -1)"
else
    echo "    уже $(xray version | head -1)"
fi

# --- ключи Reality (один раз) ---
umask 077
mkdir -p "$KEY_DIR"
if [ -f "$KEY_ENV" ]; then
    echo "==> ${KEY_ENV} уже есть — беру ключи из него"
    # shellcheck disable=SC1090
    . "$KEY_ENV"
else
    echo "==> Генерирую ключи Reality (один раз)"
    X_OUT="$(xray x25519)"
    # Формат вывода xray x25519 менялся между версиями — парсим защитно:
    #   v26.x:  "PrivateKey: ..."  /  "Password (PublicKey): ..."
    #   старее: "Private key: ..." /  "Public key: ..."
    REALITY_PRIVATE_KEY="$(printf '%s\n' "$X_OUT" \
        | sed -n 's/^Private[Kk]ey:[[:space:]]*//p; s/^Private key:[[:space:]]*//p' | head -1)"
    REALITY_PUBLIC_KEY="$(printf '%s\n' "$X_OUT" \
        | sed -n 's/^Password (PublicKey):[[:space:]]*//p; s/^Password:[[:space:]]*//p; s/^Public key:[[:space:]]*//p' \
        | head -1)"
    REALITY_SHORT_ID="$(openssl rand -hex 8)"
    : "${REALITY_PRIVATE_KEY:?не распарсил приватный ключ из вывода xray x25519 — поправьте sed в скрипте}"
    : "${REALITY_PUBLIC_KEY:?не распарсил публичный ключ из вывода xray x25519 — поправьте sed в скрипте}"
    {
        echo "REALITY_PRIVATE_KEY=${REALITY_PRIVATE_KEY}"
        echo "REALITY_PUBLIC_KEY=${REALITY_PUBLIC_KEY}"
        echo "REALITY_SHORT_ID=${REALITY_SHORT_ID}"
    } > "$KEY_ENV"
    chmod 600 "$KEY_ENV"
fi

# Каталоги — заранее и от пользователя, чтобы install -D не создал их root-owned.
sudo -u "$RUN_USER" mkdir -p "$(dirname "$XRAY_CONF")" "$(dirname "$XRAY_UNIT")"

# --- конфиг xray (user-owned, служба бота в Фазе B правит его без sudo) ---
echo "==> Пишу ${XRAY_CONF}"
cat > "$BUILD_DIR/config.json" <<EOF
{
  "log": { "loglevel": "warning" },
  "api": { "tag": "api", "services": ["HandlerService", "StatsService"] },
  "stats": {},
  "policy": {
    "levels": { "0": { "statsUserUplink": true, "statsUserDownlink": true } },
    "system": { "statsInboundUplink": true, "statsInboundDownlink": true }
  },
  "inbounds": [
    {
      "tag": "reality-in",
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${DEST}",
          "xver": 0,
          "serverNames": ["${SNI}"],
          "privateKey": "${REALITY_PRIVATE_KEY}",
          "shortIds": ["${REALITY_SHORT_ID}"]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    },
    {
      "tag": "api-in",
      "listen": "127.0.0.1",
      "port": ${API_PORT},
      "protocol": "dokodemo-door",
      "settings": { "address": "127.0.0.1" }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "rules": [
      { "type": "field", "inboundTag": ["api-in"], "outboundTag": "api" }
    ]
  }
}
EOF
# Идемпотентность: если конфиг уже был — перенести существующих клиентов и
# (если dest не задан явным 2-м аргументом) сохранить прежний dest/serverNames.
if [ -f "$XRAY_CONF" ]; then
    KEEP_DEST=0
    [ -n "${2:-}" ] || KEEP_DEST=1
    OLD="$XRAY_CONF" NEW="$BUILD_DIR/config.json" KEEP_DEST="$KEEP_DEST" python3 - <<'PY'
import json, os
old = json.load(open(os.environ["OLD"], encoding="utf-8"))
new = json.load(open(os.environ["NEW"], encoding="utf-8"))
o_rs = old["inbounds"][0]["streamSettings"]["realitySettings"]
n_in = new["inbounds"][0]
n_in["settings"]["clients"] = old["inbounds"][0]["settings"].get("clients", [])
if os.environ["KEEP_DEST"] == "1":
    for k in ("dest", "serverNames"):
        if k in o_rs:
            n_in["streamSettings"]["realitySettings"][k] = o_rs[k]
json.dump(new, open(os.environ["NEW"], "w", encoding="utf-8"), indent=2)
PY
    echo "    перенесено клиентов: $(python3 -c 'import json,os;print(len(json.load(open(os.environ["F"]))["inbounds"][0]["settings"]["clients"]))' F="$BUILD_DIR/config.json")"
fi

if ! xray run -test -c "$BUILD_DIR/config.json" >/dev/null 2>&1; then
    echo "!! xray не принял сгенерированный config.json:" >&2
    xray run -test -c "$BUILD_DIR/config.json" >&2 || true
    exit 1
fi
install -D -m 600 -o "$RUN_USER" -g "$RUN_USER" "$BUILD_DIR/config.json" "$XRAY_CONF"

# --- user-юнит systemd ---
echo "==> Пишу ${XRAY_UNIT}"
cat > "$BUILD_DIR/xray.service" <<'EOF'
[Unit]
Description=xray-core (VLESS+Reality)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/xray run -c %h/.config/xray/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
install -D -m 644 -o "$RUN_USER" -g "$RUN_USER" "$BUILD_DIR/xray.service" "$XRAY_UNIT"

# --- ufw ---
if command -v ufw >/dev/null 2>&1 && LC_ALL=C ufw status 2>/dev/null | grep -qi "Status: active"; then
    echo "==> ufw allow ${PORT}/tcp"
    ufw allow "${PORT}/tcp" >/dev/null
fi

# --- запуск от пользователя (linger должен быть включён: loginctl enable-linger) ---
echo "==> Запускаю xray.service от ${RUN_USER}"
RT="/run/user/$(id -u "$RUN_USER")"
uctl() {
    sudo -u "$RUN_USER" env XDG_RUNTIME_DIR="$RT" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$RT/bus" systemctl --user "$@"
}
if uctl daemon-reload 2>/dev/null; then
    uctl enable --now xray.service
    uctl --no-pager --lines=5 status xray.service || true
else
    cat <<EOF
  ! systemctl --user от ${RUN_USER} недоступен из этого контекста.
    Выполните сами под ${RUN_USER} (нужен включённый linger):
      sudo loginctl enable-linger ${RUN_USER}
      systemctl --user daemon-reload
      systemctl --user enable --now xray.service
EOF
fi

cat <<EOF

==================================================================
Готово. Проверьте:
  ss -tlnp | grep ${PORT}
  systemctl --user status xray.service
  xray api statsquery --server 127.0.0.1:${API_PORT}   # ответит после старта

Клиенты (Фаза A): ./reality-add-client.sh <label>  — от пользователя ${RUN_USER}.

Блок [reality] для config.toml ноды (Фаза B, значения дословно):

  [reality]
  endpoint_host = "<белый IP этого VPS>"
  port = ${PORT}
  server_public_key = "${REALITY_PUBLIC_KEY}"
  short_id = "${REALITY_SHORT_ID}"
  sni = "${SNI}"
  flow = "xtls-rprx-vision"
  api_addr = "127.0.0.1:${API_PORT}"
==================================================================
EOF
