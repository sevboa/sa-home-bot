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

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

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


class LhmSensorConfig(BaseModel):
    """LibreHardwareMonitor — источник температур на Windows (`sensors/lhm.py`).

    ``dll_path`` — путь к LibreHardwareMonitorLib.dll; пусто — поиск по
    типовым местам (%LOCALAPPDATA%\\sa-home-bot, Program Files). На Linux
    секция игнорируется.
    """

    dll_path: str = ""


class SensorsConfig(BaseModel):
    cpu: CpuSensorConfig = Field(default_factory=CpuSensorConfig)
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


class LlmConfig(BaseModel):
    """Служба llm (Альфред, `sa-home-bot --service llm`) — только на winpc.

    ``ollama_url`` — loopback-адрес Ollama на этой же машине (см. §0
    LLM_INTEGRATION_PLAN.md: наружу это никогда не смотрит, только служба →
    Ollama локально). ``wsl_distro``/``ollama_container`` — имена,
    зафиксированные при ручной настройке инфраструктуры (см. документ выше,
    §1). ``request_timeout_s`` — таймаут ответа `ask`/`chat` по протоколу
    роя (генерация, в т.ч. с холодным стартом WSL/контейнера, дольше
    типичных «быстрых» действий — см. Envelope.timeout_s в proto/messages.py).
    ``idle_sleep_after_s`` — после стольки секунд без запросов служба сама
    останавливает контейнер (освобождает VRAM); бот независимо закрывает
    диалог тем же порогом (bot/ai_idle.py) — таймеры не координируются
    протоколом, только общим значением конфига.
    """

    socket: str = "./data/llm.sock"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b"
    wsl_distro: str = "Docker"
    ollama_container: str = "ollama"
    request_timeout_s: float = Field(default=180.0, gt=0)
    idle_sleep_after_s: float = Field(default=1800.0, gt=0)


class NodeConfig(BaseModel):
    """Сервис ноды (супервизор, `sa-home-bot --service node`).

    Нода запускает службы из ``assignments`` дочерними процессами, рестартит
    упавших и отдаёт статус/управление по протоколу v0 через ``socket``
    (клиент — ``nodectl``). Известные назначения: ``monitor``,
    ``telegram-bot``, ``apps``; по умолчанию пусто — назначения только
    явные (голая нода — норма). ``id`` — имя ноды в рое (dst.node в
    конверте); пусто = hostname машины. ``listen`` — дополнительный
    endpoint для пиров (обычно ``tcp://<tailscale-ip>:8710``): нода
    слушает и ``socket`` (локальные фронтенды), и его; пусто = нет.

    ``assignments`` — стартовый набор, не единственный источник: рантайм
    (``assign``/``unassign`` по протоколу — nodectl/бот) хранит фактический
    список в ``state_path`` (см. `node/state.py`), объединяемом с этим при
    старте. Снять TOML-назначение можно только правкой конфига.
    """

    id: str = ""
    socket: str = "./data/node.sock"
    listen: str = ""
    assignments: list[str] = Field(default_factory=list)
    state_path: str = "./data/node-state.json"
    restart_delay_s: float = Field(default=5.0, gt=0)
    stop_timeout_s: float = Field(default=90.0, gt=0)  # SIGTERM → SIGKILL


class SwarmNodeConfig(BaseModel):
    """Удалённая нода роя (discovery «на минималках» — статический список).

    ``id`` — имя ноды (hostname), как в ``dst.node`` конверта;
    ``endpoint`` — endpoint её сервиса ноды, обычно ``tcp://host:port``
    (tailscale-адрес). Запросы к чужим нодам нода пересылает сама
    (правило «спроси любого», ARCHITECTURE §11 п. 2).
    """

    id: str
    endpoint: str


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


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "plain"  # plain | json


class SubscriptionConfig(BaseModel):
    name: str
    chat_id: int
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    allowed_commands: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    """Корневая модель настроек."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Путь к TOML, выставляется в load() до инстанцирования.
    _toml_path: ClassVar[Path | None] = None

    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    apps: AppsConfig = Field(default_factory=AppsConfig)
    torrents: TorrentsConfig = Field(default_factory=TorrentsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    node: NodeConfig = Field(default_factory=NodeConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Приоритет: init > env > TOML. То есть env переопределяет TOML.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if cls._toml_path is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=cls._toml_path))
        return tuple(sources)

    @classmethod
    def load(cls, config_path: str | Path | None) -> Settings:
        """Загрузить настройки из TOML (если задан) с применением env-оверрайда.

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
        try:
            settings = cls()
        finally:
            cls._toml_path = None
        if config_path is not None:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            for key in unknown_config_keys(raw, cls):
                log.warning("Конфиг %s: неизвестное поле %r — опечатка? Игнорируется", path, key)
        return settings
