#!/bin/sh
# Фаза A: добавить тестового клиента VLESS+Reality в running xray на этом VPS.
# Запускать под тем же пользователем, что владеет ~/.config/xray/config.json
# (без sudo). Аргумент — метка устройства (email клиента в терминах xray).
#
#   ./reality-add-client.sh phone
#
# Печатает UUID и метку — их скармливаем deploy/reality-client.py на alfred,
# чтобы собрать <label>.json / vless:// / QR для гостя.
#
# В Фазе B клиентов заводит служба reality через `xray api adu` без рестарта —
# этот скрипт нужен только пока сервер держит клиентов прямо в config.json.

set -eu

LABEL="${1:?использование: $0 <метка-устройства>}"
CONF="${XRAY_CONF:-$HOME/.config/xray/config.json}"
FLOW="xtls-rprx-vision"

[ -f "$CONF" ] || { echo "нет $CONF — сначала прогоните setup-reality-server.sh" >&2; exit 1; }

UUID="$(cat /proc/sys/kernel/random/uuid)"

CONF="$CONF" LABEL="$LABEL" UUID="$UUID" FLOW="$FLOW" python3 - <<'PY'
import json, os, sys

path, label, uuid, flow = os.environ["CONF"], os.environ["LABEL"], os.environ["UUID"], os.environ["FLOW"]
with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

inbounds = [i for i in cfg.get("inbounds", []) if i.get("protocol") == "vless"]
if not inbounds:
    sys.exit("в config.json нет vless inbound")
clients = inbounds[0].setdefault("settings", {}).setdefault("clients", [])
clients[:] = [c for c in clients if c.get("email") != label]  # идемпотентность: заменяем
clients.append({"id": uuid, "flow": flow, "email": label, "level": 0})

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(tmp, path)
PY

xray run -test -c "$CONF" >/dev/null 2>&1 || { echo "!! xray не принял конфиг после правки — откатите $CONF" >&2; exit 1; }

RT="/run/user/$(id -u)"
XDG_RUNTIME_DIR="$RT" DBUS_SESSION_BUS_ADDRESS="unix:path=$RT/bus" \
    systemctl --user restart xray.service

echo "Клиент добавлен и xray перезапущен."
echo "  label = $LABEL"
echo "  uuid  = $UUID"
