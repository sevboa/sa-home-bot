#!/usr/bin/env python3
"""Фаза A: собрать клиентские артефакты VLESS+Reality для одного устройства.

Запускать из репозитория (обычно на alfred). Параметры сервера берутся из
блока [reality], который напечатал deploy/setup-reality-server.sh, плюс UUID
от deploy/reality-add-client.sh.

    python3 deploy/reality-client.py --label phone --uuid <UUID> \
        --host <IP> --port 8443 --pbk <PUBKEY> --sid <SHORT_ID> \
        --sni www.microsoft.com [--all-proxy] [--qr] [--out-dir .]

Часто повторяемые флаги можно один раз положить в
~/.config/sa-home-reality-client.env (строки KEY=VALUE: HOST, PORT, PBK, SID, SNI).

Пишет <label>.json (основной артефакт для Hiddify: «+» → «Из файла»),
<label>.txt (vless:// в 1-й строке, hiddify:// deep-link во 2-й) и, с --qr,
<label>.png (QR по vless://). Deep-link печатает в stdout — его вставляем гостю
в чат: одно нажатие открывает Hiddify и импортирует профиль.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sa_home_bot.reality.client_config import (  # noqa: E402
    RealityParams,
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)

ENV_PATH = Path.home() / ".config" / "sa-home-reality-client.env"


def _load_env_defaults() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip().upper()] = value.strip().strip("'\"")
    return out


def main() -> int:
    env = _load_env_defaults()
    p = argparse.ArgumentParser(description="собрать клиент VLESS+Reality (Фаза A)")
    p.add_argument("--label", required=True, help="имя устройства (имя файла и профиля)")
    p.add_argument("--uuid", required=True, help="UUID клиента (из reality-add-client.sh)")
    p.add_argument("--host", default=env.get("HOST"), help="белый IP сервера Reality")
    p.add_argument("--port", type=int, default=int(env.get("PORT", "8443")))
    p.add_argument("--pbk", default=env.get("PBK"), help="server_public_key")
    p.add_argument("--sid", default=env.get("SID"), help="short_id")
    p.add_argument("--sni", default=env.get("SNI", "www.google.com"))
    p.add_argument("--flow", default=env.get("FLOW", "xtls-rprx-vision"))
    p.add_argument(
        "--all-proxy",
        action="store_true",
        help="одна точка выхода (всё в туннель, кроме банков) — для гостей вне РФ",
    )
    p.add_argument("--qr", action="store_true", help="сохранить <label>.png с QR по vless://")
    p.add_argument("--out-dir", default=".", type=Path)
    args = p.parse_args()

    missing = [n for n in ("host", "pbk", "sid") if not getattr(args, n)]
    if missing:
        p.error(f"не заданы: {', '.join('--' + m for m in missing)} (флагом или в {ENV_PATH})")

    params = RealityParams(
        endpoint_host=args.host,
        port=args.port,
        server_public_key=args.pbk,
        short_id=args.sid,
        sni=args.sni,
        flow=args.flow,
    )
    config_text = render_singbox_config(params, args.uuid, all_proxy=args.all_proxy)
    vless_url = render_vless_url(params, args.uuid, args.label)
    deep_link = render_deep_link(vless_url)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.label}.json"
    txt_path = out_dir / f"{args.label}.txt"
    json_path.write_text(config_text, encoding="utf-8")
    txt_path.write_text(f"{vless_url}\n{deep_link}\n", encoding="utf-8")
    written = [json_path, txt_path]

    if args.qr:
        try:
            import segno
        except ImportError:
            print("!! segno не установлен — QR пропущен (pip install segno)", file=sys.stderr)
        else:
            png_path = out_dir / f"{args.label}.png"
            segno.make(vless_url, error="m").save(str(png_path), scale=5, border=2)
            written.append(png_path)

    for path in written:
        print(f"написан {path}")
    print()
    print("deep-link для чата гостю:")
    print(deep_link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
