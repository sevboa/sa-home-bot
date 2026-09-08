"""Маршрутизация сплит-туннеля для клиентского конфига Hiddify / sing-box.

Ноль импортов пакета — только константы, как ``vpn/protocol.py``. Используется
``reality/client_config.py`` (Фаза A) и службой ``reality`` (Фаза B).

Модель: **сплит по чёрному списку**. Заблокированное в РФ → в туннель на
wooster; РФ-геолокация и всё «жёстко напрямую» → мимо туннеля. Списки
блокировок клиент тянет сам автообновляемыми remote rule-set с GitHub
(``itdoginfo/allow-domains``), поэтому свежесть не зависит от нас.
"""

from __future__ import annotations

# Автообновляемые бинарные rule-set sing-box (.srs). Требуют sing-box >= 1.11
# в клиенте (Hiddify свежих версий несёт). Релиз ``latest`` обновляется
# ежедневно самим itdoginfo.
#
#   russia_inside  — «ресурсы, которые блокируются, в т.ч. зарубежные, которые
#                    сами блокируют российские подсети» (YouTube, Instagram/Meta,
#                    X, Discord, TikTok, HDRezka, новости…) → В ТУННЕЛЬ.
#   russia_outside — «российские ресурсы, доступные только для российских
#                    подсетей» (для тех, кто за границей) → НАПРЯМУЮ (для гостя
#                    в РФ и так работает, гонять через США незачем).
RULESET_URLS: dict[str, str] = {
    "ru-blocked": (
        "https://github.com/itdoginfo/allow-domains/releases/latest/download/russia_inside.srs"
    ),
    "ru-inside": (
        "https://github.com/itdoginfo/allow-domains/releases/latest/download/russia_outside.srs"
    ),
}

RULESET_UPDATE_INTERVAL = "24h"

# --- always-direct: банки, госуслуги, платёжная инфраструктура ---
#
# Высший приоритет в ``route.rules`` — идёт РАНЬШЕ блок-листа. Страховка: если
# банковский домен случайно попадёт в ``russia_inside`` (или гость включит
# режим «всё через туннель»), он всё равно уходит напрямую. Иначе — заход в
# банк с зарубежного IP → срабатывает анти-фрод, блокировка операции/входа.
#
# Ведём вручную, меняется редко. Только регистрируемые домены (без поддоменов —
# ``domain_suffix`` в sing-box матчит и сам домен, и любые поддомены).
DIRECT_SUFFIXES: tuple[str, ...] = (
    # госуслуги / госорганы
    "gosuslugi.ru",
    "gu-st.ru",
    "gov.ru",
    "government.ru",
    "kremlin.ru",
    "mos.ru",
    "mosreg.ru",
    "nalog.ru",
    "nalog.gov.ru",
    "pfr.gov.ru",
    "sfr.gov.ru",
    "fss.ru",
    "roskomnadzor.ru",
    "rkn.gov.ru",
    "mvd.ru",
    "gibdd.ru",
    "fssp.gov.ru",
    "sudrf.ru",
    "cbr.ru",
    # НСПК / СБП / платёжная инфраструктура
    "nspk.ru",
    "mironline.ru",
    "privetmir.ru",
    "mir-connect.ru",
    # банки
    "sberbank.ru",
    "sber.ru",
    "sberbank.com",
    "sbrf.ru",
    "vtb.ru",
    "vtb24.ru",
    "bankvtb.ru",
    "alfabank.ru",
    "alfabank.com",
    "alfacapital.ru",
    "tinkoff.ru",
    "tbank.ru",
    "tcsbank.ru",
    "cdn-tinkoff.ru",
    "gazprombank.ru",
    "gpb.ru",
    "psbank.ru",
    "raiffeisen.ru",
    "raif.ru",
    "rshb.ru",
    "open.ru",
    "otkritie.ru",
    "sovcombank.ru",
    "halvacard.ru",
    "rosbank.ru",
    "mkb.ru",
    "mtsbank.ru",
    "mts.ru",
    "pochtabank.ru",
    "uralsib.ru",
    "akbars.ru",
    "otpbank.ru",
    "homecredit.ru",
    "rencredit.ru",
    "ozonbank.ru",
    "yoomoney.ru",
    "yookassa.ru",
    "qiwi.com",
    # маркетплейсы / повседневные РФ-сервисы, которым зарубежный IP ломает вход
    "wildberries.ru",
    "wb.ru",
    "ozon.ru",
    "market.yandex.ru",
    "megamarket.ru",
    "dzen.ru",
)
