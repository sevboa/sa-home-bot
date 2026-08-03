"""Единый реестр служб роя — одно место, где служба описана целиком.

До этого модуля одна и та же служба была описана в трёх независимых местах:
``ASSIGNMENT_ARGS`` (чем её спавнить), ``--service`` choices в ``cli.py`` (как
её запустить руками) и ``local_service_endpoints()`` (куда к ней стучаться).
Добавление любого свойства службы — синглтон ли она, нужны ли ей датчики
железа, есть ли у неё переносимый пакет настроек — множило эти места дальше.
Здесь всё это — поля одного ``ServiceSpec``.

Реестр описывает **службы**, которые нода может нести. Сама нода
(``--service node``) службой роя не является — она хост для служб, поэтому в
``SERVICES`` её нет, а в CLI-choices она добавляется отдельно
(``NODE_CLI_NAME``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from sa_home_bot.config import Settings


@dataclass(frozen=True)
class ServiceSpec:
    """Описание одной службы роя.

    ``name`` — имя в назначениях ноды и в ``dst.service`` конверта
    (``telegram-bot``); ``cli_name`` — значение ``--service`` (``bot``): они
    исторически различаются, и реестр — единственное место, где это знание
    хранится.

    ``endpoint_attr`` — путь к полю сокета в ``Settings`` через точку
    (``monitor.socket``); ``None`` — своего proto-сервера у службы нет
    (telegram-bot только клиент), маршрутизировать к ней нечего.

    ``assignable`` — предлагать ли службу в ``assign`` (кнопки бота,
    ``nodectl assign``). ``externally_managed`` — процесс поднимает не
    супервизор (см. ``llm`` ниже).

    ``singleton`` — на весь рой активен только один экземпляр (на инстанс,
    если ``instanced``); такими управляет аренда лидерства.
    ``instanced`` — у службы есть переносимый пакет настроек, и её экземпляры
    адресуются парой ``service@instance``.

    ``needs_hardware_sensors`` — службе нужны термозоны/SMART реальной
    машины; на ноде без них (vps) она бессмысленна и не предлагается.
    """

    name: str
    cli_name: str
    endpoint_attr: str | None = None
    assignable: bool = True
    externally_managed: bool = False
    singleton: bool = False
    instanced: bool = False
    needs_hardware_sensors: bool = False

    @property
    def cli_args(self) -> list[str]:
        """Аргументы запуска этой службы этим же пакетом."""
        return ["--service", self.cli_name]


# Порядок важен дважды: он задаёт порядок кнопок «назначить службу» в боте
# (describe → choices) и порядок остановки служб (обратный) в супервизоре.
_SPECS: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        name="monitor",
        cli_name="monitor",
        endpoint_attr="monitor.socket",
        needs_hardware_sensors=True,
    ),
    ServiceSpec(
        name="telegram-bot",
        cli_name="bot",
        endpoint_attr=None,  # бот — только клиент, своего сервера нет
        singleton=True,  # у токена может быть лишь один активный поллер
        instanced=True,  # свой пакет настроек на каждого бота
    ),
    ServiceSpec(name="apps", cli_name="apps", endpoint_attr="apps.socket"),
    ServiceSpec(name="torrents", cli_name="torrents", endpoint_attr="torrents.socket"),
    # tasks по смыслу тоже синглтон (своя БД напоминаний), но резерва для неё
    # пока не делаем: БД не реплицируется, и включать аренду без этого рано.
    ServiceSpec(name="tasks", cli_name="tasks", endpoint_attr="tasks.socket"),
    # net — обёртка над локальным SearXNG (веб-поиск для /ai). Обычная
    # служба: где стоит SearXNG, там её и назначают (сейчас alfred, см.
    # net/protocol.py::NODE_ID).
    ServiceSpec(name="net", cli_name="net", endpoint_attr="net.socket"),
    # memory — долгая память Альфреда о чате. Как и net, живёт там, где нода
    # круглосуточная (см. memory/protocol.py::NODE_ID): вспоминать должно
    # получаться всегда, а не только когда проснулась машина с моделью.
    ServiceSpec(name="memory", cli_name="memory", endpoint_attr="memory.socket"),
    # Живая находка 2026-07-23: служба llm на Windows-ноде дёргает wsl.exe, а
    # тот из-под Session-0 (Windows-служба sa-home-node, LocalSystem) вообще
    # не запускается (exit code -1) — WSL2 требует интерактивную сессию.
    # Поэтому llm супервизором НЕ спавнится: на нодах, где она нужна, процесс
    # поднимает задача планировщика от интерактивного пользователя
    # (deploy/llm-runner.ps1). При этом llm остаётся в [node].assignments —
    # супервизия и маршрутизация независимы: NodeRouter строит локальный
    # маршрут к settings.llm.socket по тому же списку назначений, так что
    # запрос доедет до внешне управляемого процесса на том же сокете.
    ServiceSpec(
        name="llm",
        cli_name="llm",
        endpoint_attr="llm.socket",
        assignable=False,
        externally_managed=True,
    ),
    # vpn — AmneziaWG-доступ на jeeves (Этап 33 IMPLEMENTATION_PLAN.md):
    # единственная нода с белым IP, только там служба имеет смысл.
    ServiceSpec(name="vpn", cli_name="vpn", endpoint_attr="vpn.socket"),
)

SERVICES: dict[str, ServiceSpec] = {s.name: s for s in _SPECS}

# Сервис ноды — не служба роя, а хост для служб: назначить его нельзя,
# но запустить через CLI нужно.
NODE_CLI_NAME = "node"


def spec(name: str) -> ServiceSpec | None:
    """Описание службы по имени назначения; None — имя неизвестно."""
    return SERVICES.get(name)


def known_names() -> tuple[str, ...]:
    """Все известные имена служб — для сообщений об опечатках в назначениях."""
    return tuple(SERVICES)


def assignable_names() -> tuple[str, ...]:
    """Службы, которые можно назначить ноде (choices действия ``assign``)."""
    return tuple(s.name for s in _SPECS if s.assignable)


def supervised_names() -> tuple[str, ...]:
    """Службы, которые спавнит супервизор (не внешне управляемые)."""
    return tuple(s.name for s in _SPECS if not s.externally_managed)


def singleton_names() -> tuple[str, ...]:
    """Службы-синглтоны — ими управляет аренда лидерства."""
    return tuple(s.name for s in _SPECS if s.singleton)


def cli_service_choices() -> tuple[str, ...]:
    """Значения ``--service``: все службы + сам сервис ноды."""
    return tuple(s.cli_name for s in _SPECS) + (NODE_CLI_NAME,)


def _resolve(settings: Settings, dotted: str) -> str:
    obj: object = settings
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return str(obj)


def service_endpoints(settings: Settings) -> dict[str, str]:
    """Локальные службы со своим proto-сервером: имя → endpoint из конфига.

    Именно к ним нода умеет проксировать запросы (telegram-bot сюда не
    попадает — он клиент, а не сервер).
    """
    return {s.name: _resolve(settings, s.endpoint_attr) for s in _SPECS if s.endpoint_attr}
