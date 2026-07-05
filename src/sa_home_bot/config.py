"""Конфигурация приложения: pydantic-модели + загрузка из TOML с env-оверрайдом.

Источник правды — TOML-файл; любое значение переопределяется переменной
окружения с префиксом ``SENTINEL__`` и разделителем вложенности ``__``.
Подписки задаются только в TOML (env-оверрайд списков не поддерживается
сознательно — см. ARCHITECTURE.md §4.5).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


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


class SensorsConfig(BaseModel):
    cpu: CpuSensorConfig = Field(default_factory=CpuSensorConfig)
    disks: DiskSensorConfig = Field(default_factory=DiskSensorConfig)


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
        """Загрузить настройки из TOML (если задан) с применением env-оверрайда."""
        if config_path is not None:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"Файл конфигурации не найден: {path}")
            cls._toml_path = path
        else:
            cls._toml_path = None
        try:
            return cls()
        finally:
            cls._toml_path = None
