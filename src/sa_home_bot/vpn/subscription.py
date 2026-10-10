# ruff: noqa: E501
"""Подписка Hiddify на устройство: токен, сборка ответа, страница (подэтап 57.10).

Модуль чистый — без сети и БД. Токен не содержит chat_id и не хранится:
4 байта «поколения» (unix-время последнего перевыпуска/отзыва VLESS-ключа
устройства на любой ноде) + 20 байт ``HMAC-SHA256(секрет, "chat_id\\0label\\0поколение")``,
всё в base64url (32 символа). Любая vpn-нода пересчитывает MAC по своим
активным VLESS-ключам и находит владельца, общая БД не нужна. Токен со старым
поколением нода отвергает, если хоть одна нода знает более новое — так
перевыпуск гасит утёкшую ссылку (vpn/service.py::resolve_subscription). Секрет — ``[vpn].sub_secret``, а если пуст,
то производная от ``[swarm].token`` (он один на весь рой и входит в бэкап
identity ноды, поэтому ссылки переживают переустановку).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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


def make_token(secret: bytes, chat_id: int, device_label: str, gen: int = 0) -> str:
    gen = max(0, min(int(gen), 0xFFFFFFFF))
    mac = hmac.new(secret, f"{chat_id}\0{device_label}\0{gen}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(gen.to_bytes(4, "big") + mac[:20]).decode().rstrip("=")


def token_gen(token: str) -> int | None:
    """Поколение из токена (без проверки подписи) или ``None``, если токен кривой."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None
    return int.from_bytes(raw[:4], "big") if len(raw) == 24 else None


def token_matches(secret: bytes, token: str, chat_id: int, device_label: str) -> bool:
    gen = token_gen(token)
    if gen is None:
        return False
    return hmac.compare_digest(make_token(secret, chat_id, device_label, gen), token)


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
class NodeInfo:
    """Нода роя, как её видит страница: публичный адрес (для «VPN включён?»),
    здоровье VLESS по проверкам, выдаёт ли AmneziaWG и есть ли уже ключ."""

    node: str
    name: str
    ip: str
    local: bool = False
    health: str = ""  # "ok" / "bad" / "" — нет данных
    awg: bool = False
    awg_key: bool = False

    def to_wire(self) -> dict:
        return {
            "node": self.node,
            "name": self.name,
            "ip": self.ip,
            "health": self.health,
            "awg": self.awg,
            "awg_key": self.awg_key,
        }

    @classmethod
    def from_wire(cls, raw: dict, *, local: bool = False) -> NodeInfo:
        return cls(
            node=str(raw["node"]),
            name=str(raw.get("name") or raw["node"]),
            ip=str(raw.get("ip") or ""),
            local=local,
            health=str(raw.get("health") or ""),
            awg=bool(raw.get("awg")),
            awg_key=bool(raw.get("awg_key")),
        )


@dataclass(frozen=True)
class Subscription:
    """Собранная подписка устройства со всех нод."""

    device_label: str
    entries: tuple[SubEntry, ...]
    used_bytes: int = 0
    total_bytes: int = 0
    expire_ts: int = 0
    chat_id: int = 0
    nodes: tuple[NodeInfo, ...] = ()


@dataclass(frozen=True)
class VpnStatus:
    on: bool
    name: str = ""  # «🇳🇱 Нидерланды»
    method: str = ""  # «AmneziaWG» / «VLESS · Hiddify» / "" — не определить надёжно


def _norm_ip(text: str):
    try:
        ip = ipaddress.ip_address(text.strip().split("%")[0])
    except ValueError:
        return None
    return ip.ipv4_mapped if getattr(ip, "ipv4_mapped", None) else ip


def detect_status(addr: str, nodes: tuple[NodeInfo, ...], local_subnet: str) -> VpnStatus:
    """Включён ли VPN у того, кто открыл страницу, — по адресу запроса. Из
    awg-подсети СВОЕЙ ноды — AmneziaWG (трафик к собственному адресу не
    маскарадится); с публичного адреса своей ноды — VLESS (xray ходит наружу
    с хоста); с адреса соседней ноды — только страна. Положение человека не
    определяется, GeoIP нет."""
    ip = _norm_ip(addr or "")
    if ip is None:
        return VpnStatus(False)
    own = next((n for n in nodes if n.local), None)
    if own is not None and local_subnet:
        try:
            if ip in ipaddress.ip_network(local_subnet, strict=False):
                return VpnStatus(True, own.name, METHOD_AWG)
        except ValueError:
            pass
    for node in nodes:
        if node.ip and _norm_ip(node.ip) == ip:
            return VpnStatus(True, node.name, METHOD_VLESS if node.local else "")
    return VpnStatus(False)


METHOD_AWG = "AmneziaWG"
METHOD_VLESS = "VLESS · Hiddify"


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


_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def _gb(value: int) -> str:
    gb = max(value, 0) / 1_000_000_000
    return f"{gb:.0f}" if gb >= 10 else f"{gb:.1f}".removesuffix(".0")


def remaining_text(sub: Subscription) -> str:
    """«Осталось 87 ГБ из 100 до 1 ноября» (пусто, если лимита нет)."""
    if sub.total_bytes <= 0:
        return ""
    left = max(sub.total_bytes - sub.used_bytes, 0)
    text = f"Осталось {_gb(left)} ГБ из {_gb(sub.total_bytes)}"
    if sub.expire_ts > 0:
        when = datetime.fromtimestamp(sub.expire_ts, UTC)
        text += f" до 1 {_MONTHS_GEN[when.month - 1]}"
    return text


@dataclass(frozen=True)
class PageLinks:
    """Внешние ссылки страницы — из ``[vpn]`` (те же, что в боте)."""

    hiddify_ios: str
    hiddify_android: str
    hiddify_site: str
    amnezia_ios: str = ""
    amnezia_android: str = ""
    amnezia_site: str = ""


_CSS = """
:root { --bg:#f5f6f8; --card:#fff; --fg:#14181f; --muted:#5b6472;
  --accent:#2f6bff; --line:#e1e4ea; --ok:#14804a; --bad:#b42318; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#10131a; --card:#181d27; --fg:#eef1f6; --muted:#98a2b3;
    --accent:#5b8cff; --line:#2a3140; --ok:#4ade80; --bad:#f87171; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:17px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:480px; margin:0 auto; padding:24px 16px 40px; }
h1 { font-size:22px; margin:0 0 16px; }
h2 { font-size:18px; margin:0 0 8px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:16px;
  padding:16px; margin-bottom:16px; }
.status { font-size:19px; font-weight:600; margin:0 0 4px; }
.status.on { color:var(--ok); } .status.off { color:var(--bad); }
.btn { display:block; width:100%; text-align:center; text-decoration:none; border:0;
  border-radius:14px; padding:16px 12px; font-size:18px; font-weight:600;
  background:var(--accent); color:#fff; cursor:pointer; font-family:inherit; margin-top:12px; }
.btn.alt { background:transparent; color:var(--accent); border:2px solid var(--accent);
  font-size:16px; padding:12px; }
.hint { color:var(--muted); font-size:14px; margin:8px 0 0; }
ol { margin:8px 0 0; padding-left:22px; } li { margin:6px 0; }
.qr { text-align:center; }
.qr img { width:220px; height:220px; max-width:100%; background:#fff; padding:8px;
  border-radius:12px; }
a.l { color:var(--accent); margin-right:12px; display:inline-block; }
input.t { width:100%; padding:10px; border-radius:10px; border:1px solid var(--line);
  background:var(--bg); color:var(--fg); font:13px monospace; margin-top:10px; }
details > summary { cursor:pointer; font-weight:600; font-size:18px; }
ul { margin:6px 0 0; padding-left:20px; }
"""


def _layout(title: str, body: str, script: str = "") -> str:
    e = html.escape
    js = f"<script>\n{script}\n</script>\n" if script else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>{e(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
{body}
</main>
{js}</body>
</html>
"""


def _links(*pairs: tuple[str, str]) -> str:
    e = html.escape
    return "".join(
        f'<a class="l" href="{e(url, quote=True)}">{e(name)}</a>' for name, url in pairs if url
    )


def _flag(name: str) -> str:
    flag = [c for c in name if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    return "".join(flag[:2])


def _status_card(status: VpnStatus | None) -> str:
    e = html.escape
    if status is None:
        return ""
    if status.on:
        method = f'<p class="hint">Способ: {e(status.method)}</p>' if status.method else ""
        return (
            '<div class="card"><p class="status on">'
            f"✅ VPN включён — {e(status.name)}</p>{method}"
            '<a class="btn alt" href="">🔄 Проверить ещё раз</a></div>'
        )
    return (
        '<div class="card"><p class="status off">❌ VPN сейчас выключен</p>'
        '<p class="hint">Включите VPN (в Hiddify — круглая кнопка) и вернитесь на эту '
        "страницу.</p>"
        '<a class="btn alt" href="">🔄 Проверить ещё раз</a></div>'
    )


def _countries_card(sub: Subscription) -> str:
    e = html.escape
    health = {n.name: n.health for n in sub.nodes}
    rows = []
    for entry in sub.entries:
        h = health.get(entry.name, "")
        mark = " — работает" if h == "ok" else " — может не работать" if h == "bad" else ""
        rows.append(f"<li>{e(entry.name)}{mark}</li>")
    left = remaining_text(sub)
    tail = f'<p class="hint">{e(left)}</p>' if left else ""
    return f'<div class="card"><h2>Страны</h2><ul>{"".join(rows)}</ul>{tail}</div>'


def _awg_card(sub: Subscription, forms: dict[str, str], path: str) -> str:
    """Свёрнутый блок AmneziaWG: по стране — кнопка «Получить настройки»;
    ``forms`` — node -> одноразовый nonce формы. Пусто, если выдавать нечем."""
    e = html.escape
    if not forms:
        return ""
    buttons = []
    for node in sub.nodes:
        nonce = forms.get(node.node)
        if nonce is None:
            continue
        buttons.append(
            f'<form method="post" action="{e(path, quote=True)}/awg">'
            f'<input type="hidden" name="nonce" value="{e(nonce, quote=True)}">'
            f'<input type="hidden" name="node" value="{e(node.node, quote=True)}">'
            f'<button class="btn alt" type="submit">Получить настройки {e(_flag(node.name) or node.name)}'
            "</button></form>"
        )
    return (
        '<div class="card"><details><summary>AmneziaWG</summary>'
        '<p class="hint">Другой способ, обычно быстрее. Из России работает не на всех '
        "серверах.</p>"
        f"{''.join(buttons)}"
        '<p class="hint">Ключ AmneziaWG на сервере не хранится: настройки показываются один '
        "раз. Если ключ в стране уже есть, новый заменит старый.</p>"
        "</details></div>"
    )


def render_page(
    sub: Subscription,
    *,
    sub_url: str,
    qr_data_uri: str,
    links: PageLinks,
    status: VpnStatus | None = None,
    awg_forms: dict[str, str] | None = None,
    auto_open: bool = True,
    path: str = "",
) -> str:
    """Хаб устройства: статус «VPN включён», подключение через Hiddify, страны
    и остаток, AmneziaWG. Русский, на «вы», без пола; всё инлайн."""
    e = html.escape
    link = deep_link(sub_url, sub.device_label)
    title = f"📶 VPN · {sub.device_label}"
    stores = _links(
        ("Google Play", links.hiddify_android),
        ("App Store", links.hiddify_ios),
        ("Сайт", links.hiddify_site),
    )
    body = f"""<h1>{e(title)}</h1>
{_status_card(status)}
<div class="card">
<h2>VLESS · Hiddify</h2>
<ol>
<li>Установите Hiddify:<br>{stores}</li>
<li>Нажмите кнопку ниже — откроется Hiddify, подтвердите добавление.</li>
<li>Включите круглую кнопку в Hiddify и вернитесь на эту страницу — здесь будет видно, что VPN работает.</li>
</ol>
<a class="btn" id="add" href="{e(link, quote=True)}">➕ Добавить в Hiddify</a>
<p class="hint">Не открылось? Откройте эту страницу в обычном браузере.</p>
<button class="btn alt" id="copy" type="button">📋 Скопировать ссылку</button>
<input class="t" id="url" readonly value="{e(sub_url, quote=True)}" aria-label="Ссылка подписки">
<p class="hint" id="copied" hidden>Ссылка скопирована. В Hiddify: «+» &rarr; «Добавить из буфера обмена».</p>
<details style="margin-top:12px"><summary style="font-size:16px">QR для другого устройства</summary>
<div class="qr"><img src="{e(qr_data_uri, quote=True)}" alt="QR-код ссылки подписки"></div></details>
</div>
{_countries_card(sub)}
{_awg_card(sub, awg_forms or {}, path)}"""
    auto = "true" if auto_open and not (status and status.on) else "false"
    script = (
        """(function () {
  var btn = document.getElementById('copy'), box = document.getElementById('url');
  var note = document.getElementById('copied'), add = document.getElementById('add');
  function done() { note.hidden = false; }
  function fallback() {
    box.focus(); box.select(); box.setSelectionRange(0, box.value.length);
    try { if (document.execCommand('copy')) done(); } catch (e) {}
  }
  btn.addEventListener('click', function () {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(box.value).then(done, fallback);
    } else { fallback(); }
  });
  if (AUTO) {
    try {
      var k = 'hid:' + location.pathname;
      if (!localStorage.getItem(k)) {
        localStorage.setItem(k, '1');
        setTimeout(function () { location.href = add.href; }, 700);
      }
    } catch (e) {}
  }
})();"""
    ).replace("AUTO", auto)
    return _layout(title, body, script)


def render_notice(title: str, text: str, *, path: str = "", back: bool = True) -> str:
    """Короткая страница-сообщение (ошибка, «подождите»)."""
    e = html.escape
    tail = (
        f'<a class="btn alt" href="{e(path, quote=True)}">⬅️ К настройкам</a>'
        if back and path
        else ""
    )
    return _layout(title, f'<h1>{e(title)}</h1><div class="card"><p>{e(text)}</p>{tail}</div>')


def render_awg_confirm(sub: Subscription, node: NodeInfo, nonce: str, path: str) -> str:
    e = html.escape
    body = f"""<h1>AmneziaWG {e(_flag(node.name) or node.name)}</h1>
<div class="card"><p>⚠️ У «{e(sub.device_label)}» уже есть ключ AmneziaWG в этой стране.
Новый заменит старый — там, где стоит старый, связь пропадёт.</p>
<form method="post" action="{e(path, quote=True)}/awg">
<input type="hidden" name="nonce" value="{e(nonce, quote=True)}">
<input type="hidden" name="node" value="{e(node.node, quote=True)}">
<input type="hidden" name="confirm" value="1">
<button class="btn" type="submit">Выпустить новый</button></form>
<a class="btn alt" href="{e(path, quote=True)}">Отмена</a></div>"""
    return _layout("AmneziaWG", body)


def render_awg_result(
    sub: Subscription,
    node: NodeInfo,
    *,
    filename: str,
    conf_text: str,
    qr_data_uri: str,
    links: PageLinks,
    path: str,
) -> str:
    """Настройки AmneziaWG — показываются один раз (ключ не хранится)."""
    e = html.escape
    flag = _flag(node.name)
    rename = f"{flag} {sub.device_label}".strip()
    data = "data:application/octet-stream;base64," + base64.b64encode(conf_text.encode()).decode()
    stores = _links(
        ("Google Play", links.amnezia_android),
        ("App Store", links.amnezia_ios),
        ("Сайт", links.amnezia_site),
    )
    body = f"""<h1>AmneziaWG {e(flag or node.name)}</h1>
<div class="card">
<p><b>Настройки показаны один раз</b> — сохраните файл сейчас.</p>
<a class="btn" download="{e(filename, quote=True)}" href="{e(data, quote=True)}">⬇️ Скачать {e(filename)}</a>
<ol>
<li>Установите AmneziaVPN:<br>{stores}</li>
<li>Откройте файл в AmneziaVPN.</li>
<li>❗️ Обязательно переименуйте добавленное подключение — например, «{e(rename)}».</li>
</ol>
<p class="hint">Магазин недоступен? Запросите файл в боте: /vpn → ❓ Помощь → Магазин недоступен?</p>
</div>
<div class="card qr"><p style="margin-top:0">QR для другого устройства:</p>
<img src="{e(qr_data_uri, quote=True)}" alt="QR-код настроек AmneziaWG"></div>
<a class="btn alt" href="{e(path, quote=True)}">⬅️ К настройкам</a>"""
    return _layout("AmneziaWG", body)


def qr_data_uri(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_data_uri(scale=6, border=2)
