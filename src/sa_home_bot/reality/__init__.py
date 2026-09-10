"""Хелперы транспорта VLESS+Reality (xray-core) — второй транспорт службы
``vpn`` (подэтап 39.0.x, см. ``~/.claude/plans/functional-imagining-otter.md``).

Отдельной службы ``reality`` нет: квота и учёт трафика общие с AmneziaWG
(одна квота на гостя на сервер, независимо от транспорта). Здесь только
чистые модули без состояния:

* ``client_config`` — генератор sing-box-конфига для Hiddify, ``vless://``,
  Hiddify deep-link;
* ``routing`` — правила сплит-туннеля (remote rule-set ``itdoginfo`` +
  inline always-direct для банков/госуслуг);
* ``xray`` — обёртка над бинарником ``xray`` (``xray api adu/rmu/
  inbounduser/statsquery``), которую использует ``vpn/service.py``.
"""
