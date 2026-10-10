#!/bin/sh
# Сертификат Let's Encrypt на публичный IP для страницы подписки Hiddify (57.10).
#
# Запуск на VPN-ноде под её пользователем (нужен беспарольный sudo):
#   sh sub-cert.sh <публичный-IP> [порт-страницы, по умолчанию 8444]
#
# Что делает (идемпотентно):
#  1. открывает tcp <порт> и tcp 80 (http-01) в firewall: ufw, либо nftables
#     (live-правило + строка в /etc/nftables.conf, полный `nft -f` НЕ делается —
#     он стёр бы таблицы tailscale/awg, см. память jeeves-nftables-forward-hook-trap);
#  2. ставит acme.sh 3.1.6 в ~/.acme.sh (под root идёт только сам выпуск);
#  3. выпускает сертификат на IP (профиль shortlived, ~6 суток) и кладёт
#     ~/.config/sa-home-bot/sub-tls/{fullchain,privkey}.pem;
#  4. ставит systemd-таймер продления (дважды в сутки). Служба vpn сама
#     перечитывает файлы (раз в 10 минут) и сама переключается с http на https.
set -eu
# неинтерактивный ssh не содержит /usr/sbin в PATH (ufw, nft)
PATH="$PATH:/usr/sbin:/sbin"

IP="${1:?нужен публичный IP}"
PORT="${2:-8444}"
ME="$(id -un)"
ACME_HOME="$HOME/.acme.sh"
TLS_DIR="$HOME/.config/sa-home-bot/sub-tls"
ACME_VER="3.1.6"

mkdir -p "$TLS_DIR" "$ACME_HOME"
chmod 700 "$TLS_DIR"

# --- 1. firewall ---
if command -v ufw >/dev/null 2>&1 && sudo -n ufw status | head -1 | grep -qi "active\|активен" \
   && ! sudo -n ufw status | head -1 | grep -qi "inactive\|неактивен"; then
    sudo -n ufw allow "$PORT/tcp" comment "sa-home vpn: подписка Hiddify"
    sudo -n ufw allow 80/tcp comment "sa-home vpn: http-01 для сертификата"
elif sudo -n nft list table inet filter >/dev/null 2>&1; then
    for p in "$PORT" 80; do
        if ! sudo -n nft list chain inet filter input | grep -q "tcp dport $p accept"; then
            sudo -n nft add rule inet filter input tcp dport "$p" accept
        fi
        if ! sudo -n grep -q "tcp dport $p accept" /etc/nftables.conf; then
            # рядом со строкой 8443 в цепочке input; проверка синтаксиса до замены
            tmp="$(mktemp)"
            sudo -n awk -v p="$p" '{print} /tcp dport 8443 accept/ && !d {print "\t\ttcp dport " p " accept"; d=1}' \
                /etc/nftables.conf > "$tmp"
            sudo -n nft -c -f "$tmp"
            sudo -n cp "$tmp" /etc/nftables.conf
            rm -f "$tmp"
        fi
    done
else
    echo "firewall не распознан — откройте tcp $PORT и tcp 80 вручную" >&2
fi

# --- 2. acme.sh ---
if [ ! -x "$ACME_HOME/acme.sh" ]; then
    curl -fsSL "https://raw.githubusercontent.com/acmesh-official/acme.sh/$ACME_VER/acme.sh" \
        -o "$ACME_HOME/acme.sh"
    chmod 755 "$ACME_HOME/acme.sh"
fi

# --- 3. выпуск ---
sudo -n "$ACME_HOME/acme.sh" --home "$ACME_HOME" --set-default-ca --server letsencrypt >/dev/null
sudo -n "$ACME_HOME/acme.sh" --home "$ACME_HOME" --issue -d "$IP" --standalone --httpport 80 \
    --server letsencrypt --certificate-profile shortlived --days 3 || rc=$?
# rc=2 у acme.sh — «пропущено, ещё не пора продлевать»: не ошибка.
if [ "${rc:-0}" != 0 ] && [ "${rc:-0}" != 2 ]; then
    echo "выпуск не удался (код ${rc})" >&2
    exit 1
fi
sudo -n "$ACME_HOME/acme.sh" --home "$ACME_HOME" --install-cert -d "$IP" \
    --fullchain-file "$TLS_DIR/fullchain.pem" --key-file "$TLS_DIR/privkey.pem" \
    --reloadcmd "chown $ME:$ME $TLS_DIR/fullchain.pem $TLS_DIR/privkey.pem && chmod 600 $TLS_DIR/privkey.pem"

# --- 4. таймер продления ---
sudo -n tee /etc/systemd/system/sa-home-sub-cert.service >/dev/null <<UNIT
[Unit]
Description=Продление сертификата страницы подписки Hiddify (acme.sh)
After=network-online.target

[Service]
Type=oneshot
ExecStart=$ACME_HOME/acme.sh --cron --home $ACME_HOME
UNIT
sudo -n tee /etc/systemd/system/sa-home-sub-cert.timer >/dev/null <<UNIT
[Unit]
Description=Продление сертификата подписки Hiddify

[Timer]
OnCalendar=*-*-* 00,12:17:00
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now sa-home-sub-cert.timer
echo "готово: $TLS_DIR"
