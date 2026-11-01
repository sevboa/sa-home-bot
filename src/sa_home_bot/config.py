from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator
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
from sa_home_bot.reality.client_config import DEFAULT_FLOW

log = logging.getLogger(__name__)


def unknown_config_keys(
    data: dict[str, Any], model: type[BaseModel], prefix: str = ""
) -> list[str]:
    unknown: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None and model is DevlabModelsConfig and isinstance(value, dict):
            unknown += unknown_config_keys(value, DevlabModelConfig, f"{prefix}{key}.")
            continue
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
    proxy: str = ""
    proxy_mode: Literal["fixed", "auto"] = "fixed"
    extra_proxies: list[str] = Field(default_factory=list)
    prefer_direct: bool = True


class DatabaseConfig(BaseModel):
    path: Path = Path("./data/sentinel.sqlite")


class ScheduleConfig(BaseModel):
    scan_cron: str = "*/1 * * * *"
    smart_cron: str = "0 * * * *"
    housekeeping_cron: str = "0 3 * * *"


class _BaselineParams(BaseModel):
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
    enabled: bool = False
    warn_c: float = 80.0
    crit_c: float = 90.0
    hysteresis_delta_c: float = 5.0
    consecutive_to_alert: int = Field(default=3, ge=1)
    consecutive_to_clear: int = Field(default=3, ge=1)


class LhmSensorConfig(BaseModel):
    dll_path: str = ""


class HostMetricThreshold(BaseModel):
    warn: float
    crit: float


class HostSensorConfig(BaseModel):
    enabled: bool | None = None
    sample_window_s: float = Field(default=10.0, gt=0)
    consecutive_to_alert: int = Field(default=3, ge=1)
    consecutive_to_clear: int = Field(default=3, ge=1)
    thresholds: dict[str, HostMetricThreshold] = Field(default_factory=dict)


class SensorsConfig(BaseModel):
    cpu: CpuSensorConfig = Field(default_factory=CpuSensorConfig)
    gpu: GpuSensorConfig = Field(default_factory=GpuSensorConfig)
    disks: DiskSensorConfig = Field(default_factory=DiskSensorConfig)
    host: HostSensorConfig = Field(default_factory=HostSensorConfig)
    lhm: LhmSensorConfig = Field(default_factory=LhmSensorConfig)


class WakeConfig(BaseModel):
    mac: str = ""
    ip: str = ""
    broadcast: str = "255.255.255.255"
    port: int = Field(default=9, ge=1, le=65535)
    wait_timeout_s: float = Field(default=120.0, gt=0)


CPUSET_PATTERN = r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$"


class DevlabConfig(BaseModel):
    socket: str = "./data/devlab.sock"
    root: str = "/models/sa-home/devlab"
    user: str = "devlab"
    hub_url: str = ""
    hub_token: str = ""
    work_dir: str = ""
    poll_interval_s: float = Field(default=5.0, gt=0)
    heartbeat_interval_s: float = Field(default=30.0, gt=0)
    owner_wait_s: float = Field(default=3600.0, ge=0)
    freeze_max_s: float = Field(default=300.0, gt=0)
    neighbor_socket: str = ""
    test_image: str = "docker.io/library/python:3.13-slim"
    test_cpuset: str = Field(default="6-11,18-23", pattern=CPUSET_PATTERN)
    test_cpus: float = Field(default=12.0, gt=0)
    test_memory: str = Field(default="24g", pattern=r"^[0-9]+[kmg]$")
    agent_cpuset: str = Field(default="6-11", pattern=CPUSET_PATTERN)
    agent_cpus: float = Field(default=6.0, gt=0)
    agent_memory: str = Field(default="12g", pattern=r"^[0-9]+[kmg]$")
    build_timeout_s: float = Field(default=1800.0, gt=0)
    agent_max_steps: int = Field(default=60, ge=1)
    agent_max_wall_s: float = Field(default=8 * 3600.0, gt=0)
    agent_max_red_runs: int = Field(default=5, ge=1)
    agent_max_cost_usd: float = Field(default=2.0, ge=0)
    agent_max_tokens: int = Field(default=0, ge=0)
    agent_context_tokens: int = Field(default=16000, ge=1000)
    agent_reply_tokens: int = Field(default=2000, ge=100)


class MonitorConfig(BaseModel):
    socket: str = "./data/monitor.sock"
    db_path: Path = Path("./data/monitor.sqlite")


class AppConfig(BaseModel):
    id: str
    title: str
    unit: str
    urls: list[str] = Field(default_factory=list)


class AppsConfig(BaseModel):
    socket: str = "./data/apps.sock"
    items: list[AppConfig] = Field(default_factory=list)


class TorrentsConfig(BaseModel):
    socket: str = "./data/torrents.sock"
    qbittorrent_url: str = "http://127.0.0.1:8080"
    qbittorrent_user: str = ""
    qbittorrent_password: str = ""
    save_dirs: list[str] = Field(default_factory=list)
    search_engine_dir: str = ""


class MemoryConfig(BaseModel):
    socket: str = "./data/memory.sock"
    db_path: Path = Path("./data/memory.sqlite")
    family_chat_ids: list[int] = Field(default_factory=list)


class GraphMemoryConfig(BaseModel):
    socket: str = "./data/graph_memory.sock"
    db_path: Path = Path("./data/graph_memory.sqlite")
    uri: str = "bolt://127.0.0.1:7687"
    user: str = "neo4j"
    password: str = ""
    extraction_model: str = ""
    embedding_model: str = "nomic-embed-text"
    ingest_timeout_s: float = 120.0
    search_timeout_s: float = 15.0


class DevlabHubConfig(BaseModel):
    socket: str = "./data/devlab_hub.sock"
    port: int = Field(default=8720, ge=1, le=65535)
    data_dir: Path = Path("/mnt/data/devlab")
    bind_host: str = ""
    mailbox_retention_days: int = Field(default=30, ge=1)
    executor_node: str = "mycraft"
    hub_node: str = "alfred"


OPTIONAL_CPUSET_PATTERN = r"^(|" + CPUSET_PATTERN[1:-1] + ")$"

LLAMACPP_DEFAULTS: dict[str, Any] = {
    "free": True,
    "remote": False,
    "threads": 8,
    "threads_night": 12,
    "cpus": "0-5,12-17",
}


class DevlabModelConfig(BaseModel):
    kind: Literal["openai", "llamacpp"] = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    proxy: str = ""
    free: bool = False
    price_in_per_m: float = Field(default=0.0, ge=0)
    price_out_per_m: float = Field(default=0.0, ge=0)
    rpm: int = Field(default=0, ge=0)
    rpd: int = Field(default=0, ge=0)
    context: int = Field(default=0, ge=0)
    remote: bool = True
    report_cost: bool = False
    gguf: str = ""
    threads: int = Field(default=0, ge=0)
    threads_night: int = Field(default=0, ge=0)
    cpus: str = Field(default="", pattern=OPTIONAL_CPUSET_PATTERN)
    cpus_night: str = Field(default="", pattern=OPTIONAL_CPUSET_PATTERN)
    idle_unload_s: int = Field(default=600, ge=0)
    extra_args: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _local_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("kind") == "llamacpp":
            return {**LLAMACPP_DEFAULTS, **data}
        return data

    @model_validator(mode="after")
    def _llamacpp_needs_gguf(self) -> DevlabModelConfig:
        if self.kind == "llamacpp" and not self.gguf:
            raise ValueError("kind=llamacpp требует gguf")
        return self


class DevlabModelsConfig(BaseModel):
    hub_url: str = ""
    hub_token: str = ""
    port: int = Field(default=8721, ge=1, le=65535)
    bind_host: str = ""
    data_dir: Path = Path("./data/devlab_models")
    llamacpp_bin: str = "llama-server"
    llamacpp_port_min: int = Field(default=8730, ge=1, le=65535)
    llamacpp_port_max: int = Field(default=8749, ge=1, le=65535)
    freeze_max_s: float = Field(default=300.0, gt=0)
    neighbor_socket: str = ""
    entries: dict[str, DevlabModelConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _collect_entries(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        own = {k: v for k, v in data.items() if k in cls.model_fields}
        own["entries"] = {
            k: v for k, v in data.items() if k not in cls.model_fields and isinstance(v, dict)
        }
        return own


class TasksConfig(BaseModel):
    socket: str = "./data/tasks.sock"
    db_path: Path = Path("./data/tasks.sqlite")


class NetConfig(BaseModel):
    socket: str = "./data/net.sock"
    searxng_url: str = "http://127.0.0.1:8888"
    request_timeout_s: float = Field(default=20.0, gt=0)
    probe_timeout_s: float = Field(default=5.0, gt=0)
    max_results: int = Field(default=5, ge=1, le=20)
    language: str = "ru"


class LlmConfig(BaseModel):
    socket: str = "./data/llm.sock"
    container_backend: str = "wsl-docker"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:14b"
    wsl_distro: str = "Docker"
    ollama_container: str = "ollama"
    request_timeout_s: float = Field(default=240.0, gt=0)
    idle_sleep_after_s: float = Field(default=1800.0, gt=0)
    warmup_timeout_s: float = Field(default=360.0, gt=0)
    think_chat: bool = True
    num_ctx: int = Field(default=8192, gt=0)
    context_compression: bool = True
    context_compress_ratio: float = Field(default=0.70, gt=0.2, lt=0.95)
    context_base_tokens: int = Field(default=12000, ge=0)
    context_chars_per_token: float = Field(default=3.2, gt=0.5)
    context_keep_recent_turns: int = Field(default=10, ge=2)
    context_min_compress_turns: int = Field(default=4, ge=1)
    context_summary_max_chars: int = Field(default=4500, ge=500)
    context_summary_chunk_chars: int = Field(default=36000, ge=2000)
    context_recall_budget_chars: int = Field(default=8000, ge=0)
    context_old_reply_max_chars: int = Field(default=600, ge=0)
    context_compress_wait_s: float = Field(default=120.0, ge=0)
    model_profiles_path: Path | None = None
    mode: Literal["router_think", "single_call"] = "router_think"
    single_call_think: bool | None = None
    think_style: Literal["flag", "implicit"] = "flag"
    response_mode: Literal["rich", "typing_plain"] = "rich"
    rich_draft_streaming: bool = True
    rich_draft_min_interval_s: float = Field(default=6.0, ge=0)
    rich_draft_min_growth_chars: int = Field(default=120, ge=0)
    rich_draft_keepalive_idle_s: float = Field(default=18.0, gt=0)
    persona_prompt: str = ""
    speech_therapy_state_path: str = "./data/speech-therapy-state.json"
    speech_therapy_pinned_chat_ids: list[int] = Field(default_factory=list)
    interactives_canary_user_ids: list[int] = Field(default_factory=list)
    interactives_return_idle_h: float = Field(default=2.0, gt=0)
    interactives_spontaneous_chance: float = Field(default=1 / 8, ge=0, le=1)
    photos_dir: Path = Path("./data/ai-photos")
    photo_max_edge_px: int = Field(default=896, gt=0)
    photo_jpeg_quality: int = Field(default=80, ge=1, le=100)
    photo_ttl_days: int = Field(default=30, gt=0)
    stt_model_size: str = "medium"
    stt_compute_type: str = "int8"
    stt_cpu_threads: int = Field(default=8, gt=0)
    stt_language: str = "ru"
    stt_max_duration_s: float = Field(default=600.0, gt=0)
    stt_request_timeout_s: float = Field(default=300.0, gt=0)
    stt_tmp_dir: Path = Path("./data/stt-tmp")
    stt_model_dir: Path = Path("./data/stt-models")
    tts_language: str = "ru"
    tts_reference_voice_path: Path = Path("./data/tts-voice-ref/alfred.wav")
    tts_max_text_chars: int = Field(default=2000, gt=0)
    tts_request_timeout_s: float = Field(default=240.0, gt=0)
    tts_tmp_dir: Path = Path("./data/tts-tmp")
    tts_model_dir: Path = Path("./data/tts-models")
    tts_opus_bitrate: str = "32k"
    imagegen_enabled: bool = False
    imagegen_model: str = "Lykon/dreamshaper-8"
    imagegen_variant: str = "fp16"
    imagegen_lcm_lora: str = "latent-consistency/lcm-lora-sdv1-5"
    imagegen_prompt_template: str = "{prompt}"
    imagegen_prompt_agent: bool = True
    imagegen_style_negative: str = ""
    imagegen_steps: int = Field(default=4, ge=1, le=12)
    imagegen_guidance: float = Field(default=1.0, ge=1.0, le=3.0)
    imagegen_size: int = Field(default=128, ge=16, le=512)
    imagegen_colors: int = Field(default=0, ge=0, le=256)
    imagegen_threads: int = Field(default=10, gt=0)
    imagegen_negative: str = "blurry, lowres, watermark, text, signature, deformed"
    imagegen_request_timeout_s: float = Field(default=180.0, gt=0)
    imagegen_model_dir: Path = Path("./data/imagegen-models")
    imagegen_civitai_token: str = ""
    imagegen_display_px: int = Field(default=512, ge=64, le=2048)
    imagegen_fast_decode: bool = False
    imagegen_daily_limit: int = Field(default=0, ge=0)
    items_dir: Path = Path("./data/items")
    item_harmonize_strength: float = Field(default=0.35, gt=0, le=1.0)
    item_mood_lora_weight: float = Field(default=0.4, ge=0, le=2.0)
    item_preview_check: bool = False


def reminder_reason(llm: LlmConfig) -> str:
    if llm.mode == "single_call" and llm.single_call_think is False:
        return "off"
    return "high" if llm.think_chat else "off"


class RealityTransportConfig(BaseModel):
    endpoint_host: str = ""
    port: int = Field(default=8443, ge=1, le=65535)
    server_public_key: str = ""
    short_id: str = ""
    sni: str = "www.google.com"
    flow: str = DEFAULT_FLOW
    api_addr: str = "127.0.0.1:10085"
    inbound_tag: str = "reality-in"


class VpnConfig(BaseModel):
    socket: str = "./data/vpn.sock"
    db_path: Path = Path("./data/vpn.sqlite")
    interface: str = "awg0"
    subnet: str = "10.9.0.0/24"
    endpoint_host: str = ""
    endpoint_port: int = Field(default=51820, ge=1, le=65535)
    dns: str = "1.1.1.1"
    mtu: int = Field(default=1280, ge=1280, le=1420)
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
    location: str = ""
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

    hiddify_site_url: str = "https://hiddify.com"
    hiddify_google_play_url: str = "https://play.google.com/store/apps/details?id=app.hiddify.com"
    hiddify_ios_app_store_url: str = "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532"
    hiddify_releases_url: str = "https://github.com/hiddify/hiddify-app/releases/latest"
    wizard_settings_file: bool = True

    check_targets: list[str] = Field(
        default_factory=lambda: [
            "https://1.1.1.1",
            "https://api.telegram.org",
            "https://www.google.com",
            "https://www.youtube.com",
            "https://www.instagram.com",
            "https://discord.com",
        ]
    )
    check_nodes: list[str] = Field(default_factory=lambda: ["jeeves", "alfred"])

    mtg_domain: str = "www.microsoft.com"
    mtg_port: int = Field(default=443, ge=1, le=65535)
    mtg_public_host: str = ""
    socks_port: int = Field(default=1080, ge=1, le=65535)
    socks_host: str = ""
    check_interval_s: float = Field(default=300.0, gt=0)
    check_fail_threshold: int = Field(default=2, ge=1)
    check_clear_threshold: int = Field(default=1, ge=1)
    check_dispatch_timeout_s: float = Field(default=5.0, gt=0)

    transports: list[str] = Field(default_factory=lambda: ["awg"])
    reality: RealityTransportConfig | None = None

    sub_port: int = Field(default=8444, ge=0, le=65535)
    sub_bind: str = "0.0.0.0"
    sub_public_host: str = ""
    sub_tls_cert: str = ""
    sub_tls_key: str = ""
    sub_allow_http: bool = True
    sub_secret: str = ""
    sub_update_interval_h: int = Field(default=12, ge=1)
    sub_support_url: str = ""


class VpnCheckConfig(BaseModel):
    socket: str = "./data/vpn_check.sock"
    ip_echo_url: str = "https://api.ipify.org"
    check_timeout_s: float = Field(default=8.0, gt=0)


class WeatherConfig(BaseModel):
    city: str = ""


class NodeConfig(BaseModel):
    id: str = ""
    kind: NodeKind = "workstation"
    power_controllable: bool | None = None
    idle_poweroff: bool = False
    socket: str = "./data/node.sock"
    listen: list[str] = Field(default_factory=list)
    assignments: list[str] = Field(default_factory=list)
    state_path: str = "./data/node-state.json"
    restart_delay_s: float = Field(default=5.0, gt=0)
    stop_timeout_s: float = Field(default=90.0, gt=0)

    @field_validator("listen", mode="before")
    @classmethod
    def _listen_as_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value


class SwarmNodeConfig(BaseModel):
    id: str
    endpoint: str
    endpoints: list[str] = Field(default_factory=list)
    kind: str = ""


class DiscoveryConfig(BaseModel):
    enabled: bool = True
    port: int = Field(default=32167, ge=1, le=65535)
    idle_interval_s: float = Field(default=300.0, gt=0)
    active_interval_s: float = Field(default=15.0, gt=0)


class SwarmConfig(BaseModel):
    token: str = ""
    nodes: list[SwarmNodeConfig] = Field(default_factory=list)
    join: str = ""
    node_down_alert_after_s: float = Field(default=300.0, gt=0)
    failover_grace_s: float = Field(default=120.0, gt=0)
    superior_ready_timeout_s: float = Field(default=45.0, gt=0)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)


class BackupConfig(BaseModel):
    recipient_public_key: str = ""
    private_key_file: str = ""
    partner: str = ""
    xray_config: str = "~/.config/xray/config.json"
    snapshot_interval_s: float = 600.0
    snapshot_debounce_s: float = 15.0
    snapshot_poll_s: float = 300.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "plain"


class SubscriptionConfig(BaseModel):
    name: str
    chat_id: int
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    allowed_commands: list[str] = Field(default_factory=list)


class GuestSubscriptionConfig(SubscriptionConfig):
    invited_by_chat_id: int = 0
    invited_at: str = ""
    invited_user: str = ""


class InvitesConfig(BaseModel):
    enabled: bool = True
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
    max_attempts_per_hour: int = Field(default=5, gt=0)


class PersonConfig(BaseModel):
    telegram_username: str = ""
    telegram_id: int = 0
    full_name: str
    gender: Literal["m", "f"]
    city: str = ""
    timezone: str = ""
    birth_date: str = ""


def _warn_on_shadowed_sections(config_path: Path, instance_path: Path) -> None:
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
            config_path,
            ", ".join(f"[{s}]" for s in shadowed),
            instance_path.name,
        )


def _load_persona_prompt(path: Path, settings: Settings) -> None:
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
    model_config = SettingsConfigDict(
        env_prefix="SENTINEL__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    _toml_path: ClassVar[Path | None] = None
    _instance_path: ClassVar[Path | None] = None
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
    graph_memory: GraphMemoryConfig = Field(default_factory=GraphMemoryConfig)
    devlab_hub: DevlabHubConfig = Field(default_factory=DevlabHubConfig)
    devlab_models: DevlabModelsConfig = Field(default_factory=DevlabModelsConfig)
    net: NetConfig = Field(default_factory=NetConfig)
    vpn: VpnConfig = Field(default_factory=VpnConfig)
    vpn_check: VpnCheckConfig = Field(default_factory=VpnCheckConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    node: NodeConfig = Field(default_factory=NodeConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    wake: WakeConfig = Field(default_factory=WakeConfig)
    devlab: DevlabConfig = Field(default_factory=DevlabConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    invites: InvitesConfig = Field(default_factory=InvitesConfig)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list)
    guest_subscriptions: list[GuestSubscriptionConfig] = Field(default_factory=list)
    people: list[PersonConfig] = Field(default_factory=list)
    guests_path: Path | None = None

    @model_validator(mode="after")
    def _derive_sensors_from_node_kind(self) -> Settings:
        is_vps = self.node.kind == "vps"
        if self.sensors.host.enabled is None:
            self.sensors.host.enabled = is_vps
        if is_vps:
            for sensor in (self.sensors.cpu, self.sensors.disks):
                if "enabled" not in sensor.model_fields_set:
                    sensor.enabled = False
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
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
                raise FileNotFoundError(f"Пакет настроек инстанса не найден: {instance_path}")
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
            settings.llm.model_profiles_path = path.parent / "model-profiles.toml"
        return settings
