"""TorrentsService — ServiceHandler службы torrents (адаптер qBittorrent).

`add` — добавить раздачу по magnet-ссылке/URL или по
содержимому .torrent-файла. Файл идёт по протоколу как base64-строка
(`ActionParam` — только string|int|float|bool, PROTOCOL.md), обычные
.torrent-метафайлы на порядки меньше лимита сообщения (`MAX_MESSAGE_BYTES`
= 1 МиБ, proto/messages.py). Директория сохранения — конечный список из
конфига (`save_dirs`), а не свободный ввод: бот строит кнопки по `choices`
действия, в systemd не ходит — только к этой службе.

`list` — read-only состояние закачек. Появилось 2026-07-27 вместе с тулом
swarm_status: до него служба вообще не умела рассказать, что качается
(get_state отдавал только список директорий), и спросить Альфреда про
торренты было не у кого. Отдаёт узкий срез (имя, прогресс, состояние,
скорость, оценка) — путей на диске в нём нет: они ничего не добавляют к
ответу на вопрос «что там качается», а знать их этому потребителю незачем.

`pause`/`resume` — остановить и снова запустить раздачу. Адресуются
**по имени** (`name`), не по хэшу: имена — это то, что уже видит потребитель
в `list`, а сорокасимвольный хэш пришлось бы гонять в контекст модели
целиком и надеяться, что она перепишет его без опечатки. Разрешение имени в
хэш делает сама служба (`_select`): точное совпадение → хэш → узнаваемая
часть имени; неоднозначность — не «остановим на всякий случай все похожие»,
а честная ошибка со списком кандидатов (`все` — отдельное явное слово).

`search` — поиск по трекерам ЧУЖИМИ руками: встроенным поисковым движком
qBittorrent и его плагинами (`search_start`/`search_results` Web API).
Заведено 2026-07-29, потому что Альфред с одним `web_search` (SearXNG)
упирался в стену: выдача поисковика даёт заголовок, ссылку и сниппет, а
magnet живёт ВНУТРИ темы трекера, часто ещё и под логином — открыть
страницу модель не может ничем. Плагин же логинится сам и отдаёт готовые
поля (имя, размер, сиды), по которым уже можно выбирать осмысленно.

`search_smart` — та же выдача, но по-другому добытая: заведено 2026-08-09,
переписано под явные `phrase`+`words` 2026-08-10. Обычный `search` (и его
лестница `_query_ladder`) всё равно шлёт трекеру НЕПРЕРЫВНУЮ фразу — если
реальное название на трекере записано в другом порядке слов («Дюна: Часть
вторая» вместо «Дюна Часть 2») или с доп. словом ПОСЕРЕДИНЕ (не в начале/
конце, где его срезает лестница), обычный поиск находит ноль. `search_smart`
принимает `phrase` — ту же непрерывную подстрочную фразу, что и `search`
(отправляется трекеру как pattern тем же способом, но в большой сырой пул
`SEARCH_SMART_FETCH_LIMIT` с одного задания поиска) — и опциональный `words`:
набор дополнительных слов (год, качество, сезон, что угодно ещё), каждое из
которых должно встречаться в имени находки в любом порядке — AND-фильтр уже
в Python, поверх сырой выдачи по `phrase`. Модель сама решает, что кладёт в
`phrase` (устойчивая часть названия), а что — в `words` (то, что могло стоять
в другом порядке или не рядом с названием); никакой автоматической разборки
свободного текста здесь больше нет. Ответ модели по-прежнему видит только
верхушку (`SEARCH_LIMIT`) — расширяется только внутренний пул для
фильтрации, а не то, что уезжает в контекст.

Ссылка из результатов поиска — это НЕ magnet, а `dl.php?t=…`, который
отдаётся только авторизованному. Поэтому `add` для таких ссылок качает
метафайл не через qBittorrent, а через `nova2dl.py` самого qBittorrent
(`_fetch_via_plugin`) — тем же способом, каким это делает его собственный
GUI: плагин отдаёт .torrent своей сессией, а мы добавляем его как файл, уже
с нужным `save_path`. Штатное `search/downloadTorrent` Web API так не умеет
— оно кладёт раздачу в директорию по умолчанию, мимо выбора «куда сохранить».

`details` — карточка одной найденной раздачи: страница трекера, очищенная
от разметки. В выдаче поиска есть только имя, размер и сиды — а «какая
озвучка», «какой битрейт», «сколько серий» живёт на странице. Ходим только
по сайтам с установленным плагином (то же ограничение, что у `add`) и
только по одной странице за вызов.

`space` — сколько места на дисках директорий сохранения. Свободное место
считается локально (`shutil.disk_usage`) — служба живёт на той же машине,
что и qBittorrent, спрашивать его Web API про чужие ФС незачем; сам
qBittorrent нужен только ради `downloading_left_bytes` (сколько ещё
предстоит докачать уже принятым раздачам), и если он молчит — это поле
просто `null`, а место всё равно отдаётся: на вопрос «хватает ли места»
клиент торрентов не нужен вовсе.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import qbittorrentapi

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "torrents"
ACTION_ADD = "add"
ACTION_LIST = "list"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_SPACE = "space"
ACTION_SEARCH = "search"
ACTION_SEARCH_SMART = "search_smart"
ACTION_DETAILS = "details"
ACTION_SPEED_LIMIT = "speed_limit"

# Потолок общего лимита скорости (download+upload разом) из бот-панели —
# домашний канал слабый, выше пользователь ограничивать и не просил.
MAX_SPEED_LIMIT_MBPS = 5

# Сколько находок отдавать. Их читает модель и выбирает одну — на десятке
# строк выбор по сидам/размеру делается не хуже, чем на полусотне. Было 10,
# уменьшено до 6 2026-07-29: вместе с декларациями тулов выдача выбивала окно
# модели (num_ctx=8192 на тот момент), ответ приходил пустым или обрывался на
# полуслове. С 2026-07-25 num_ctx поднят до 24576 (см. память
# llm-model-upgrade) — бюджета снова хватает, вернули 10.
SEARCH_LIMIT = 10
# search_smart: сколько тянуть из ОДНОГО задания поиска по слову-якорю, чтобы
# после AND-фильтра в Python на выходе всё ещё что-то оставалось. Модели
# отдаётся всё равно не больше SEARCH_LIMIT — эта цифра только про сырой пул,
# который фильтруется здесь, а не про то, что уезжает в контекст.
SEARCH_SMART_FETCH_LIMIT = 200
# Плагин ходит на живой трекер (логин + страница выдачи) — это секунды, но
# не мгновение; ждём завершения задания, а не первой пустой выдачи.
SEARCH_WAIT_S = 45.0
SEARCH_POLL_S = 0.5
SEARCH_RUNNING = "Running"
# Скачивание метафайла руками плагина (nova2dl) — короткая операция, но по
# сети; свой потолок, чтобы зависший трекер не держал команду вечно.
PLUGIN_FETCH_TIMEOUT_S = 60.0
# Карточка раздачи (details): что забирать со страницы трекера. Голова —
# название и «о фильме», техническая часть — качество/видео/аудио/размер.
# Живая находка 2026-07-29: на руторе техническая карточка лежит ПОСЛЕ
# синопсиса (позиция ~3300 при полном тексте страницы в 16 000 знаков), так
# что «первые N символов» теряют ровно то, ради чего страницу и открывают —
# поэтому берём голову И отдельным куском техническую часть.
DETAILS_HEAD_CHARS = 700
DETAILS_TECH_CHARS = 900
DETAILS_TIMEOUT_S = 20.0
# UA обычного браузера: с «Python-urllib» часть трекеров отвечает отказом, а
# ничего специфичного для конкретного клиента мы не делаем.
DETAILS_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

# Где живут поисковый движок и плагины qBittorrent (nova2dl.py, engines/).
# Пусто в конфиге — путь по умолчанию для Linux-сборки.
DEFAULT_NOVA_DIR = "~/.local/share/qBittorrent/nova3"

# Сколько раздач отдавать за раз. Ответ читает и человек, и модель (тул
# torrents) — полный список на сотню раздач бесполезен обоим, а в контексте
# модели это ещё и заметные токены. Сортировка — по убыванию активности.
LIST_LIMIT = 20

# Ниже этого порога свободного места служба новую раздачу не принимает вовсе.
# Проверка именно здесь, а не только в подсказке модели: размер раздачи по
# magnet-ссылке заранее неизвестен (метаданные качаются уже после добавления),
# так что «влезет ли ИМЕННО ЭТО» не проверить в принципе — но забить диск
# досуха, когда там уже нечего занимать, можно и нужно не дать. Порог общий
# для всех директорий: на sdb (расходная «помойка») и на /mnt/data одинаково
# незачем доводить ФС до нуля.
MIN_FREE_BYTES = 2 * 1024**3

# «Останови всё» — отдельное явное слово, а не побочный эффект того, что
# пустая/короткая подстрока подошла ко всем раздачам сразу (см. _select).
ALL_SELECTORS = frozenset({"*", "all", "все", "всё", "все закачки", "всё сразу"})


def _is_magnet_or_url(source: str) -> bool:
    return source.startswith(("magnet:", "http://", "https://"))


# Живая находка 2026-07-29: трекер (rutor) ищет запрос как НЕПРЕРЫВНУЮ фразу
# в заголовке раздачи, поэтому любое лишнее слово обнуляет выдачу целиком:
# «задача трёх тел» → 10 находок, «задача трёх тел 2024» → ноль. Модель же
# упорно дописывает год, качество и имя релизера (в логе — восемь пустых
# попыток подряд, каждая с новым набором лишних слов). Просить её в промпте
# «передавай только название» помогает не всегда, поэтому лестница попыток
# живёт здесь: запрос как есть → без шумовых слов → всё короче и короче.
_QUERY_NOISE_RE = re.compile(
    r"\b(?:"
    r"19\d{2}|20\d{2}"  # год
    r"|[sS]\d{1,2}(?:[eE]\d{1,3})?"  # S01, S01E02
    r"|\d{3,4}[pi]"  # 1080p, 720i, 2160p
    r"|web-?dl(?:rip)?|web-?rip|hd-?rip|bd-?rip|dvd-?rip|blu-?ray|hdtv|remux"
    r"|hevc|avc|x26[45]|h\.?26[45]"
    r"|сериал|сезон|series|season"
    r")\b",
    re.IGNORECASE,
)
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")
# Потолок попыток: каждая — живой запрос к трекеру (~1–2 с). Пяти хватает,
# чтобы дойти от полного названия релиза до двух слов.
MAX_QUERY_ATTEMPTS = 5


def _clean_query(query: str) -> str:
    words = [w for w in _QUERY_NOISE_RE.sub(" ", query).split() if not _PUNCT_ONLY_RE.match(w)]
    return " ".join(words)


def _query_ladder(query: str) -> list[str]:
    """Запросы к трекеру от самого точного к самому широкому (см. выше)."""
    attempts = [query.strip()]
    cleaned = _clean_query(query)
    words = cleaned.split()
    for candidate in (cleaned, *(" ".join(words[:n]) for n in (4, 3, 2))):
        if candidate and candidate not in attempts:
            attempts.append(candidate)
    return attempts[:MAX_QUERY_ATTEMPTS]


# Вырезаем то, что на странице раздачи заведомо не про раздачу: скрипты и
# навигацию. Список id/class намеренно общий — у трекеров они называются
# примерно одинаково, а промахнуться безопасно: не вырезали — просто больше
# мусора в тексте, вырезали лишнее — текст короче.
_PAGE_DROP_TAGS_RE = re.compile(r"(?is)<(script|style|head|nav|noscript)\b.*?</\1>")
_PAGE_DROP_BLOCKS_RE = re.compile(
    r'(?is)<(div|table|ul)[^>]*(?:id|class)="[^"]*'
    r'(?:menu|logo|sidebar|footer|header|news_table)[^"]*".*?</\1>'
)
_PAGE_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d)>")
_PAGE_TAG_RE = re.compile(r"(?s)<[^>]+>")
_PAGE_MAGNET_RE = re.compile(r'href="(magnet:[^"]+)"', re.IGNORECASE)
# Начало технической части карточки — по словам, одинаковым у русских
# трекеров. Строка целиком, а не вхождение: «качество» встречается и в
# описании фильма.
# Длина строки, с которой начинается «содержательная» часть страницы (см.
# _page_text): пункты меню короче, названия раздач и подписи полей — длиннее.
_MEANINGFUL_LINE_CHARS = 25
_PAGE_TECH_RE = re.compile(
    r"(?im)^\s*(?:качество|формат|видео|аудио|перевод|продолжительность|субтитры|размер)\b"
)


def _page_text(page: str) -> str:
    text = _PAGE_DROP_BLOCKS_RE.sub(" ", _PAGE_DROP_TAGS_RE.sub(" ", page))
    text = _PAGE_TAG_RE.sub(" ", _PAGE_BREAK_RE.sub("\n", text))
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # Живая находка 2026-07-29: вырезание блоков по id/class ловит не всё —
    # у настоящей страницы меню сделано вложенными div'ами, и нежадный
    # «до первого </div>» останавливается раньше. Верхнее меню при этом
    # узнаётся по форме: короткие строки-ссылки («Топ», «Категории», «Чат»)
    # до первой содержательной. Их и снимаем, а то они съедают голову
    # карточки, которой отведено немного.
    first = next((i for i, line in enumerate(lines) if len(line) >= _MEANINGFUL_LINE_CHARS), 0)
    return "\n".join(lines[first:])


def _page_summary(text: str) -> str:
    """Голова страницы + техническая карточка (см. DETAILS_HEAD_CHARS)."""
    head = text[:DETAILS_HEAD_CHARS]
    match = _PAGE_TECH_RE.search(text, len(head))
    if match is None:
        return head
    tech = text[match.start() : match.start() + DETAILS_TECH_CHARS]
    return f"{head}\n…\n{tech}"


_ENGINE_ERROR_MARK = "[Error]"


def _is_engine_error(result: dict[str, Any]) -> bool:
    return _ENGINE_ERROR_MARK in str(result.get("fileName", "")) or str(
        result.get("fileUrl", "")
    ).endswith("/error")


def _engine_error_text(result: dict[str, Any]) -> str:
    name = str(result.get("fileName", ""))
    _, _, tail = name.partition(_ENGINE_ERROR_MARK)
    return (tail.lstrip(": ") or name).strip() or "плагин не сказал, что именно"


def _disk_usage(path: str) -> tuple[int | None, int | None]:
    """(свободно, всего) в байтах, либо (None, None) — путь недоступен
    (не смонтирован внешний диск, опечатка в конфиге)."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.free, usage.total


# qBittorrent отдаёт eta = 8640000 (100 суток) как «неизвестно/бесконечность»
# для стоящих и раздающихся торрентов, а не как реальную оценку.
_ETA_UNKNOWN_S = 8640000


def _eta_or_none(raw: Any) -> int | None:
    try:
        eta = int(raw)
    except (TypeError, ValueError):
        return None
    return None if eta < 0 or eta >= _ETA_UNKNOWN_S else eta


class TorrentsService:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.torrents
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(
                ACTION_ADD,
                ACTION_LIST,
                ACTION_PAUSE,
                ACTION_RESUME,
                ACTION_SPACE,
                ACTION_SEARCH,
                ACTION_SEARCH_SMART,
                ACTION_DETAILS,
                ACTION_SPEED_LIMIT,
            ),
            actions=(
                ActionSpec(
                    id=ACTION_LIST,
                    title="📋 Что качается",
                ),
                ActionSpec(
                    id=ACTION_SEARCH,
                    title="🔎 Поиск по трекерам",
                    params=(ActionParam(name="query", type="string", title="Что ищем"),),
                ),
                ActionSpec(
                    id=ACTION_SEARCH_SMART,
                    title="🧠 Умный поиск (фраза + слова)",
                    params=(
                        ActionParam(name="phrase", type="string", title="Точная фраза (название)"),
                        ActionParam(
                            name="words",
                            type="string",
                            required=False,
                            title="Доп. слова через пробел (год, качество…)",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_DETAILS,
                    title="📖 Карточка раздачи",
                    params=(ActionParam(name="page", type="string", title="Ссылка на страницу"),),
                ),
                ActionSpec(
                    id=ACTION_SPACE,
                    title="💾 Сколько места",
                ),
                ActionSpec(
                    id=ACTION_SPEED_LIMIT,
                    title="🐢 Ограничить скорость",
                    params=(
                        ActionParam(
                            name="mbps",
                            type="int",
                            title=(
                                "Лимит, МБ/с (0 — без ограничения, "
                                f"максимум {MAX_SPEED_LIMIT_MBPS})"
                            ),
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_PAUSE,
                    title="⏸ Остановить закачку",
                    params=(ActionParam(name="name", type="string", title="Раздача"),),
                ),
                ActionSpec(
                    id=ACTION_RESUME,
                    title="▶️ Продолжить закачку",
                    params=(ActionParam(name="name", type="string", title="Раздача"),),
                ),
                ActionSpec(
                    id=ACTION_ADD,
                    title="🧲 Добавить торрент",
                    params=(
                        ActionParam(name="source", type="string", title="Magnet или файл"),
                        ActionParam(name="name", type="string", required=False, title="Имя"),
                        ActionParam(
                            name="save_path",
                            type="string",
                            title="Куда сохранить",
                            choices=tuple(self._cfg.save_dirs),
                        ),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "save_dirs": list(self._cfg.save_dirs),
        }

    def _client(self) -> qbittorrentapi.Client:
        return qbittorrentapi.Client(
            host=self._cfg.qbittorrent_url,
            username=self._cfg.qbittorrent_user,
            password=self._cfg.qbittorrent_password,
        )

    def _nova_dir(self) -> Path:
        raw = self._cfg.search_engine_dir or DEFAULT_NOVA_DIR
        return Path(raw).expanduser()

    @staticmethod
    def _plugin_for(plugins: list[Any], url: str) -> str | None:
        """Имя включённого плагина, чьему сайту принадлежит ссылка.

        Сравниваем хосты, а не префикс строки: у плагина в `url` адрес
        раздела (`https://rutracker.org/forum/`), а ссылка из выдачи ведёт на
        другой путь. Поддомен считается тем же сайтом — живая находка
        2026-07-29: rutor ищет на `rutor.info`, а качать отдаёт с
        `d.rutor.info`, и точное сравнение хостов такую находку отвергало.
        """
        host = urlsplit(url).netloc.lower()
        if not host:
            return None
        for plugin in plugins:
            if not plugin.get("enabled"):
                continue
            site = urlsplit(str(plugin.get("url", ""))).netloc.lower()
            if site and (host == site or host.endswith(f".{site}")):
                return str(plugin.get("name"))
        return None

    def _fetch_via_plugin(self, plugin: str, url: str) -> bytes:
        """Метафайл руками самого плагина (его авторизованной сессией).

        Ровно тот путь, которым качает GUI qBittorrent: `nova2dl.py <плагин>
        <ссылка>` печатает «<путь к .torrent> <ссылка>». Свой HTTP-клиент
        здесь бесполезен — `dl.php` отдаётся только залогиненному, а куки
        живут внутри плагина.
        """
        nova = self._nova_dir() / "nova2dl.py"
        if not nova.exists():
            raise ProtoError(
                ERR_INTERNAL,
                f"поисковый движок qBittorrent не найден: нет {nova} "
                "(проверьте [torrents].search_engine_dir)",
            )
        try:
            proc = subprocess.run(  # noqa: S603 — фиксированный argv, интерпретатор свой
                [sys.executable, str(nova), plugin, url],
                capture_output=True,
                timeout=PLUGIN_FETCH_TIMEOUT_S,
                check=False,
                cwd=str(self._nova_dir()),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtoError(ERR_INTERNAL, f"плагин {plugin} не отдал файл: {exc}") from exc
        out = proc.stdout.decode(errors="replace").strip()
        # Успех — одна строка «<путь> <ссылка>»; всё прочее (в т.ч. текст
        # ошибки, который плагин печатает вместо пути) — неудача.
        path = Path(out.split(" ", 1)[0]) if out else None
        if path is None or not path.is_file():
            log.warning(
                "nova2dl %s: rc=%s out=%r err=%r", plugin, proc.returncode, out, proc.stderr
            )
            raise ProtoError(
                ERR_INTERNAL,
                f"плагин {plugin} не отдал .torrent — вероятно, не настроен логин к трекеру",
            )
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    def _run_search_job(
        self, client: Any, pattern: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Одно задание поиска: старт → ожидание → результаты → уборка.

        Возвращает находки (уже урезанные до `limit`) и `total` — сколько
        всего нашли плагины по этому запросу независимо от `limit` (так
        отдаёт сам qBittorrent Web API): по нему видно, что выдача обрезана,
        не выловив это из числа строк в ответе.
        """
        job = client.search_start(pattern=pattern, plugins="enabled", category="all")
        search_id = job["id"]
        try:
            deadline = time.monotonic() + SEARCH_WAIT_S
            while time.monotonic() < deadline:
                statuses = client.search_status(search_id=search_id)
                if not statuses or statuses[0].get("status") != SEARCH_RUNNING:
                    break
                time.sleep(SEARCH_POLL_S)
            raw = client.search_results(search_id=search_id, limit=limit)
        finally:
            # Задание живёт в qBittorrent, пока его не удалят: без этого
            # каждый вопрос Альфреда оставлял бы за собой мусор.
            client.search_delete(search_id=search_id)
        results = list(raw.get("results", []))
        return results, int(raw.get("total", len(results)) or 0)

    def _details_sync(self, page: str) -> dict[str, Any]:
        """Карточка раздачи со страницы трекера.

        Ходим только на сайты, для которых установлен поисковый плагин — то
        же ограничение, что и у `add`: «открой вот эту ссылку из разговора»
        модели давать незачем, а свои же находки открывать надо.
        """
        client = self._client()
        try:
            client.auth_log_in()
            plugin = self._plugin_for(list(client.search_plugins()), page)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        if plugin is None:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "открываю только страницы трекеров, для которых установлен "
                "поисковый плагин — этот сайт не из них",
            )

        request = urllib.request.Request(page, headers={"User-Agent": DETAILS_UA})
        try:
            with urllib.request.urlopen(request, timeout=DETAILS_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read()
                # Кодировку берём из заголовков: рутор отдаёт utf-8, а
                # рутрекер и его родня — cp1251, и без этого текст приезжает
                # кракозябрами.
                charset = resp.headers.get_content_charset() or "utf-8"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProtoError(ERR_INTERNAL, f"страница не открылась: {exc}") from exc
        body = raw.decode(charset, errors="replace")

        magnet = _PAGE_MAGNET_RE.search(body)
        result: dict[str, Any] = {"page": page, "text": _page_summary(_page_text(body))}
        if magnet is not None:
            # Со страницы magnet берётся напрямую — его можно отдать в add как
            # есть, не поднимая плагин ради метафайла.
            result["magnet"] = html.unescape(magnet.group(1))
        return result

    @staticmethod
    def _map_result(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": r.get("fileName", "?"),
            "size_bytes": int(r.get("fileSize", 0) or 0),
            "seeders": max(0, int(r.get("nbSeeders", 0) or 0)),
            "leechers": max(0, int(r.get("nbLeechers", 0) or 0)),
            # Именно это значение потом уходит в add как source: у трекеров с
            # логином это не magnet, а ссылка на метафайл.
            "source": r.get("fileUrl", ""),
            "page": r.get("descrLink", ""),
        }

    def _search_sync(self, query: str, limit: int) -> dict[str, Any]:
        client = self._client()
        try:
            client.auth_log_in()
            if not [p for p in client.search_plugins() if p.get("enabled")]:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    "поиск по трекерам не настроен: в qBittorrent нет ни одного "
                    "включённого поискового плагина",
                )
            attempts = _query_ladder(query)
            usable: list[dict[str, Any]] = []
            broken: list[dict[str, Any]] = []
            total = 0
            used_query = attempts[0]
            for attempt in attempts:
                raw, total = self._run_search_job(client, attempt, limit)
                # Сбой плагина приезжает НЕ ошибкой, а фальшивой «находкой»:
                # имя вида «[запрос][Error]: …», ссылка «<сайт>/error», 100
                # сидов и терабайт размера (так плагины показывают проблему в
                # GUI). Пропустить такое в модель — значит дать ей «раздачу» с
                # рекордными сидами, которую она честно предложит скачать.
                broken = [r for r in raw if _is_engine_error(r)]
                usable = [r for r in raw if not _is_engine_error(r)]
                used_query = attempt
                if usable or broken:
                    # Пусто из-за лишних слов — пробуем короче; сломанный
                    # плагин короче не починится, дальше идти незачем.
                    break
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

        if broken and not usable:
            raise ProtoError(ERR_INTERNAL, f"поиск не отработал: {_engine_error_text(broken[0])}")

        results = [self._map_result(r) for r in usable]
        # Сортировка по сидам — то, по чему выбирают раздачу в первую очередь;
        # qBittorrent отдаёт результаты в порядке прихода от плагинов.
        results.sort(key=lambda r: r["seeders"], reverse=True)
        shown = results[:limit]
        payload: dict[str, Any] = {
            "results": shown,
            "count": len(shown),
            "query": query,
        }
        if total > len(shown):
            # Реальных «страниц» нет — задание поиска удаляется сразу после
            # чтения результатов (см. _run_search_job), просить следующую
            # порцию не у чего. Отдаём общее число находок, чтобы модель
            # честно сказала человеку «показал N из total», а не выдала
            # верхушку за всю выдачу.
            payload["total"] = total
        if used_query != query.strip():
            # Модель должна знать, что искали не совсем то, о чём её просили:
            # выдача шире запрошенного, и год/качество надо проверять глазами
            # по именам находок, а не считать, что трекер их уже учёл.
            payload["used_query"] = used_query
        return payload

    def _search_smart_sync(
        self, phrase: str, words: list[str], limit: int
    ) -> dict[str, Any]:
        client = self._client()
        try:
            client.auth_log_in()
            if not [p for p in client.search_plugins() if p.get("enabled")]:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    "поиск по трекерам не настроен: в qBittorrent нет ни одного "
                    "включённого поискового плагина",
                )
            raw, _total = self._run_search_job(client, phrase, SEARCH_SMART_FETCH_LIMIT)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

        broken = [r for r in raw if _is_engine_error(r)]
        usable = [r for r in raw if not _is_engine_error(r)]
        if broken and not usable:
            raise ProtoError(ERR_INTERNAL, f"поиск не отработал: {_engine_error_text(broken[0])}")

        # phrase уже отфильтровала трекер (pattern ищется как непрерывная
        # фраза — то же доверие движку, что и в search). words — доп.
        # AND-фильтр ПОВЕРХ найденного, каждое слово в любом порядке,
        # casefold-подстрока.
        needles = [w.casefold() for w in words]
        matched = [
            r for r in usable if all(n in str(r.get("fileName", "")).casefold() for n in needles)
        ]

        results = [self._map_result(r) for r in matched]
        results.sort(key=lambda r: r["seeders"], reverse=True)
        shown = results[:limit]
        payload: dict[str, Any] = {
            "results": shown,
            "count": len(shown),
            "phrase": phrase,
        }
        if words:
            payload["words"] = words
        if len(results) > len(shown):
            payload["total"] = len(results)
        return payload

    def _add_sync(self, source: str, save_path: str) -> int | None:
        """Свободные байты в целевой директории после приёма раздачи (или
        None — путь недоступен). Раздача не принимается вовсе, если места и
        так почти нет (MIN_FREE_BYTES)."""
        payload: dict[str, Any] = {}
        plugin: str | None = None
        if source.startswith("magnet:"):
            payload = {"urls": source}
        elif not _is_magnet_or_url(source):
            try:
                payload = {"torrent_files": base64.b64decode(source, validate=True)}
            except (binascii.Error, ValueError) as exc:
                raise ProtoError(ERR_BAD_REQUEST, f"невалидный base64 .torrent: {exc}") from exc
        free, _ = _disk_usage(save_path)
        if free is not None and free < MIN_FREE_BYTES:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"мало места в {save_path}: свободно {free / 1024**3:.1f} ГиБ "
                f"(минимум {MIN_FREE_BYTES / 1024**3:.0f} ГиБ) — освободите место "
                "или выберите другую директорию",
            )
        client = self._client()
        try:
            client.auth_log_in()
            if not payload:
                # Осталась http(s)-ссылка: принимаем её ТОЛЬКО как находку
                # своего же поиска (см. докстринг модуля). Отдать её
                # qBittorrent напрямую нельзя — с трекера под логином
                # вернётся страница входа вместо метафайла, а пускать модель
                # качать по произвольному адресу из разговора незачем.
                plugin = self._plugin_for(list(client.search_plugins()), source)
                if plugin is None:
                    raise ProtoError(
                        ERR_BAD_REQUEST,
                        "ссылку на .torrent беру только с трекеров, для которых "
                        "установлен поисковый плагин (иначе нужна magnet-ссылка "
                        "или сам файл)",
                    )
                payload = {"torrent_files": self._fetch_via_plugin(plugin, source)}
            client.torrents_add(save_path=save_path, **payload)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return free

    def _list_sync(self, *, with_hash: bool = False) -> tuple[list[dict[str, Any]], int]:
        """Раздачи + текущий общий лимит скорости (в одной qBittorrent-сессии).

        ``with_hash`` — хэш раздачи по умолчанию наружу не уезжает (см.
        докстринг модуля: сорокасимвольный хэш ничего не даёт модели, только
        раздувает контекст). Ставит его только бот-панель (bot/torrents_view)
        — напрямую через ``args``, а не через объявленный ``ActionParam``
        действия ``list``, так что вызвать это модель не может: тул строит
        схему из ``describe()``, где такого параметра нет.
        """
        client = self._client()
        try:
            client.auth_log_in()
            # sort/limit делает сам qBittorrent — тащить сотню раздач по сети,
            # чтобы выбросить хвост здесь, незачем.
            raw = client.torrents_info(sort="dlspeed", reverse=True, limit=LIST_LIMIT)
            limit_bytes = client.transfer_download_limit()
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        torrents = []
        for t in raw:
            item: dict[str, Any] = {
                "name": t.get("name", "?"),
                "state": t.get("state", "?"),
                # progress приходит долей 0..1 — в процентах читают и человек,
                # и модель, а округление до целых убирает шум вида 0.9999.
                "progress_pct": round(float(t.get("progress", 0.0)) * 100),
                "dlspeed_bytes_s": int(t.get("dlspeed", 0)),
                "upspeed_bytes_s": int(t.get("upspeed", 0)),
                # Сырое state — внутренние строки qBittorrent («stalledUP»,
                # «stoppedDL»), по которым не человеку, не модели не очевидно,
                # стоит раздача или нет; а решение «ставить на паузу или,
                # наоборот, запускать» именно на этом и держится. Префиксов
                # два: qBittorrent 5 переименовал paused* в stopped*.
                "paused": str(t.get("state", "")).startswith(("paused", "stopped")),
                # -1/огромное значение у qBittorrent означает "неизвестно"
                # (раздача стоит или сидируется) — отдаём None, чтобы модель не
                # пересказывала 8640000 секунд как реальную оценку.
                "eta_s": _eta_or_none(t.get("eta")),
                # Подключённые пиры (не размер всего роя — то, что реально
                # качает/раздаёт эту раздачу прямо сейчас).
                "seeds": max(0, int(t.get("num_seeds", 0) or 0)),
                "peers": max(0, int(t.get("num_leechs", 0) or 0)),
                # Unix-время добавления — бот сортирует список по нему внутри
                # групп статуса (bot/torrents_view.py::_sort_key); человеку и
                # модели просто число, страшного в нём нет.
                "added_on": int(t.get("added_on", 0) or 0),
            }
            if with_hash:
                item["hash"] = str(t.get("hash", ""))
            torrents.append(item)
        # 0 — общесистемное соглашение qBittorrent для «без ограничения».
        limit_mbps = int(limit_bytes) // (1024 * 1024) if limit_bytes else 0
        return torrents, limit_mbps

    def _set_speed_limit_sync(self, mbps: int) -> None:
        """Общий лимит (download И upload разом, одним значением — отдельных
        направлений пользователь не просил) — см. докстринг модуля."""
        limit_bytes = mbps * 1024 * 1024
        client = self._client()
        try:
            client.auth_log_in()
            client.transfer_set_download_limit(limit=limit_bytes)
            client.transfer_set_upload_limit(limit=limit_bytes)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

    @staticmethod
    def _select(raw: list[Any], selector: str) -> list[dict[str, Any]]:
        """Раздачи, которые имел в виду вызывающий (см. докстринг модуля).

        Порядок проб — от самого однозначного к самому свободному: явное
        «все» → точное имя → хэш → узнаваемая часть имени. Без этого порядка
        точное имя, оказавшееся подстрокой другого («Foo» при живом
        «Foo.S02»), считалось бы неоднозначным, хотя вызывающий назвал его
        целиком.
        """
        needle = selector.strip().lower()
        if needle in ALL_SELECTORS:
            return list(raw)
        exact = [t for t in raw if str(t.get("name", "")).lower() == needle]
        if exact:
            return exact
        by_hash = [t for t in raw if str(t.get("hash", "")).lower() == needle]
        if by_hash:
            return by_hash
        return [t for t in raw if needle in str(t.get("name", "")).lower()]

    def _switch_sync(self, selector: str, *, pause: bool) -> list[str]:
        client = self._client()
        try:
            client.auth_log_in()
            raw = list(client.torrents_info())
            picked = self._select(raw, selector)
            if not picked:
                known = ", ".join(str(t.get("name", "?")) for t in raw[:LIST_LIMIT])
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"не нашёл раздачу «{selector}» (сейчас есть: {known or 'ни одной'})",
                )
            if len(picked) > 1 and selector.strip().lower() not in ALL_SELECTORS:
                names = ", ".join(str(t.get("name", "?")) for t in picked)
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"под «{selector}» подходит несколько раздач ({names}) — "
                    "назовите одну точнее или скажите «все»",
                )
            hashes = [str(t.get("hash", "")) for t in picked]
            # torrents_pause/_resume — версионно-безопасные псевдонимы: в
            # qBittorrent 5 эти эндпойнты называются stop/start, и библиотека
            # сама выбирает нужный по app_web_api_version.
            if pause:
                client.torrents_pause(torrent_hashes=hashes)
            else:
                client.torrents_resume(torrent_hashes=hashes)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return [str(t.get("name", "?")) for t in picked]

    def _space_sync(self) -> dict[str, Any]:
        dirs = []
        for path in self._cfg.save_dirs:
            free, total = _disk_usage(path)
            entry: dict[str, Any] = {"path": path, "free_bytes": free, "total_bytes": total}
            if free is None:
                entry["error"] = "путь недоступен (диск не смонтирован?)"
            dirs.append(entry)
        # Сколько ещё предстоит докачать уже принятым раздачам — лучший
        # ответ на «а хватит ли места»: свободное место само по себе не
        # учитывает того, что уже обещано текущим закачкам. Best-effort:
        # молчащий qBittorrent не должен отнимать ответ про сами диски.
        left: int | None = None
        try:
            client = self._client()
            try:
                client.auth_log_in()
                left = sum(int(t.get("amount_left", 0) or 0) for t in client.torrents_info())
            finally:
                client.auth_log_out()
        except qbittorrentapi.APIError:
            left = None
        return {"dirs": dirs, "downloading_left_bytes": left, "min_free_bytes": MIN_FREE_BYTES}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_LIST:
            torrents, limit_mbps = await asyncio.to_thread(
                self._list_sync, with_hash=bool(args.get("with_hash"))
            )
            return {
                "torrents": torrents,
                "count": len(torrents),
                "limit": LIST_LIMIT,
                "speed_limit_mbps": limit_mbps,
            }
        if action == ACTION_SPEED_LIMIT:
            mbps = int(args.get("mbps") or 0)
            if not 0 <= mbps <= MAX_SPEED_LIMIT_MBPS:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"лимит — от 0 (без ограничения) до {MAX_SPEED_LIMIT_MBPS} МБ/с",
                )
            await asyncio.to_thread(self._set_speed_limit_sync, mbps)
            return {"speed_limit_mbps": mbps}
        if action == ACTION_SPACE:
            return await asyncio.to_thread(self._space_sync)
        if action == ACTION_DETAILS:
            page = str(args.get("page") or "").strip()
            if not page.startswith(("http://", "https://")):
                raise ProtoError(ERR_BAD_REQUEST, "не указана ссылка на страницу раздачи (page)")
            return await asyncio.to_thread(self._details_sync, page)
        if action == ACTION_SEARCH:
            query = str(args.get("query") or "").strip()
            if not query:
                raise ProtoError(ERR_BAD_REQUEST, "не указан поисковый запрос (query)")
            limit = min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT)
            return await asyncio.to_thread(self._search_sync, query, limit)
        if action == ACTION_SEARCH_SMART:
            phrase = str(args.get("phrase") or "").strip()
            if not phrase:
                raise ProtoError(ERR_BAD_REQUEST, "не указана точная фраза для поиска (phrase)")
            words = str(args.get("words") or "").split()
            limit = min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT)
            return await asyncio.to_thread(self._search_smart_sync, phrase, words, limit)
        if action in (ACTION_PAUSE, ACTION_RESUME):
            selector = str(args.get("name") or "").strip()
            if not selector:
                raise ProtoError(ERR_BAD_REQUEST, "не указано имя раздачи (name)")
            names = await asyncio.to_thread(
                self._switch_sync, selector, pause=action == ACTION_PAUSE
            )
            key = "paused" if action == ACTION_PAUSE else "resumed"
            return {key: names, "count": len(names)}
        if action != ACTION_ADD:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        source = str(args.get("source") or "")
        name = str(args.get("name") or "торрент")
        save_path = str(args.get("save_path") or "")
        if not source:
            raise ProtoError(ERR_BAD_REQUEST, "не указан source (magnet-ссылка или файл)")
        if save_path not in self._cfg.save_dirs:
            known = ", ".join(self._cfg.save_dirs) or "нет доступных директорий"
            raise ProtoError(
                ERR_BAD_REQUEST, f"недопустимая директория: {save_path!r} (есть: {known})"
            )
        free = await asyncio.to_thread(self._add_sync, source, save_path)
        return {"name": name, "save_path": save_path, "free_bytes": free}
