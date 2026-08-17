"""Конфигурация приложения: pydantic-модели + загрузка из TOML с env-оверрайдом.

Источник правды — TOML-файл; любое значение переопределяется переменной
окружения с префиксом ``SENTINEL__`` и разделителем вложенности ``__``.
Подписки задаются только в TOML (env-оверрайд списков не поддерживается
сознательно — см. ARCHITECTURE.md §4.5).
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from sa_home_bot.node.instances import (
    INSTANCES_DIRNAME,
    PACKAGE_SUFFIX,
    guests_package_path,
)
from sa_home_bot.node.kind import NodeKind

log = logging.getLogger(__name__)


def unknown_config_keys(
    data: dict[str, Any], model: type[BaseModel], prefix: str = ""
) -> list[str]:
    """Дотированные пути полей TOML, которых нет в моделях, — почти всегда опечатки.

    Конфиг сознательно терпим к лишним полям (extra="ignore": старый код
    должен переживать конфиг более новой версии), поэтому опечатка вида
    ``assigments`` молча включает дефолт. Этот обход находит такие поля,
    чтобы load() их хотя бы прокричал в лог.
    """
    unknown: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown.append(prefix + key)
            continue
        annotation = field.annotation
        if (
            isinstance(value, dict)
            and isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
        ):
            unknown += unknown_config_keys(value, annotation, f"{prefix}{key}.")
        elif isinstance(value, list):
            args = get_args(annotation)
            item_type = args[0] if args else None
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        unknown += unknown_config_keys(item, item_type, f"{prefix}{key}[{i}].")
    return unknown


class TelegramConfig(BaseModel):
    token: str = ""
    # Прокси для исходящих вызовов Bot API (aiogram/aiohttp-socks), напр.
    # "socks5://100.111.4.42:1080" — на случай блокировки api.telegram.org
    # с этой ноды напрямую. Пусто — соединение без прокси (по умолчанию).
    proxy: str = ""


class DatabaseConfig(BaseModel):
    path: Path = Path("./data/sentinel.sqlite")


class ScheduleConfig(BaseModel):
    scan_cron: str = "*/1 * * * *"
    smart_cron: str = "0 * * * *"  # снимок SMART-счётчиков дисков раз в час
    housekeeping_cron: str = "0 3 * * *"


class _BaselineParams(BaseModel):
    """Общие поля выбора политики порогов и параметров baseline.

    ``mode="fixed"`` (по умолчанию) — фиксированные warn/crit. ``mode="baseline"``
    включает адаптивный порог: ``min(warn_c, mean + k_sigma * max(std, min_std))``
    по последним ``baseline_window`` показаниям. Пока накоплено меньше
    ``baseline_min_samples`` — используется фиксированный warn_c (холодный старт).
    Baseline только повышает чувствительность; warn_c остаётся верхней страховкой.
    """

    mode: Literal["fixed", "baseline"] = "fixed"
    baseline_window: int = Field(default=240, ge=1)
    baseline_min_samples: int = Field(default=30, ge=1)
    baseline_k_sigma: float = Field(default=4.0, gt=0)
    baseline_min_std_c: float = Field(default=3.0, ge=0)


class CpuSensorConfig(_BaselineParams):
    enabled: bool = True
    warn_c: float = 80.0
    crit_c: float = 90.0
    hysteresis_delta_c: float = 5.0
    consecutive_to_alert: int = Field(default=3, ge=1)
    consecutive_to_clear: int = Field(default=3, ge=1)


class DiskSensorConfig(_BaselineParams):
    enabled: bool = True
    warn_c: float = 55.0
    crit_c: float = 65.0
    hysteresis_delta_c: float = 5.0
    consecutive_to_alert: int = Field(default=2, ge=1)
    consecutive_to_clear: int = Field(default=2, ge=1)
    devices: list[str] = Field(default_factory=list)


class GpuSensorConfig(_BaselineParams):
    """Температура GPU через `nvidia-smi` (см. sensors/gpu.py).

    ``enabled`` по умолчанию выключен (в отличие от cpu/disks): в отличие от
    процессора и дисков, которые есть на КАЖДОЙ ноде, видеокарта — редкое
    исключение (на 2026-08 — только mycraft, Tesla V100 + RTX 3060). Держать
    датчик включённым по умолчанию значило бы шуметь «nvidia-smi не найден»
    на каждой ноде без GPU — включается явно в конфиге той машины, где карта
    есть.
    """

    enabled: bool = False
    warn_c: float = 80.0
    crit_c: float = 90.0
    hysteresis_delta_c: float = 5.0
    consecutive_to_alert: int = Field(default=3, ge=1)
    consecutive_to_clear: int = Field(default=3, ge=1)


class LhmSensorConfig(BaseModel):
    """LibreHardwareMonitor — источник температур на Windows (`sensors/lhm.py`).

    ``dll_path`` — путь к LibreHardwareMonitorLib.dll; пусто — поиск по
    типовым местам (%LOCALAPPDATA%\\sa-home-bot, Program Files). На Linux
    секция игнорируется.
    """

    dll_path: str = ""


class SensorsConfig(BaseModel):
    cpu: CpuSensorConfig = Field(default_factory=CpuSensorConfig)
    gpu: GpuSensorConfig = Field(default_factory=GpuSensorConfig)
    disks: DiskSensorConfig = Field(default_factory=DiskSensorConfig)
    lhm: LhmSensorConfig = Field(default_factory=LhmSensorConfig)


class WakeConfig(BaseModel):
    """Wake-on-LAN для внешней машины (например, домашнего ПК).

    Пустой ``mac`` = функция выключена (/wake ответит «не настроено»).
    ``ip`` опционален: если задан, /wake сначала проверит, не в сети ли машина
    уже, а после отправки magic packet подождёт ответа на ping.
    """

    mac: str = ""
    ip: str = ""
    broadcast: str = "255.255.255.255"
    port: int = Field(default=9, ge=1, le=65535)
    wait_timeout_s: float = Field(default=120.0, gt=0)


class MonitorConfig(BaseModel):
    """Служба monitor (отдельный процесс, `sa-home-bot --service monitor`).

    ``socket`` — endpoint протокола v0 (unix-путь или ``tcp://host:port``,
    см. PROTOCOL.md), через который бот (и позже сервис ноды) общается
    с монитором. ``db_path`` — собственная БД монитора (readings,
    health_states, SMART, job_runs); БД бота остаётся отдельной.
    """

    socket: str = "./data/monitor.sock"
    db_path: Path = Path("./data/monitor.sqlite")


class AppConfig(BaseModel):
    """Одно приложение под присмотром службы apps (умение роя).

    ``id`` — идентификатор действия в describe (и право ``id@apps``),
    ``unit`` — системный systemd-юнит, ``urls`` — ссылки на веб-морду.
    """

    id: str
    title: str
    unit: str
    urls: list[str] = Field(default_factory=list)


class AppsConfig(BaseModel):
    """Служба apps (адаптер приложений, `sa-home-bot --service apps`).

    Умения роя поверх готового софта (торрент, медиасервер): служба отвечает
    по протоколу v0 состоянием systemd-юнита и ссылками на веб-морду. Бот сам
    в систему не ходит — только запросы к этой службе.
    """

    socket: str = "./data/apps.sock"
    items: list[AppConfig] = Field(default_factory=list)


class TorrentsConfig(BaseModel):
    """Служба torrents (адаптер qBittorrent, `sa-home-bot --service torrents`).

    Умение роя «добавить торрент по .torrent-файлу/magnet-ссылке из чата» —
    в отличие от apps (systemd start/stop/status), здесь бот реально
    проксирует данные в Web API готового клиента. ``save_dirs`` — конечный
    список директорий, которые можно предложить пользователю кнопками
    (ActionParam.choices, PROTOCOL.md); порядок важен — callback-кнопки в
    боте адресуют директорию по индексу в этом списке.
    """

    socket: str = "./data/torrents.sock"
    qbittorrent_url: str = "http://127.0.0.1:8080"
    qbittorrent_user: str = ""
    qbittorrent_password: str = ""
    save_dirs: list[str] = Field(default_factory=list)
    # Каталог поискового движка qBittorrent (`nova2dl.py`, `engines/`) — им
    # служба качает метафайлы с трекеров под логином (см. докстринг
    # torrents/service.py). Пусто — путь по умолчанию для Linux-сборки
    # (`~/.local/share/qBittorrent/nova3`); задавать нужно, только если
    # qBittorrent живёт под другим пользователем или в другой ОС.
    search_engine_dir: str = ""


class MemoryConfig(BaseModel):
    """Служба memory (`sa-home-bot --service memory`) — долгая память Альфреда
    о чате (факты и предпочтения, которые модель иначе забывает между
    разговорами; см. memory/service.py).

    ``db_path`` — своя БД (как у tasks и monitor): служба живёт отдельным
    процессом и к БД бота отношения не имеет. Память привязана к чату, общего
    хранилища на весь дом нет — решение пользователя 2026-07-29.

    ``family_chat_ids`` — чаты, которым доступен scope="family" (решение
    пользователя 2026-08-03): факт, записанный с этим scope из ЛЮБОГО чата
    из списка, виден во ВСЕХ чатах из списка — в отличие от scope="common"
    (виден вообще всем, включая случайных гостей). Список правит человек
    руками (как и сам scope="family" — тул модели его не даёт, см.
    memory/service.py, по аналогии с common).

    С решением 2026-08-04 этот список — не единственный источник доступа:
    гость с флагом ``family`` (``/guests``, ``Subscription.family``,
    ``bot/invites.py::Gatekeeper.set_guest_family``) получает тот же доступ
    к scope="family", не будучи вписан сюда руками — служба memory узнаёт об
    этом флагом ``guest_family`` в каждом запросе (см.
    ``memory/service.py::_is_family_chat``, служба-процесс своего доступа к
    гостевым подпискам не имеет).
    """

    socket: str = "./data/memory.sock"
    db_path: Path = Path("./data/memory.sqlite")
    family_chat_ids: list[int] = Field(default_factory=list)


class TasksConfig(BaseModel):
    """Служба tasks (`sa-home-bot --service tasks`) — генерализованные
    отложенные задачи роя (замена старого тула remind, писавшего прямо в
    БД бота константным текстом, живая находка 2026-07-24: пользователь
    явно попросил отдельный сервис роя, а не доработку внутри бота).

    Задача = due_at + произвольная протокольная команда (dst_node/
    dst_service/action/args) + непрозрачные meta, которые эта служба не
    читает, только хранит и возвращает целиком в событии `task_result`
    вызывающему (например боту — bot/node_events.py). Один специальный
    action — ``chat_loop`` (см. tasks/protocol.py) — не форвардится как
    есть, а прогоняется через полный цикл tool-calling поверх llm.chat
    (sa_home_bot.llm_chat.run_chat_loop): это единственный сейчас
    предусмотренный «богатый» тип задачи — тул remind (bot/tools.py)
    создаёт именно такие, в том числе может создать сама модель во время
    собственного ответа (self-scheduling).

    ``db_path`` — своя БД (как у monitor), не БД бота: у этой службы нет
    доступа к Telegram и к диалогам бота, только к своей очереди задач.
    """

    socket: str = "./data/tasks.sock"
    db_path: Path = Path("./data/tasks.sqlite")


class NetConfig(BaseModel):
    """Служба net (`sa-home-bot --service net`) — веб-поиск через локальный
    SearXNG (LLM_INTEGRATION_PLAN.md §9).

    Сам SearXNG ставится на ноду отдельно (venv + uWSGI под systemd) и в
    ``settings.yml`` ему нужно ЯВНО включить формат json — по умолчанию он
    выключен, и служба получила бы HTML вместо структурированного ответа.

    Redis, который упоминался в исходном плане как эфемерный кэш, сознательно
    не заводим: на alfred слабый CPU и отдельно отслеживаемый износ eMMC, а
    limiter SearXNG (единственное, ради чего Redis нужен по-настоящему) не
    требуется — слушаем только localhost, запросов снаружи нет.

    ``max_results`` — сколько результатов отдавать: ответ целиком уезжает в
    контекст модели, полная выдача там не нужна и стоит токенов.
    """

    socket: str = "./data/net.sock"
    searxng_url: str = "http://127.0.0.1:8888"
    request_timeout_s: float = Field(default=20.0, gt=0)
    # Проба живости в get_state — короче рабочего таймаута: карточка службы не
    # должна висеть 20 секунд из-за мёртвого поисковика.
    probe_timeout_s: float = Field(default=5.0, gt=0)
    max_results: int = Field(default=5, ge=1, le=20)
    language: str = "ru"


class LlmConfig(BaseModel):
    """Служба llm (Альфред, `sa-home-bot --service llm`) — на winpc (GPU,
    WSL2+Docker) и на Linux-нодах с постоянно поднятой нативной Ollama
    (mycraft, Tesla V100 16 ГБ, с 2026-08-02).

    ``container_backend`` — как служба управляет жизнью Ollama:
    ``"wsl-docker"`` (дефолт, winpc) — Ollama живёт в Docker-контейнере
    внутри WSL2, служба сама поднимает/гасит их через `wsl.exe`/`docker`
    (см. llm/ollama.py::ensure_running/stop, живая находка 2026-07-23 —
    WSL не запускается из-под Session 0, поэтому службу поднимает
    deploy/llm-runner.ps1, а не супервизор ноды). ``"native"`` (Linux) —
    Ollama установлена как systemd-сервис и всегда поднята сама
    (Restart=on-failure), служба её не стартует/не гасит — вместо
    "усыпить контейнер" `stop()` шлёт Ollama явный `keep_alive: 0`,
    выгружая модель из RAM немедленно, не трогая сам процесс.

    ``ollama_url`` — loopback-адрес Ollama на этой же машине (см. §0
    LLM_INTEGRATION_PLAN.md: наружу это никогда не смотрит, только служба →
    Ollama локально). ``wsl_distro``/``ollama_container`` — имена,
    зафиксированные при ручной настройке инфраструктуры (см. документ выше,
    §1); используются только при ``container_backend = "wsl-docker"``.
    ``request_timeout_s`` — таймаут ответа `ask`/`chat` по протоколу
    роя (генерация, в т.ч. с холодным стартом WSL/контейнера, дольше
    типичных «быстрых» действий — см. Envelope.timeout_s в proto/messages.py).
    ``idle_sleep_after_s`` — после стольки секунд без запросов служба сама
    останавливает контейнер (освобождает VRAM) и, если за это время были
    чаты с запросами, эмитит событие `llm_idle_sleep` со списком их
    chat_id — бот (bot/node_events.py) шлёт туда закрывающее сообщение.

    ``think_chat`` — ДЕФОЛТ thinking-режима qwen3 для действия `chat`, когда
    вызывающий не указал `args["think"]` явно. Живая находка 2026-07-24:
    сначала стоял False ради скорости, затем True — на формуле (площадь
    цилиндра) без рассуждения модель путалась в арифметике; но включать
    его на КАЖДЫЙ запрос оказалось расточительно (даже "привет" занимал
    30-40с). Решение — вариативное рассуждение (см. bot/ai_flow.py):
    сначала быстрый проход с think=false и явной инструкцией модели самой
    попросить подумать (маркер), если вопрос того требует; думающий проход
    — только тогда, think=true. bot/ai_flow.py теперь всегда передаёт
    `think` явно на каждый вызов chat — этот конфиг остаётся чистым
    фоллбэком на случай вызова chat без явного think (см. llm/ollama.py).

    ``num_ctx`` — окно контекста Ollama, явно фиксированное (живая находка
    2026-07-24): раньше нигде не передавалось, работал дефолт модели/Ollama,
    который может оказаться меньше, чем реально нужно системному промпту
    Альфреда (~2900 ток.) + декларациям тулов (~1000-1200 ток. у собеседника
    с полными правами, bot/tools.py::tools_for) + растущей истории — риск
    тихого обрезания старых сообщений в длинных чатах. 8192 — с запасом;
    при нехватке памяти на winpc можно уменьшить.

    Живая находка 2026-07-24 (вторая, тем же заходом): ``idle_sleep_after_s``
    управляет только жизнью WSL2-VM/контейнера — саму МОДЕЛЬ внутри Ollama
    это не трогает. Ollama по умолчанию (без явного `keep_alive` в запросе)
    сама выгружает модель из памяти через 5 минут после последнего ответа,
    независимо от контейнера — служба об этом не узнаёт вообще (`_asleep`
    не выставляется, `llm_idle_sleep` не эмитится), пользователь видит
    только "модель зачем-то выгружается за несколько минут" без единого
    сообщения в характере ни при уходе, ни при возврате. Явный `keep_alive`
    в каждом запросе (см. llm/ollama.py) решает это по построению —
    используем то же `idle_sleep_after_s`, не заводя отдельное поле:
    Ollama и sa-home-bot держат модель тёплой одинаковое время и не
    расходятся в понимании, когда наступил сон.
    """

    socket: str = "./data/llm.sock"
    container_backend: str = "wsl-docker"  # "wsl-docker" | "native"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:14b"
    wsl_distro: str = "Docker"
    ollama_container: str = "ollama"
    request_timeout_s: float = Field(default=240.0, gt=0)
    idle_sleep_after_s: float = Field(default=1800.0, gt=0)
    # Сколько ждать прогрева модели, прежде чем счесть службу неготовой
    # (wake_core.try_warmup). Отдельно от request_timeout_s, потому что
    # платится за ХОЛОДНЫЙ старт: замер 2026-07-30 на winpc — 201 с на
    # загрузку 35B-модели с диска, при прежних 180 с отложенные задачи
    # проваливались, хотя модель поднималась. Железо у нод разное, поэтому
    # настройка, а не константа.
    warmup_timeout_s: float = Field(default=360.0, gt=0)
    think_chat: bool = True
    num_ctx: int = Field(default=8192, gt=0)
    # Режим работы bot/ai_flow.py::request_alfred с моделью — разные модели
    # умеют разное, гонять их через одну и ту же схему нельзя.
    # ``"router_think"`` (дефолт, как было всегда) — два прохода: лёгкий
    # router без персонажа решает, нужно ли думать (THINK_MARKER, см.
    # llm/prompt.py), затем персонажный проход с уже известным think.
    # ``"single_call"`` — один персонажный проход сразу, без router (роутер
    # решает ТОЛЬКО думать или нет — если у модели этот выбор недоступен,
    # решать нечего, а второй вызов на каждое сообщение остаётся чистым
    # расходом). Указывать per-node/per-model в config.toml, а не константой
    # в коде — модели умеют разное.
    #
    # Живая находка 2026-08-02: gemma-4-26B-A4B (mycraft) на любой think=true
    # отвечает `400 {"error":"... does not support thinking"}` — отсюда был
    # сделан вывод «модель не умеет думать» и включён single_call.
    # ОШИБКА ДИАГНОЗА, разобрано 2026-08-10 (см. single_call_think ниже):
    # 400 приходит только на попытку ВКЛЮЧИТЬ размышление явно, а думает эта
    # модель и без всякого флага — по умолчанию и на каждое сообщение.
    mode: Literal["router_think", "single_call"] = "router_think"
    # Значение поля think для единственного прохода в режиме "single_call".
    # None (дефолт) — не слать флаг вообще, прежнее поведение.
    #
    # Живая находка 2026-08-10 (замер на mycraft, Ollama 0.32.3): «не слать
    # флаг» НЕ равно «не думать». gemma-4-26B-A4B на голом «Привет!» без
    # флага сгенерировала 222 токена, из которых видимого ответа — пять
    # слов, а остальное ушло в невидимый message.thinking (757 символов
    # рассуждений о том, как поздороваться); в проде это давало 23 секунды
    # на ответ «Здравствуйте, сэр» при нуле вызовов тулов. Тот же запрос с
    # think=false — 9 токенов и 1.7 с вместо ~48 с. Prefill тут ни при чём:
    # он идёт 640-1355 ток/с при работающем prefix-кэше, всё время съедала
    # генерация невидимых токенов на 70 ток/с.
    #
    # Почему настройка, а не False в коде: для qwen3.5/3.6 явный think=false
    # уже ломал качество (живая находка 2026-07-25, llm/ollama.py::chat) —
    # у них своя адаптивная логика, которой лучше не мешать. Рой разнороден,
    # решение per-node.
    single_call_think: bool | None = None
    # Чем в режиме "router_think" выражается решение роутера «надо думать».
    # ``"flag"`` (дефолт, как было всегда) — думать: think=true, не думать:
    # think=false. Так умеют qwen3.5/3.6.
    # ``"implicit"`` — думать: НЕ слать флаг вовсе (модель уходит в
    # размышление сама, это её поведение по умолчанию), не думать:
    # think=false. Для моделей вроде gemma-4, которые на явный think=true
    # отвечают 400, но прекрасно понимают think=false (см. single_call_think
    # выше) — именно этот стиль возвращает им вариативное рассуждение,
    # которого они казались лишены.
    think_style: Literal["flag", "implicit"] = "flag"
    # Как отправлять ответ Альфреда пользователю (этап 34, Фаза 2 —
    # IMPLEMENTATION_PLAN.md). ``"rich"`` (дефолт, решение пользователя
    # 2026-08-09) — Telegram Bot API 10.1/10.2 Rich Messages: в приватных
    # чатах (основной сценарий — Private Chat Topics) текст стримится
    # черновиком (sendRichMessageDraft, живая анимация) и в конце
    # персистится sendRichMessage; в группах/супергруппах streaming-черновик
    # платформенно недоступен (Bot API ограничивает sendRichMessageDraft
    # приватным чатом) — там один цельный sendRichMessage без анимации, но
    # с тем же форматированием (таблицы, код, списки). Никакого смешивания
    # с обычным editMessageText внутри одного ответа — решение пользователя.
    # ``"typing_plain"`` — прежнее поведение (typing-индикатор + обычный
    # HTML-текст с чанкованием по 4096, bot/notifier.py) — путь отката на
    # случай проблем с рендером у конкретного клиента: известный живой баг
    # платформы на момент написания — Telegram Desktop не показывает
    # sendRichMessageDraft-стрим (iOS/Android — нормально), Telegram Web
    # вообще не рендерит sendRichMessage. Переключается per-node в
    # config.toml, не глобальная константа — семья может сидеть на разных
    # клиентах.
    response_mode: Literal["rich", "typing_plain"] = "rich"
    # Живая находка 2026-07-25: текст персонажа (тон, характер, конкретные
    # реплики) намеренно НЕ в репозитории — слишком личный/объёмный для
    # config.toml. На практике заполняется не здесь напрямую, а отдельным
    # локальным файлом llm-prompt.toml рядом с config.toml (см.
    # _load_persona_prompt ниже) — держать десятки строк персонажа среди
    # обычных настроек было неудобно. Поле здесь — просто пункт назначения:
    # можно заполнить и прямо в config.toml, если отдельный файл не нужен.
    # DEFAULT_PERSONA_PROMPT (llm/prompt.py) — безликая заглушка на случай,
    # если ни то ни другое не заполнено (свежий чекаут, CI).
    persona_prompt: str = ""
    # Логопед (llm/speech_therapy.py) — состояние вероятностной, излечимой
    # картавости Альфреда: вероятность искажения слова с «р», счётчик
    # коррекций, исключённые слова. Тот же паттерн относительного пути, что
    # у node/state.py — per-process, не singleton на весь рой.
    speech_therapy_state_path: str = "./data/speech-therapy-state.json"
    # Чаты, где картавость закреплена на 100% навсегда, независимо от
    # общего прогресса лечения (и даже уже после полного излечения для
    # остальных чатов) — решение пользователя 2026-08-03.
    speech_therapy_pinned_chat_ids: list[int] = Field(default_factory=list)
    # Мультимодальный /ai (2026-08-10) — ресайз/хранение уменьшенных фото
    # намеренно на СТОРОНЕ службы llm (см. llm/vision.py), а не бота: бот
    # (alfred) шлёт сюда сырые байты как есть, эта нода (сейчас mycraft,
    # сильнее CPU, уже держит Ollama/Tesla V100) сама делает Pillow-ресайз и
    # хранит результат у себя же — тулу look_at_photo не нужно ничего
    # пересылать заново. ``./data/`` — та же относительная директория, что
    # у ``socket`` выше: реально окажется на диске узла, где физически
    # запущена служба.
    photos_dir: Path = Path("./data/ai-photos")
    # Длинная сторона после ресайза — SigLip-энкодер Gemma кодирует
    # фиксированным входом (~896×896), слать крупнее — трата полосы без
    # пользы для качества ответа.
    photo_max_edge_px: int = Field(default=896, gt=0)
    photo_jpeg_quality: int = Field(default=80, ge=1, le=100)
    # TTL уменьшенных копий на диске — уборка ленивая (llm/vision.py),
    # запускается не чаще раза в сутки при сохранении нового фото, отдельный
    # периодический планировщик под это заводить избыточно.
    photo_ttl_days: int = Field(default=30, gt=0)
    # Голосовые сообщения /ai: распознавание тоже на стороне службы llm (не
    # бота), как и фото, но CPU-движком faster-whisper — не Ollama/Gemma:
    # аудио-вход Gemma 4 через Ollama на практике сломан (галлюцинации,
    # минуты обработки секунд аудио — живая находка при выборе архитектуры
    # 2026-08-10). CPU, не GPU: V100 занята основной моделью почти под
    # завязку (15.4 из 16.4 ГБ при загрузке), второй модели там просто нет
    # места без выгрузки первой — а Xeon E5-2678 v3 (12 ядер/24 потока)
    # справляется сам, не трогая VRAM вовсе. См. llm/stt.py.
    stt_model_size: str = "medium"  # tiny/base/small/medium/large-v3/distil-large-v3
    stt_compute_type: str = "int8"  # квантизация CPU-инференса faster-whisper
    stt_cpu_threads: int = Field(default=8, gt=0)
    stt_language: str = "ru"
    # Длиннее — вежливый отказ ДО скачивания и похода в рой (bot/voice_stt.py).
    stt_max_duration_s: float = Field(default=600.0, gt=0)
    stt_request_timeout_s: float = Field(default=300.0, gt=0)
    stt_tmp_dir: Path = Path("./data/stt-tmp")
    stt_model_dir: Path = Path("./data/stt-models")
    # Голосовые ОТВЕТЫ /ai (синтез, llm/tts.py) — тоже на стороне службы llm,
    # тот же CPU-принцип, что у STT, но другая модель: Coqui XTTS v2 (voice
    # cloning по референсному клипу), не faster-whisper. Голос Альфреда — не
    # выбор из готового набора дикторов, а один референсный WAV-образец
    # (6-10с чистой речи, без дефектов в самой записи — картавость персонажа
    # добавляется отдельно через подмену букв в тексте, см.
    # llm/speech_therapy.py, синтезатору она не нужна). Решение пользователя
    # 2026-08-11: пока финальный голос не выбран — временный референс в духе
    # Володарского/Гаврилова. Смена референса — просто замена файла по этому
    # пути, без перезапуска модели (speaker_wav передаётся при каждом вызове).
    tts_language: str = "ru"
    tts_reference_voice_path: Path = Path("./data/tts-voice-ref/alfred.wav")
    tts_max_text_chars: int = Field(default=2000, gt=0)
    # XTTS v2 на CPU заметно медленнее faster-whisper (RTF примерно 1-2 даже
    # на серверном Xeon) — таймаут кратно больше stt_request_timeout_s.
    tts_request_timeout_s: float = Field(default=240.0, gt=0)
    tts_tmp_dir: Path = Path("./data/tts-tmp")
    tts_model_dir: Path = Path("./data/tts-models")
    tts_opus_bitrate: str = "32k"


def resolve_think(llm: LlmConfig, *, needs_think: bool) -> bool | None:
    """Единая точка, чем ``needs_think`` (хочет ли вызывающий рассуждение)
    выражается в поле ``think`` запроса к Ollama — см. ``LlmConfig.think_style``.

    Живая находка 2026-08-10 (вторая тем же заходом): эта логика раньше была
    списана вручную в трёх местах (bot/ai_flow.py::request_alfred — оба
    прохода; bot/tools.py::tool_remind — срабатывание отложенного
    напоминания), и когда завели ``think_style`` для устранения скрытого
    thinking у gemma, поправили только ai_flow.py — bot/tools.py остался со
    старой формулой (``think_chat`` напрямую, без оглядки на style) и
    продолжил слать явный ``true`` на срабатывании каждого напоминания,
    ловя тот же самый 400 "does not support thinking", который весь этот
    механизм и должен был устранить. Одна функция вместо трёх копий —
    единственный способ не разъезжаться так снова.

    Не покрывает ``mode="single_call"`` — там нет решения "нужно/не нужно",
    вызывающие берут ``single_call_think`` напрямую (см. LlmConfig)."""
    if not needs_think:
        return False
    return None if llm.think_style == "implicit" else True


class VpnConfig(BaseModel):
    """Служба vpn (`sa-home-bot --service vpn`) — AmneziaWG-доступ на jeeves,
    выдаваемый и учитываемый через бота (Этап 33 IMPLEMENTATION_PLAN.md).

    ``interface``/``subnet`` — интерфейс и подсеть, поднятые ops-скриптом
    (``deploy/setup-awg-jeeves.sh``) отдельно от кода ноды: сама служба
    интерфейс не создаёт, только читает/пишет пиров через ``awg`` под узким
    sudoers (``node/fixups.py::AWG_SUDOERS``). ``jc``/``jmin``/``jmax``/
    ``s1``/``s2``/``h1``-``h4`` — параметры обфускации AmneziaWG: ОБЯЗАНЫ
    совпадать с тем, что реально прописано в серверном ``awg0.conf`` (их
    подбирают один раз при установке, не на лету).

    ``base_quota_gb`` — базовая месячная квота гостя; ``extra_step_gb`` —
    шаг самостоятельной докупки кнопкой «+100 ГБ»; ``self_ceiling_gb`` —
    потолок самообслуживания (решение пользователя 2026-08-03: до этого
    порога гость добавляет трафик сам, выше — только заявкой админу).
    ``warn_remaining_gb`` — двойная роль (решение пользователя 2026-08-03):
    на скольки гигабайтах ДО исчерпания слать предупреждение, И порог,
    НИЖЕ которого вообще открыто самообслуживание («+100 ГБ» без
    подтверждения админа) — гость с ещё почти полной квотой докупить
    заранее не может, только когда трафик реально заканчивается
    (см. vpn/service.py::_grant_extra). Не процент, а абсолютный остаток.
    ``node_limit_gb`` — общий месячный лимit канала VDS по тарифу
    (сообщено пользователем 2026-08-03: 10 ТБ) — при приближении событие
    уходит админам, гостей не касается. Число устройств на гостя НЕ
    ограничено (решение пользователя 2026-08-03: стоимость и так упирается
    в трафик, отдельный потолок на устройства — лишняя строгость).

    ``apk_repo`` — откуда брать официальный APK AmneziaWG (не полный клиент
    AmneziaVPN — тот кратно больше лимита Telegram-бота на файл).
    ``apk_cache_dir`` — кэш на jeeves; свежесть по GitHub API проверяется
    на каждый запрос (см. vpn/apk.py), а не по расписанию.

    ``ios_app_store_url``/``google_play_url``/``official_download_url`` —
    решение пользователя 2026-08-04: перед тем как слать сам файл .apk,
    сначала показать все официальные способы поставить AmneziaWG (не только
    Android/сайдлоад — на iOS сайдлоада нет вовсе). Проверены вручную
    2026-08-04: App Store id6478942365 (издатель Privacy Technologies, тот
    же, что публикует AmneziaVPN), Google Play org.amnezia.awg.

    ``amneziavpn_ios_app_store_url``/``amneziavpn_google_play_url`` —
    решение пользователя 2026-08-04: сообщение с ссылками рекомендует
    полную AmneziaVPN отдельными кликабельными ссылками рядом с AmneziaWG
    (а не одной длинной URL текстом) — у неё, в отличие от AmneziaWG, нет
    единой страницы загрузки со всеми платформами, только маркеты. Проверены
    вручную 2026-08-04: App Store id1600529900, Google Play org.amnezia.vpn.
    ``official_download_url`` остаётся общей ссылкой на страницу загрузок
    (сейчас это фактически страница AmneziaVPN).
    """

    socket: str = "./data/vpn.sock"
    db_path: Path = Path("./data/vpn.sqlite")
    interface: str = "awg0"
    subnet: str = "10.9.0.0/24"
    endpoint_host: str = ""
    endpoint_port: int = Field(default=51820, ge=1, le=65535)
    dns: str = "1.1.1.1"
    jc: int = Field(default=5, ge=1)
    jmin: int = Field(default=40, ge=0)
    jmax: int = Field(default=70, ge=0)
    s1: int = Field(default=50, ge=0)
    s2: int = Field(default=80, ge=0)
    h1: int = Field(default=5, ge=1)
    h2: int = Field(default=6, ge=1)
    h3: int = Field(default=7, ge=1)
    h4: int = Field(default=8, ge=1)
    base_quota_gb: int = Field(default=500, ge=0)
    extra_step_gb: int = Field(default=100, gt=0)
    self_ceiling_gb: int = Field(default=1000, ge=0)
    warn_remaining_gb: int = Field(default=100, ge=0)
    node_limit_gb: int = Field(default=10000, ge=0)
    sample_interval_s: float = Field(default=180.0, gt=0)
    apk_repo: str = "amnezia-vpn/amneziawg-android"
    apk_cache_dir: Path = Path("./data/vpn-apk")
    ios_app_store_url: str = "https://apps.apple.com/app/amneziawg/id6478942365"
    google_play_url: str = "https://play.google.com/store/apps/details?id=org.amnezia.awg"
    amneziavpn_ios_app_store_url: str = "https://apps.apple.com/us/app/amneziavpn/id1600529900"
    amneziavpn_google_play_url: str = (
        "https://play.google.com/store/apps/details?id=org.amnezia.vpn"
    )
    official_download_url: str = "https://amnezia.org/downloads"
    config_message_ttl_s: float = Field(default=600.0, gt=0)

    # --- Мониторинг доступности VPN (служба vpn_check, найдена нужда
    # 2026-08-17: NAT-правило на jeeves тихо пропало на 4 дня, узнали только
    # когда понадобился доступ из Казахстана). check_targets — что проверять
    # (нейтральный сайт + api.telegram.org, т.к. это то, что реально нужно
    # пользователю и что блокируется избирательно); check_nodes — с каких
    # нод (jeeves — локальный сигнал «сервер вообще жив», alfred — реальная
    # точка в Казахстане; список расширяется без правки кода). Пороги
    # гистерезиса — см. domain/vpn_check.py::reconcile_vpn_check.
    check_targets: list[str] = Field(
        default_factory=lambda: ["https://1.1.1.1", "https://api.telegram.org"]
    )
    check_nodes: list[str] = Field(default_factory=lambda: ["jeeves", "alfred"])

    # --- Прокси на jeeves (mtg — MTProto, microsocks — SOCKS5), 2026-08-17.
    # Секрет mtg НЕ здесь — он в БД (proxy_state, см. vpn/service.py), чтобы
    # rotate_secret не требовал правки TOML. Трафик обоих демонов идёт через
    # тот же канал, что и AmneziaWG — учитывается в том же node_limit_gb
    # (решение пользователя 2026-08-17: не заводить отдельный бюджет).
    mtg_domain: str = "www.microsoft.com"  # fake-TLS фронт
    mtg_port: int = Field(default=443, ge=1, le=65535)
    mtg_public_host: str = ""  # публичный IP/домен jeeves; пусто — proxy_link откажет с подсказкой
    socks_port: int = Field(default=1080, ge=1, le=65535)
    socks_host: str = ""  # tailscale-адрес jeeves; пусто — proxy_link откажет с подсказкой
    check_interval_s: float = Field(default=300.0, gt=0)
    check_fail_threshold: int = Field(default=2, ge=1)
    check_clear_threshold: int = Field(default=1, ge=1)
    check_dispatch_timeout_s: float = Field(default=5.0, gt=0)


class VpnCheckConfig(BaseModel):
    """Служба vpn_check (`sa-home-bot --service vpn_check`) — исполнитель
    пробных запросов через VPN на конкретной ноде (jeeves, alfred, ...),
    см. ``VpnConfig.check_nodes`` выше и vpn_check/service.py.

    ``netns`` — сетевой неймспейс с уже поднятым (отдельным деплой-скриптом,
    вне этого кода) клиентским туннелем AmneziaWG: изоляция нужна, чтобы
    основная маршрутизация ноды не менялась независимо от того, какие IP
    отдаёт DNS для проверяемых целей (например api.telegram.org).
    """

    socket: str = "./data/vpn_check.sock"
    netns: str = "vpn-probe"
    check_timeout_s: float = Field(default=8.0, gt=0)


class WeatherConfig(BaseModel):
    """Город дома для тула ``get_weather`` (/ai, LLM_INTEGRATION_PLAN.md
    §8.4). Не отдельная служба роя — Open-Meteo не требует ключа и не хранит
    состояния, вызов делает сам бот-процесс (см. bot/tools.py). Обычное
    название города, не координаты — широту/долготу тул сам получает через
    геокодинг-API того же провайдера (детерминированно, не полагаясь на
    "память" модели о географии) и кэширует на время жизни процесса. Пусто
    по умолчанию — тул явно ответит "не настроено"."""

    city: str = ""


class NodeConfig(BaseModel):
    """Сервис ноды (супервизор, `sa-home-bot --service node`).

    Нода запускает службы из ``assignments`` дочерними процессами, рестартит
    упавших и отдаёт статус/управление по протоколу v0 через ``socket``
    (клиент — ``nodectl``). Известные назначения: ``monitor``,
    ``telegram-bot``, ``apps``; по умолчанию пусто — назначения только
    явные (голая нода — норма). ``id`` — имя ноды в рое (dst.node в
    конверте); пусто = hostname машины. ``listen`` — адрес (или список
    адресов) для пиров: нода слушает и ``socket`` (локальные фронтенды),
    и их; пусто = нет.

    Адресов имеет смысл держать несколько (этап 24) — например,
    tailscale-адрес и LAN-адрес, или один ``tcp://0.0.0.0:8710``. Причина
    не в маршруте (между домашними нодами tailscale и так ходит по LAN
    напрямую), а в доступности: единственный tailscale-адрес появляется
    через 40–60 с после загрузки и не появляется вовсе без интернета —
    и тогда две машины в двух метрах друг от друга не видят друг друга,
    хотя LAN исправна. Расширение поверхности осознанное: TCP в любом
    случае требует ``[swarm].token`` первым сообщением.

    ``assignments`` — стартовый набор, не единственный источник: рантайм
    (``assign``/``unassign`` по протоколу — nodectl/бот) хранит фактический
    список в ``state_path`` (см. `node/state.py`), объединяемом с этим при
    старте. Снять TOML-назначение можно только правкой конфига.

    ``kind`` — тип машины (``server`` | ``workstation`` | ``vps``). Не делает
    ноду главной (рой равноправен), а отвечает на вопросы «алертить ли о её
    пропаже», «можно ли её будить по WoL», «есть ли у неё датчики железа» и
    задаёт базовый приоритет аренды синглтонов — см. `node/kind.py`.
    По умолчанию ``workstation``: консервативнее молчать, чем шуметь — прежний
    дефолт ``server`` делал из ненастроенного ноутбука машину, обязанную быть
    в сети (живой пример: бесполезные алерты про arch-t480, этап 23 п. 4).
    Серверу и VDS тип задаётся явно — как уже сделано у alfred и jeeves.

    ``power_controllable`` — явный оверрайд умения выключить/перезагрузить/
    усыпить машину кнопкой в боте (``poweroff``/``reboot``/``suspend`` в
    node/service.py). По умолчанию ``None`` — берётся из типа машины
    (``NodeTraits.power_controllable``, node/kind.py): always_on-нода
    (server/vps) кнопок не получает, потому что для неё недоступность —
    авария, а не штатное состояние. Но «недоступность — авария» и «админ не
    должен мочь сам выключить свой же сервер» — разные вещи: домашний
    сервер, в отличие от удалённого VDS, стоит физически рядом — если что,
    кнопку питания нажмут руками. Поэтому явный ``true`` в конфиге той
    машины включает кнопки, не трогая поведение алертов о пропаже (пример —
    alfred: kind=server, но он дома, не в облаке).
    """

    id: str = ""
    kind: NodeKind = "workstation"
    power_controllable: bool | None = None
    # По умолчанию выключено — явный opt-in для конкретной машины (как и
    # power_controllable), не следствие типа/умений. Работает только вместе
    # с power_controllable: когда служба llm этой же ноды засыпает САМА по
    # простою (`llm_went_idle`, не тихий ручной роспуск — см.
    # node/app.py::on_local_event), нода проверяет открытые SSH-сессии
    # (utils/ssh_sessions.py) и либо выключается (systemctl poweroff), либо,
    # если кто-то зашёл по SSH, шлёт админам событие `idle_power_blocked` с
    # кнопкой «закрыть сессии и выключить» (node/service.py, bot/node_events.py).
    # Отдельного тайм-аута нет — источник простоя один, `[llm].idle_sleep_after_s`.
    idle_poweroff: bool = False
    socket: str = "./data/node.sock"
    listen: list[str] = Field(default_factory=list)
    assignments: list[str] = Field(default_factory=list)
    state_path: str = "./data/node-state.json"
    restart_delay_s: float = Field(default=5.0, gt=0)
    stop_timeout_s: float = Field(default=90.0, gt=0)  # SIGTERM → SIGKILL

    @field_validator("listen", mode="before")
    @classmethod
    def _listen_as_list(cls, value: Any) -> Any:
        """``listen = "tcp://..."`` (одна строка, как было до этапа 24) и
        ``listen = ["tcp://...", ...]`` — обе формы валидны; пустая строка
        по-прежнему значит «TCP не слушаем»."""
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class SwarmNodeConfig(BaseModel):
    """Удалённая нода роя (discovery «на минималках» — статический список).

    ``id`` — имя ноды (hostname), как в ``dst.node`` конверта;
    ``endpoint`` — endpoint её сервиса ноды, обычно ``tcp://host:port``
    (tailscale-адрес). Запросы к чужим нодам нода пересылает сама
    (правило «спроси любого», ARCHITECTURE §11 п. 2).

    ``endpoints`` — остальные известные адреса той же ноды (LAN и т.п.),
    выученные из её hello (этап 24). В TOML заполнять не нужно: там
    достаточно одного адреса, по которому сосед найдётся впервые, — дальше
    он сам расскажет о себе всё, и список переживёт рестарт через
    состояние ноды. ``endpoint`` при этом — последний удачный путь.
    """

    id: str
    endpoint: str
    endpoints: list[str] = Field(default_factory=list)
    # Тип машины соседа, узнанный из его hello и сохранённый в состоянии ноды.
    # В TOML заполнять не нужно (и не следует — источник истины сам сосед):
    # поле нужно, чтобы знать тип ноды, которая прямо сейчас недоступна.
    kind: str = ""


class DiscoveryConfig(BaseModel):
    """Маячок в локальной сети: ноды находят друг друга сами (этап 31).

    Статический список пиров и разовый ``join`` переживают рестарт, но не
    переживают СМЕНУ адреса: если DHCP выдал соседу другой IP, пока тот был
    недоступен, чинить приходилось руками. Маячок закрывает и это, и первое
    знакомство: запрос уходит broadcast'ом, ответ приходит unicast'ом, а
    дальше работает уже существующий путь ``join`` («один seed → полный
    mesh», node/service.py).

    ``port`` — 32167: не занят IANA и лежит ниже эфемерного диапазона Linux
    (32768–60999), то есть его не займёт случайный исходящий сокет.

    Кадеданс адаптивный. ``idle_interval_s`` — фон спокойного роя, где все
    на связи и искать некого; ``active_interval_s`` действует, пока пиров нет
    вовсе или хоть один недоступен, — то есть ровно тогда, когда сосед и
    ищется. Так эфир почти всегда чист, а находится сосед за секунды, а не
    за минуты.

    Ограничение по природе broadcast'а: он живёт в локальном сегменте и через
    tailscale не проходит. Ноды вне локалки (VDS) добавляются статикой —
    см. ``SwarmConfig.nodes``/``join``.
    """

    enabled: bool = True
    port: int = Field(default=32167, ge=1, le=65535)
    idle_interval_s: float = Field(default=300.0, gt=0)
    active_interval_s: float = Field(default=15.0, gt=0)


class SwarmConfig(BaseModel):
    """Общие параметры роя.

    ``token`` — общий секрет роя: обязателен для служб на TCP-endpoint'ах
    (Windows-нода, межнодовый канал); unix-сокеты защищены правами файла
    и токен не используют. Один токен на весь рой (домашняя сеть/tailnet).

    ``nodes`` — статический список (совместимость, продолжает работать).
    ``join`` — endpoint одной уже существующей ноды роя, используется
    ТОЛЬКО при самом первом запуске новой ноды (пока персистентный список
    пиров в `node/state.py` пуст): нода спрашивает у него полный граф
    известных пиров и связывается со всеми напрямую («один seed → полный
    mesh»). При следующих рестартах не используется повторно — полагаемся
    на уже сохранённый список. Пусто = не присоединяться самостоятельно
    (только статический ``nodes``).
    """

    token: str = ""
    nodes: list[SwarmNodeConfig] = Field(default_factory=list)
    join: str = ""
    # Сколько нода, обязанная быть в сети (kind=server|vps), может быть
    # недоступна, прежде чем рой сочтёт это аварией и пришлёт node_down.
    # С запасом больше типичного рестарта ноды и перезагрузки машины, чтобы
    # плановый ребут не будил владельца ночью.
    node_down_alert_after_s: float = Field(default=300.0, gt=0)
    # Сколько резервная нода ждёт, прежде чем перенять службу-синглтон у
    # пропавшей основной. Слишком мало — служба скачет по рою на каждом
    # моргании сети; слишком много — бот дольше молчит после реальной аварии.
    failover_grace_s: float = Field(default=120.0, gt=0)
    # Сколько младший ждёт после уступки слота, прежде чем считать, что
    # вернувшаяся основная нода не смогла реально подняться (не прислала
    # report_ready), и забрать слот обратно — иначе служба может зависнуть
    # «ничьей» на неопределённое время, если основная нода упала уже ПОСЛЕ
    # того, как объявила притязание, но раньше, чем реально заработала
    # (живой инцидент 2026-08-08, см. node/lease.py::SUPERIOR_READY_TIMEOUT_S).
    superior_ready_timeout_s: float = Field(default=45.0, gt=0)
    # Маячок в локальной сети (секция [swarm.discovery]). Включён по
    # умолчанию: сосед в той же локалке должен находиться сам, без строчки
    # в конфиге. Выключается там, где broadcast бесполезен или нежелателен.
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "plain"  # plain | json


class SubscriptionConfig(BaseModel):
    name: str
    chat_id: int
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    allowed_commands: list[str] = Field(default_factory=list)
    family: bool = False  # свой человек — доступ к memory scope=family, см. MemoryConfig


class GuestSubscriptionConfig(SubscriptionConfig):
    """Подписка, выданная инвайт-кодом, — из гостевого пакета.

    Живёт не там, где владельческие: гостевой пакет
    ``instances/telegram-bot.<инстанс>.guests.toml`` ведёт сам бот и
    перезаписывает целиком, тогда как основной пакет правит человек (см.
    subscriptions/guests.py). Отдельное поле ``guest_subscriptions``, а не
    вторая пачка ``[[subscriptions]]``, потому что pydantic-settings не
    сливает списки из разных источников — гостевой затёр бы владельческий.
    """

    invited_by_chat_id: int = 0
    invited_at: str = ""  # UTC ISO, когда код погашен
    invited_user: str = ""  # как подписался гость (для /guests), не идентификатор


class InvitesConfig(BaseModel):
    """Приватный вход: одноразовые коды приглашения (AUTHORIZATION.md §10).

    ``grant_*`` — что именно получает гость при активации. По умолчанию —
    разговор с Альфредом, память о себе и веб-поиск: тулы фильтруются
    подпиской (§3.4), поэтому ни рой, ни торренты гостю не видны. События не
    шлём вовсе — алерты о температуре дисков не его дело.

    VPN (Этап 33) сюда НЕ входит по умолчанию — это отдельный ресурс с
    реальной стоимостью трафика, выдавать его каждому новому гостю молча
    неверно. Чтобы новые гости получали доступ к /vpn сразу при активации
    кода, добавьте в свой ``config.toml`` (не в этот дефолт в коде):
    ``usage@vpn, issue@vpn, reissue@vpn, revoke@vpn, grant_extra@vpn,
    request_extra@vpn, apk@vpn``. Существующим гостям права можно выдать
    точечно, поправив их подписку.
    """

    enabled: bool = True
    # Дефолт на случай /invite без аргумента часов (решение пользователя
    # 2026-08-04, второй заход: сама команда теперь принимает "/invite
    # <часы>" — раньше, когда переопределить было нечем, дефолт держали
    # длинным (24ч), с аргументом смысла в этом больше нет).
    ttl_s: float = Field(default=3600.0, gt=0)
    grant_commands: list[str] = Field(
        default_factory=lambda: [
            "chat@llm",
            "recall@memory",
            "remember@memory",
            "search@net",
        ]
    )
    grant_events: list[str] = Field(default_factory=list)
    # Потолок попыток «похожего на код» из одного неподписного чата за час:
    # молчание само по себе от онлайн-подбора не защищает.
    max_attempts_per_hour: int = Field(default=5, gt=0)


class PersonConfig(BaseModel):
    """Один известный собеседник /ai — bot/ai_flow.py сопоставляет с ним
    отправителя сообщения (по telegram_username, а для тех, у кого username
    неизвестен, — по telegram_id) и подмешивает в context_note точный
    возраст и местное время (см. bot/ai_flow.py::_find_known_person).

    Живая находка 2026-07-25: ФИО/дата рождения/город — персональные данные
    живых людей, поэтому только в config.toml (gitignored), не в примере в
    репозитории — там лишь плейсхолдеры (config.example.toml). Отдельно от
    llm-prompt.toml: там — семейное древо и правила поведения одним
    промптом персонажа (общее знание, не завязанное на "кто пишет прямо
    сейчас"), здесь — сырые факты для детерминированного вычисления
    возраста/времени в коде (те же соображения, что и с часовыми поясами:
    не поручать модели арифметику с датами, когда код может посчитать
    точно)."""

    telegram_username: str = ""  # без "@", матчится с message.from_user.username
    telegram_id: int = 0  # фоллбэк на случай, когда username неизвестен (0 = не задан)
    full_name: str
    gender: Literal["m", "f"]
    city: str = ""
    timezone: str = ""  # IANA-имя, напр. "Asia/Almaty"; пусто — время не подмешиваем
    birth_date: str = ""  # ISO "YYYY-MM-DD"; пусто — возраст неизвестен, не считаем


def _warn_on_shadowed_sections(config_path: Path, instance_path: Path) -> None:
    """Предупредить о секциях, оставшихся в общем конфиге после переезда.

    Молчать тут нельзя: владелец правит знакомый ``config.toml``, не понимает,
    почему ничего не изменилось, — а изменилось бы, правь он пакет. Пакет
    выигрывает намеренно (он же реплицируется), поэтому единственное честное
    поведение — сказать, где теперь живёт настройка.
    """
    try:
        with open(config_path, "rb") as f:
            base = tomllib.load(f)
        with open(instance_path, "rb") as f:
            package = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return
    shadowed = sorted(set(base) & set(package))
    if shadowed:
        log.warning(
            "Конфиг %s: секции %s теперь живут в пакете инстанса %s и берутся оттуда — "
            "перенесите правки туда и удалите их из общего конфига",
            config_path, ", ".join(f"[{s}]" for s in shadowed), instance_path.name,
        )


def _load_persona_prompt(path: Path, settings: Settings) -> None:
    """Подмешать settings.llm.persona_prompt из отдельного локального файла.

    Живая находка 2026-07-25: текст персонажа (тон, характер, конкретные
    реплики) убран из репозитория — слишком личный/объёмный, чтобы жить в
    основном ``config.toml`` вперемешку с обычными настройками. Живёт
    рядом, в ``llm-prompt.toml`` (тот же каталог, что и config_path),
    gitignored так же, как config.toml. Формат — один ключ верхнего уровня:
    ``persona_prompt = \"\"\"...многострочный текст...\"\"\"``. Файла может не
    быть вовсе (нода без службы llm, например alfred) — тогда молча
    пропускаем, LlmConfig.persona_prompt остаётся пустым и в дело идёт
    llm/prompt.py::DEFAULT_PERSONA_PROMPT (см. llm/service.py)."""
    if not path.exists():
        return
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    persona = raw.get("persona_prompt")
    if isinstance(persona, str) and persona.strip():
        settings.llm.persona_prompt = persona
    else:
        log.warning("%s: нет непустого ключа persona_prompt — игнорируется", path)


class Settings(BaseSettings):
    """Корневая модель настроек."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Путь к TOML, выставляется в load() до инстанцирования.
    _toml_path: ClassVar[Path | None] = None
    # Путь к пакету настроек инстанса — там же и так же.
    _instance_path: ClassVar[Path | None] = None
    # Путь к гостевому пакету (его ведёт бот) — там же и так же.
    _guests_path: ClassVar[Path | None] = None

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    torrents: TorrentsConfig = Field(default_factory=TorrentsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    net: NetConfig = Field(default_factory=NetConfig)
    vpn: VpnConfig = Field(default_factory=VpnConfig)
    vpn_check: VpnCheckConfig = Field(default_factory=VpnCheckConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    node: NodeConfig = Field(default_factory=NodeConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    invites: InvitesConfig = Field(default_factory=InvitesConfig)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list)
    guest_subscriptions: list[GuestSubscriptionConfig] = Field(default_factory=list)
    people: list[PersonConfig] = Field(default_factory=list)
    # Куда бот пишет гостевые подписки. Не настройка человека, а результат
    # load(): боту не передают ни config_path, ни имя инстанса, а путь ему
    # нужен (subscriptions/guests.py). None — конфиг не файловый или инстанс
    # не задан: гостей принимать некуда, инвайты просто не работают.
    guests_path: Path | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Приоритет: init > env > пакет инстанса > TOML. Пакет выше общего
        # конфига потому, что он и есть источник истины для своих секций:
        # оставшаяся в config.toml копия — след прошлой раскладки, она не
        # должна переигрывать то, что рой только что синхронизировал.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        # Гостевой пакет — отдельным источником: его единственное поле
        # (guest_subscriptions) ни с чем не пересекается, поэтому место в
        # порядке приоритетов роли не играет.
        if cls._guests_path is not None and cls._guests_path.exists():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=cls._guests_path))
        if cls._instance_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=cls._instance_path))
        if cls._toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=cls._toml_path))
        return tuple(sources)

    @classmethod
    def load(
        cls,
        config_path: str | Path | None,
        *,
        instance: str = "",
        instance_service: str = "telegram-bot",
    ) -> Settings:
        """Загрузить настройки из TOML (если задан) с применением env-оверрайда.

        ``instance`` — имя инстанса службы-синглтона (конкретного бота): его
        переносимые настройки лежат отдельным пакетом рядом с config.toml и
        реплицируются по рою (см. `node/instances.py`). Пусто — пакета нет,
        всё берётся из общего конфига, как раньше.

        Неизвестные поля TOML не ошибка (совместимость версий), но каждое
        уходит warning'ом в лог — опечатка не должна молчать.
        """
        if config_path is not None:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
            cls._toml_path = path
        else:
            cls._toml_path = None
        instance_path = None
        guests_path = None
        if instance and config_path is not None:
            instance_path = (
                Path(config_path).parent
                / INSTANCES_DIRNAME
                / f"{instance_service}.{instance}{PACKAGE_SUFFIX}"
            )
            if not instance_path.exists():
                raise FileNotFoundError(
                    f"Пакет настроек инстанса не найден: {instance_path}"
                )
            # Гостевого пакета может ещё не быть (никого не приглашали) —
            # это не ошибка, в отличие от отсутствия основного.
            guests_path = guests_package_path(instance_path)
        cls._instance_path = instance_path
        cls._guests_path = guests_path
        try:
            settings = cls()
        finally:
            cls._toml_path = None
            cls._instance_path = None
            cls._guests_path = None
        settings.guests_path = guests_path
        if instance_path is not None:
            _warn_on_shadowed_sections(path, instance_path)
        if config_path is not None:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            for key in unknown_config_keys(raw, cls):
                log.warning("Конфиг %s: неизвестное поле %r — опечатка? Игнорируется", path, key)
            _load_persona_prompt(path.parent / "llm-prompt.toml", settings)
        return settings
