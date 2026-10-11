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
import json
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
    base: str = ""  # https://хост:порт страницы этой ноды (для проверки из браузера)
    # Устройство этой ноде известно (по VLESS или по подписи токена) — можно выдавать
    # AmneziaVPN, даже если VLESS в этой стране у устройства нет.
    device: bool = False

    def to_wire(self) -> dict:
        return {
            "node": self.node,
            "name": self.name,
            "ip": self.ip,
            "base": self.base,
            "health": self.health,
            "awg": self.awg,
            "awg_key": self.awg_key,
            "device": self.device,
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
            base=str(raw.get("base") or ""),
            device=bool(raw.get("device")),
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


METHOD_AWG = "AmneziaVPN"
METHOD_VLESS = "VLESS · Hiddify"
VIA_AWG = "awg"  # ответ /where: трафик идёт через эту ноду по AmneziaWG
VIA_VLESS = "vless"
_VIA_METHOD = {VIA_AWG: METHOD_AWG, VIA_VLESS: METHOD_VLESS}


def detect_via(addr: str, nodes: tuple[NodeInfo, ...], local_subnet: str) -> str:
    """Для /where: идёт ли запрос через ЭТУ ноду. ``"awg"`` / ``"vless"`` / ``""``."""
    status = detect_status(addr, nodes, local_subnet)
    if not status.on or not status.method:
        return ""
    return VIA_AWG if status.method == METHOD_AWG else VIA_VLESS


def check_nodes(nodes: tuple[NodeInfo, ...]) -> list[dict[str, str]]:
    """Ноды, у которых страница умеет отвечать на /where: ``[{n: имя, u: https://…}]``."""
    return [{"n": n.name, "u": n.base} for n in nodes if n.base.startswith("https://")]


def page_csp(nodes: tuple[NodeInfo, ...]) -> str:
    """CSP страницы: ``connect-src`` — только она сама и страницы наших нод."""
    connect = " ".join(["'self'", *sorted({n["u"] for n in check_nodes(nodes)})])
    return (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        f"script-src 'unsafe-inline'; connect-src {connect}; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


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
        "referrer-policy": "same-origin",
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
  --accent:#2f6bff; --line:#e1e4ea; --ok:#14804a; --bad:#b42318; --okbg:#e6f6ec; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#10131a; --card:#181d27; --fg:#eef1f6; --muted:#98a2b3;
    --accent:#5b8cff; --line:#2a3140; --ok:#4ade80; --bad:#f87171; --okbg:#10301e; }
}
* { box-sizing: border-box; }
[hidden] { display:none !important; }
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
.card.ok { border-color:var(--ok); background:var(--okbg); }
.pair { display:flex; flex-wrap:wrap; gap:8px; }
.cell { flex:1 1 150px; min-width:0; }
.btn.warn { background:var(--bad); color:#fff; border:0; font-size:15px; padding:12px; }
.btn:disabled { opacity:.6; cursor:default; }
summary.sm { font-size:16px; }
.moreblk { margin-top:10px; }
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
<meta name="referrer" content="same-origin">
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


def flag_of(name: str) -> str:
    flag = [c for c in name if 0x1F1E6 <= ord(c) <= 0x1F1FF]
    return "".join(flag[:2])


_OFF_HINT = "Включите VPN (в Hiddify — круглая кнопка) и вернитесь на эту страницу."


def _status_card(status: VpnStatus | None) -> str:
    """Карточка «VPN включён?». Элементы с id переписывает скрипт страницы после
    опроса /where всех нод; без скрипта остаётся то, что решил сервер по адресу."""
    e = html.escape
    if status is None:
        return ""
    again = '<a class="btn alt" id="recheck" href="">🔄 Проверить ещё раз</a>'
    if status.on:
        method = f"Способ: {e(status.method)}" if status.method else ""
        return (
            '<div class="card ok" id="stcard"><p class="status on" id="st">'
            f"✅ VPN включён — {e(status.name)}</p>"
            f'<p class="hint" id="stm"{"" if method else " hidden"}>{method}</p>{again}</div>'
        )
    return (
        '<div class="card" id="stcard"><p class="status off" id="st">❌ VPN сейчас выключен</p>'
        f'<p class="hint" id="stm">{_OFF_HINT}</p>{again}</div>'
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


PLATFORM_ANDROID, PLATFORM_IOS, PLATFORM_OTHER = "android", "ios", "other"


def detect_platform(user_agent: str) -> str:
    """Платформа по User-Agent: ``android`` / ``ios`` (iPhone, iPad, iPod) / ``other``.
    iPadOS в режиме «компьютер» представляется как Macintosh — это ``other`` (там
    показываются обе кнопки)."""
    ua = (user_agent or "").lower()
    if "android" in ua:
        return PLATFORM_ANDROID
    if any(mark in ua for mark in ("iphone", "ipad", "ipod")):
        return PLATFORM_IOS
    return PLATFORM_OTHER


_CHECK_STEP = (
    "Включите VPN, вернитесь на эту страницу и нажмите «🔄 Проверить ещё раз» — "
    "должно загореться зелёное «✅ VPN включён»."
)


def _awg_steps(platform: str, stores: str) -> str:
    """Короткая нумерованная инструкция AmneziaVPN под платформу."""
    install = (
        "Убедитесь, что установлено именно приложение <b>AmneziaVPN</b> (не AmneziaWG) — "
        "без него ключ не заработает:"
        f"<br>{stores}"
    )
    if platform == PLATFORM_IOS:
        steps = [
            install,
            "Нажмите «📋 Скопировать» с нужной страной.",
            "В AmneziaVPN нажмите «+», в поле «Вставьте ключ» — «Вставить», затем «Продолжить».",
            "Если хотите, так же добавьте другую страну.",
            _CHECK_STEP,
        ]
    elif platform == PLATFORM_ANDROID:
        steps = [
            install,
            "Нажмите «➕ Подключить» с нужной страной — откроется AmneziaVPN, подтвердите добавление.",
            _CHECK_STEP,
        ]
    else:
        steps = [
            install,
            "Нажмите «➕ Подключить» — если AmneziaVPN не открылся, нажмите «📋 Скопировать», "
            "в AmneziaVPN «+» → «Вставьте ключ» → «Вставить» → «Продолжить». "
            "Файл .conf — в «Другие способы» ниже.",
            _CHECK_STEP,
        ]
    return "<ol>" + "".join(f"<li>{step}</li>" for step in steps) + "</ol>"


def _awg_card(sub: Subscription, forms: dict[str, str], platform: str, links: PageLinks) -> str:
    """Основной (раскрытый) блок AmneziaVPN. Ключи выпускает и перевыпускает скрипт
    страницы (POST ``…/awg``) только по нажатию кнопки, автовыпуска нет; по стране — ячейка с кнопкой. ``forms`` —
    node -> одноразовый nonce. Пусто, если выдавать нечем."""
    e = html.escape
    if not forms:
        return ""
    stores = _links(
        ("Google Play", links.amnezia_android),
        ("App Store", links.amnezia_ios),
        ("Сайт", links.amnezia_site),
    )
    cells = []
    for node in sub.nodes:
        nonce = forms.get(node.node)
        if nonce is None:
            continue
        flag = flag_of(node.name) or node.name
        if node.awg_key:
            act = f'<button class="btn alt re" type="button">🔄 Перевыпустить {e(flag)}</button>'
            msg = "Ключ уже выпускался — показать его нельзя."
        else:
            act = f'<button class="btn alt go" type="button">🔑 Сгенерировать ключ {e(flag)}</button>'
            msg = ""
        cells.append(
            f'<div class="cell" data-node="{e(node.node, quote=True)}" '
            f'data-flag="{e(flag, quote=True)}" data-have="{1 if node.awg_key else 0}" '
            f'data-nonce="{e(nonce, quote=True)}"><div class="act">{act}</div>'
            f'<p class="hint msg">{e(msg)}</p></div>'
        )
    return (
        '<div class="card"><details id="awg" open><summary>AmneziaVPN — основной способ, обычно быстрее'
        "</summary>"
        f"{_awg_steps(platform, stores)}"
        f'<div class="pair">{"".join(cells)}</div>'
        '<p class="hint">Ключ на сервере не хранится и показывается один раз. Выпущенный раньше '
        "показать нельзя — его можно только перевыпустить: старый перестанет работать.</p>"
        '<noscript><p class="hint">Для выдачи ключей нужен JavaScript. Запросите ключ в боте: '
        "/vpn → «Другие способы».</p></noscript>"
        '<details id="awgmore" hidden><summary class="sm">Другие способы</summary>'
        '<div id="awgmorebody"></div></details>'
        "</details></div>"
    )


_PAGE_JS = r"""(function () {
  var NODES = __NODES__, WHERE = __WHERE__, OFF_HINT = __OFF_HINT__;
  var btn = document.getElementById('copy'), box = document.getElementById('url');
  var note = document.getElementById('copied');
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
  var st = document.getElementById('st'), stm = document.getElementById('stm');
  var stcard = document.getElementById('stcard');
  var again = document.getElementById('recheck');
  var METHODS = { awg: 'AmneziaVPN', vless: 'VLESS · Hiddify' };
  function show(cls, text, hint) {
    if (!st) return;
    st.className = 'status ' + cls; st.textContent = text;
    if (stcard) stcard.className = 'card' + (cls === 'on' ? ' ok' : '');
    if (stm) { stm.textContent = hint; stm.hidden = !hint; }
  }
  // Каждая нода отвечает только за себя: идёт ли трафик через неё и каким способом.
  function ask(n) {
    return new Promise(function (resolve) {
      var ctl = window.AbortController ? new AbortController() : null;
      var timer = setTimeout(function () {
        if (ctl) ctl.abort();
        resolve({ n: n.n, err: true });
      }, 4000);
      fetch(n.u + WHERE, { cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
                           signal: ctl ? ctl.signal : undefined })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (j) { clearTimeout(timer); resolve({ n: n.n, via: (j && j.via) || '' }); })
        .catch(function () { clearTimeout(timer); resolve({ n: n.n, err: true }); });
    });
  }
  var init = st ? { c: st.className.replace('status ', ''), t: st.textContent,
                    h: stm && !stm.hidden ? stm.textContent : '' } : null;
  function check() {
    show('', '⏳ Проверяем…', '');
    return Promise.all(NODES.map(ask)).then(function (rs) {
      var hit = rs.filter(function (r) { return r.via && METHODS[r.via]; })[0];
      if (hit) {
        show('on', '✅ VPN включён — ' + hit.n, 'Способ: ' + METHODS[hit.via]);
        return true;
      }
      if (rs.some(function (r) { return !r.err; })) {
        show('off', '❌ VPN сейчас выключен', OFF_HINT);
      } else if (init) {
        show(init.c, init.t, init.h);  // ни одна нода не ответила — решение сервера
      }
      return false;
    });
  }
  if (!window.fetch || !window.Promise || !NODES.length || !WHERE) return;
  if (again) {
    again.addEventListener('click', function (ev) { ev.preventDefault(); check(); });
  }
  check();
})();"""


# Выпуск и перевыпуск ключей AmneziaVPN на месте: POST ``…/awg`` (node, nonce, action),
# ответ — JSON. GET ничего не выпускает, поэтому предпросмотр ссылок и сканеры (без JS)
# ключей не создают.
_AWG_JS = r"""(function () {
  var PLATFORM = __PLATFORM__, URL = __URL__;
  var box = document.getElementById('awg');
  if (!box || !window.fetch || !window.Promise) return;
  var cells = Array.prototype.slice.call(box.querySelectorAll('.cell'));
  var more = document.getElementById('awgmore'), moreBody = document.getElementById('awgmorebody');
  function el(tag, cls, text) {
    var x = document.createElement(tag);
    if (cls) x.className = cls;
    if (text) x.textContent = text;
    return x;
  }
  function btn(cls, text) { var b = el('button', 'btn ' + cls, text); b.type = 'button'; return b; }
  function part(cell, sel) { return cell.querySelector(sel); }
  function flag(cell) { return cell.getAttribute('data-flag'); }
  function setMsg(cell, text) { part(cell, '.msg').textContent = text || ''; }
  function setAct(cell, node) {
    var act = part(cell, '.act');
    while (act.firstChild) act.removeChild(act.firstChild);
    act.appendChild(node);
  }
  function busy(cell, text) {
    var b = btn('alt', '⏳ ' + text); b.disabled = true; setAct(cell, b);
  }
  function copyText(text, onDone) {
    function legacy() {
      var t = el('textarea'); t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.focus(); t.select();
      try { if (document.execCommand('copy')) onDone(); } catch (e) {}
      document.body.removeChild(t);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(onDone, legacy);
    } else { legacy(); }
  }
  function post(cell, action) {
    var body = new URLSearchParams();
    body.set('node', cell.getAttribute('data-node'));
    body.set('nonce', cell.getAttribute('data-nonce'));
    body.set('action', action);
    return fetch(URL, { method: 'POST', body: body, cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.nonce) cell.setAttribute('data-nonce', j.nonce); return j; });
  }
  function fail(cell, j, retry) {
    var why = j && j.error;
    setMsg(cell, why === 'stale' ? 'Страница устарела — обновите её.'
      : why === 'rate' ? 'Слишком часто — подождите несколько секунд и нажмите ещё раз.'
      : 'Не получилось — попробуйте ещё раз чуть позже.');
    if (why === 'exists') { setMsg(cell, 'Ключ уже выпускался — показать его нельзя.'); showReissue(cell); }
    else retry(cell);
  }
  function showIssue(cell) {
    var b = btn('alt go', '🔑 Сгенерировать ключ ' + flag(cell));
    b.addEventListener('click', function () { issue(cell); });
    setAct(cell, b);
  }
  function showReissue(cell) {
    var label = '🔄 Перевыпустить ' + flag(cell), armed = false, timer = null;
    var b = btn('alt re', label);
    function reset() { armed = false; b.className = 'btn alt re'; b.textContent = label; }
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true; b.className = 'btn warn';
        b.textContent = 'Старый ключ перестанет работать — нажмите ещё раз';
        timer = setTimeout(reset, 7000);
        return;
      }
      clearTimeout(timer);
      busy(cell, 'Перевыпускаю…'); setMsg(cell, '');
      post(cell, 'reissue').then(function (j) {
        if (j && j.ok) ready(cell, j); else fail(cell, j, showReissue);
      }, function () { fail(cell, null, showReissue); });
    });
    setAct(cell, b);
  }
  function more_(cell, j) {
    var node = cell.getAttribute('data-node'), old = moreBody.querySelector('[data-for="' + node + '"]');
    if (old) moreBody.removeChild(old);
    var blk = el('div', 'moreblk'); blk.setAttribute('data-for', node);
    blk.appendChild(el('p', 'hint', 'Ключ ' + flag(cell) + ' (ссылка vpn://) и файл настроек:'));
    if (j.key) {
      var inp = el('input', 't'); inp.readOnly = true; inp.value = j.key;
      inp.setAttribute('aria-label', 'Ключ AmneziaVPN ' + flag(cell));
      blk.appendChild(inp);
    }
    if (j.conf) {
      var a = el('a', 'btn alt', '⬇️ Скачать ' + j.filename);
      a.setAttribute('download', j.filename);
      try { a.href = 'data:application/octet-stream;base64,' + btoa(unescape(encodeURIComponent(j.conf))); }
      catch (e) { a.hidden = true; }
      blk.appendChild(a);
    }
    if (j.qr) {
      var q = el('div', 'qr'), img = el('img'); img.src = j.qr; img.alt = 'QR-код настроек AmneziaVPN';
      q.appendChild(img); blk.appendChild(q);
    }
    moreBody.appendChild(blk); more.hidden = false;
  }
  function ready(cell, j) {
    var holder = el('div'), f = flag(cell);
    if (j.key && PLATFORM !== 'ios') {
      var a = el('a', 'btn', '➕ ' + f + ' Подключить'); a.href = j.key; holder.appendChild(a);
    }
    if (j.key && PLATFORM !== 'android') {
      var label = '📋 Скопировать ' + f;
      var c = btn(PLATFORM === 'ios' ? '' : 'alt', label);
      c.addEventListener('click', function () {
        copyText(j.key, function () { c.textContent = '✅ Скопировано ' + f; });
      });
      holder.appendChild(c);
    }
    setAct(cell, holder);
    setMsg(cell, j.key ? 'Ключ показан один раз — добавьте его в AmneziaVPN сейчас.'
      : 'Ключ собрать не вышло — используйте файл в «Другие способы».');
    more_(cell, j);
    if (!j.key) more.open = true;
  }
  function issue(cell) {
    busy(cell, 'Выпускаю…'); setMsg(cell, '');
    return post(cell, 'issue').then(function (j) {
      if (j && j.ok) ready(cell, j); else fail(cell, j, showIssue);
    }, function () { fail(cell, null, showIssue); });
  }
  cells.forEach(function (cell) {
    if (cell.getAttribute('data-have') === '1') showReissue(cell); else showIssue(cell);
  });
})();"""


def render_page(
    sub: Subscription,
    *,
    sub_url: str,
    qr_data_uri: str,
    links: PageLinks,
    status: VpnStatus | None = None,
    awg_forms: dict[str, str] | None = None,
    platform: str = PLATFORM_OTHER,
    path: str = "",
) -> str:
    """Хаб устройства: статус «VPN включён», Hiddify одной кнопкой, страны и остаток,
    AmneziaVPN (ключи выпускаются скриптом страницы, не по нажатию). Русский, на
    «вы», без пола; всё инлайн. Автоперехода в приложения нет."""
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
{_awg_card(sub, awg_forms or {}, platform, links)}
<div class="card">
<h2>VLESS · Hiddify{" — запасной способ" if awg_forms else ""}</h2>
{'<p class="hint">Если AmneziaVPN не подключается — используйте Hiddify.</p>' if awg_forms else ""}
<ol>
<li>Убедитесь, что установлено приложение <b>Hiddify</b>:<br>{stores}</li>
<li>Нажмите кнопку ниже и подтвердите добавление в Hiddify.</li>
<li>{e(_CHECK_STEP)}</li>
</ol>
<a class="btn" id="add" href="{e(link, quote=True)}">➕ Добавить в Hiddify</a>
<details style="margin-top:12px"><summary class="sm">Другие способы</summary>
<p class="hint">Не открылось? Откройте эту страницу в обычном браузере или добавьте вручную.</p>
<button class="btn alt" id="copy" type="button">📋 Скопировать ссылку</button>
<input class="t" id="url" readonly value="{e(sub_url, quote=True)}" aria-label="Ссылка подписки">
<p class="hint" id="copied" hidden>Ссылка скопирована. В Hiddify: «+» &rarr; «Добавить из буфера обмена».</p>
<a class="btn alt" download href="{e(sub_url + "?format=singbox", quote=True)}">⬇️ Файл настроек</a>
<div class="qr"><p class="hint">QR для другого устройства:</p><img src="{e(qr_data_uri, quote=True)}" alt="QR-код ссылки подписки"></div>
</details>
</div>
{_countries_card(sub)}"""
    nodes_json = json.dumps(check_nodes(sub.nodes), ensure_ascii=False).replace("<", "\\u003c")
    where = json.dumps(f"{path}/where") if path else '""'
    script = (
        _PAGE_JS.replace("__NODES__", nodes_json)
        .replace("__WHERE__", where)
        .replace("__OFF_HINT__", json.dumps(_OFF_HINT, ensure_ascii=False))
    )
    if awg_forms and path:
        script += "\n" + (
            _AWG_JS.replace("__PLATFORM__", json.dumps(platform)).replace(
                "__URL__", json.dumps(f"{path}/awg")
            )
        )
    return _layout(title, body, script)


def qr_data_uri(text: str) -> str:
    import segno

    return segno.make(text, error="m").svg_data_uri(scale=6, border=2)
