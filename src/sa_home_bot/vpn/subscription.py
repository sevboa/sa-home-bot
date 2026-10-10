"""Подписка Hiddify на устройство: токен, сборка ответа, страница (подэтап 57.10).

Модуль чистый — без сети и БД. Токен не содержит chat_id и не хранится:
``HMAC-SHA256(секрет, "chat_id\\0device_label")``, первые 24 байта в base64url
(32 символа). Любая vpn-нода пересчитывает его по своим активным VLESS-ключам и
находит владельца, общая БД не нужна. Секрет — ``[vpn].sub_secret``, а если пуст,
то производная от ``[swarm].token`` (он один на весь рой и входит в бэкап
identity ноды, поэтому ссылки переживают переустановку).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
from dataclasses import dataclass
from urllib.parse import quote

from sa_home_bot.reality.client_config import (
    RealityParams,
    render_singbox_multi,
    render_vless_url,
)

TOKEN_LEN = 32
_TOKEN_RE = re.compile(rf"^[A-Za-z0-9_-]{{{TOKEN_LEN}}}$")
_SECRET_CONTEXT = b"sa-home-bot/vpn-subscription/v1"

FORMAT_VLESS = "vless"  # base64-список vless:// (по умолчанию)
FORMAT_PLAIN = "plain"  # тот же список без base64
FORMAT_SINGBOX = "singbox"  # sing-box JSON с несколькими outbound
FORMATS = (FORMAT_VLESS, FORMAT_PLAIN, FORMAT_SINGBOX)


def derive_secret(sub_secret: str, swarm_token: str) -> bytes:
    """Ключ подписи. Явный ``sub_secret`` главнее; иначе — от токена роя."""
    base = (sub_secret or swarm_token or "").encode()
    return hmac.new(base, _SECRET_CONTEXT, hashlib.sha256).digest()


def make_token(secret: bytes, chat_id: int, device_label: str) -> str:
    mac = hmac.new(secret, f"{chat_id}\0{device_label}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode().rstrip("=")


def token_matches(secret: bytes, token: str, chat_id: int, device_label: str) -> bool:
    return hmac.compare_digest(make_token(secret, chat_id, device_label), token)


def valid_token_shape(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


@dataclass(frozen=True)
class SubEntry:
    """Одна страна устройства: сервер и UUID клиента."""

    node: str
    name: str  # «🇳🇱 Нидерланды»
    uuid: str
    params: RealityParams

    @property
    def url(self) -> str:
        return render_vless_url(self.params, self.uuid, self.name)

    def to_wire(self) -> dict:
        p = self.params
        return {
            "node": self.node,
            "name": self.name,
            "uuid": self.uuid,
            "params": {
                "endpoint_host": p.endpoint_host,
                "port": p.port,
                "server_public_key": p.server_public_key,
                "short_id": p.short_id,
                "sni": p.sni,
                "flow": p.flow,
            },
        }

    @classmethod
    def from_wire(cls, raw: dict) -> SubEntry:
        p = raw["params"]
        return cls(
            node=str(raw["node"]),
            name=str(raw["name"]),
            uuid=str(raw["uuid"]),
            params=RealityParams(
                endpoint_host=str(p["endpoint_host"]),
                port=int(p["port"]),
                server_public_key=str(p["server_public_key"]),
                short_id=str(p["short_id"]),
                sni=str(p["sni"]),
                flow=str(p.get("flow") or "xtls-rprx-vision"),
            ),
        )


@dataclass(frozen=True)
class Subscription:
    """Собранная подписка устройства со всех нод."""

    device_label: str
    entries: tuple[SubEntry, ...]
    used_bytes: int = 0
    total_bytes: int = 0
    expire_ts: int = 0


def sort_entries(entries: list[SubEntry]) -> list[SubEntry]:
    return sorted(entries, key=lambda e: (e.name, e.node))


def render_body(sub: Subscription, fmt: str) -> tuple[str, str]:
    """``(тело, content-type)`` подписки в выбранном формате."""
    if fmt == FORMAT_SINGBOX:
        text = render_singbox_multi([(e.name, e.params, e.uuid) for e in sub.entries])
        return text, "application/json; charset=utf-8"
    plain = "\n".join(e.url for e in sub.entries) + "\n"
    if fmt == FORMAT_PLAIN:
        return plain, "text/plain; charset=utf-8"
    return base64.b64encode(plain.encode()).decode() + "\n", "text/plain; charset=utf-8"


def profile_title(device_label: str) -> str:
    return f"VPN · {device_label}"


def _b64_header(text: str) -> str:
    return "base64:" + base64.b64encode(text.encode()).decode()


def render_headers(
    sub: Subscription,
    *,
    page_url: str,
    update_interval_h: int,
    support_url: str = "",
    fmt: str = FORMAT_VLESS,
) -> dict[str, str]:
    """Заголовки профиля для Hiddify. Заголовки HTTP — latin-1, поэтому
    название (с эмодзи и кириллицей) уходит в ``base64:…``."""
    title = profile_title(sub.device_label)
    headers = {
        "profile-title": _b64_header(title),
        "profile-update-interval": str(update_interval_h),
        "subscription-userinfo": (
            f"upload=0; download={max(sub.used_bytes, 0)}; "
            f"total={max(sub.total_bytes, 0)}; expire={max(sub.expire_ts, 0)}"
        ),
        "profile-web-page-url": page_url,
        "content-disposition": f"attachment; filename*=UTF-8''{quote(title, safe='')}",
        "cache-control": "no-store",
        "x-robots-tag": "noindex, nofollow",
        "referrer-policy": "no-referrer",
    }
    if support_url:
        headers["support-url"] = support_url
    return headers


def deep_link(sub_url: str, device_label: str) -> str:
    """``hiddify://import/<адрес подписки>`` без ``#имени``: Hiddify показывает
    фрагмент как есть, не раскодируя проценты («VPN%20%C2%B7…»), а имя с
    эмодзи и кириллицей берёт из заголовка ``profile-title`` (base64)."""
    return f"hiddify://import/{sub_url}"


def render_page(
    sub: Subscription,
    *,
    sub_url: str,
    qr_data_uri: str,
    ios_url: str,
    android_url: str,
    site_url: str,
) -> str:
    """Страница подписки: одна кнопка «Открыть в Hiddify», запасной путь —
    копирование ссылки. Русский, на «вы», без пола; вёрстка под телефон."""
    e = html.escape
    countries = "".join(f"<li>{e(entry.name)}</li>" for entry in sub.entries)
    link = deep_link(sub_url, sub.device_label)
    page_title = e(profile_title(sub.device_label))
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>{page_title}</title>
<style>
:root {{ --bg:#f5f6f8; --card:#fff; --fg:#14181f; --muted:#5b6472;
  --accent:#2f6bff; --line:#e1e4ea; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#10131a; --card:#181d27; --fg:#eef1f6; --muted:#98a2b3;
    --accent:#5b8cff; --line:#2a3140; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:17px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:480px; margin:0 auto; padding:24px 16px 40px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
.sub {{ color:var(--muted); margin:0 0 20px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
  padding:16px; margin-bottom:16px; }}
ul {{ margin:6px 0 0; padding-left:20px; }}
.btn {{ display:block; width:100%; text-align:center; text-decoration:none; border:0;
  border-radius:14px; padding:18px 12px; font-size:19px; font-weight:600;
  background:var(--accent); color:#fff; cursor:pointer; }}
.btn.alt {{ background:transparent; color:var(--accent); border:2px solid var(--accent);
  font-size:17px; padding:14px 12px; margin-top:12px; }}
.hint {{ color:var(--muted); font-size:15px; margin:12px 0 0; }}
.qr {{ text-align:center; }}
.qr img {{ width:220px; height:220px; max-width:100%; background:#fff; padding:8px;
  border-radius:12px; }}
.links a {{ color:var(--accent); display:inline-block; margin:4px 12px 4px 0; }}
input {{ width:100%; padding:10px; border-radius:10px; border:1px solid var(--line);
  background:var(--bg); color:var(--fg); font:14px monospace; margin-top:12px; }}
</style>
</head>
<body>
<main>
<h1>{page_title}</h1>
<p class="sub">Подключение к VPN</p>
<div class="card">
<a class="btn" href="{e(link, quote=True)}">Открыть в Hiddify</a>
<p class="hint">Не открывается? Откройте эту страницу в браузере:
&#8942; &rarr; Открыть в браузере.</p>
<button class="btn alt" id="copy" type="button">Скопировать ссылку</button>
<input id="url" readonly value="{e(sub_url, quote=True)}" aria-label="Ссылка подписки">
<p class="hint" id="copied" hidden>Ссылка скопирована. В Hiddify: «+» &rarr;
«Добавить из буфера обмена».</p>
</div>
<div class="card">
<strong>В подписке:</strong>
<ul>{countries}</ul>
</div>
<div class="card qr">
<p style="margin-top:0">Другое устройство? Наведите камеру:</p>
<img src="{e(qr_data_uri, quote=True)}" alt="QR-код ссылки подписки">
</div>
<div class="card links">
<strong>Нет приложения Hiddify?</strong><br>
<a href="{e(ios_url, quote=True)}">App Store</a>
<a href="{e(android_url, quote=True)}">Google Play</a>
<a href="{e(site_url, quote=True)}">Сайт</a>
</div>
</main>
<script>
(function () {{
  var btn = document.getElementById('copy'), box = document.getElementById('url');
  var note = document.getElementById('copied');
  function done() {{ note.hidden = false; }}
  btn.addEventListener('click', function () {{
    if (navigator.clipboard && window.isSecureContext) {{
      navigator.clipboard.writeText(box.value).then(done, fallback);
    }} else {{ fallback(); }}
  }});
  function fallback() {{
    box.focus(); box.select(); box.setSelectionRange(0, box.value.length);
    try {{ if (document.execCommand('copy')) done(); }} catch (e) {{}}
  }}
}})();
</script>
</body>
</html>
"""


def qr_data_uri(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_data_uri(scale=6, border=2)
