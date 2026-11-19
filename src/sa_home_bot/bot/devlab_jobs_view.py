from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

from sa_home_bot.bot.pagination import clamp_offset, nav_row
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import button, markup, refresh_row
from sa_home_bot.devlab import protocol
from sa_home_bot.utils.text import clip
from sa_home_bot.utils.timefmt import format_dhms

CB_JOBS = "dj"
CODE_PAGE = "p"
CODE_CARD = "c"
CODE_TASK = "t"
PAGE_SIZE = 20
SMALL_PAGE_SIZE = 10
TEXT_LIMIT = 4096
TITLE_CHARS = 60
QUESTION_CHARS = 1500
STATUS_ICONS = {
    protocol.STATUS_RUNNING: "🟢",
    protocol.STATUS_WAITING_OWNER: "❓",
    protocol.STATUS_QUEUED: "⏳",
}
SECTION_ORDER = (
    protocol.STATUS_RUNNING,
    protocol.STATUS_WAITING_OWNER,
    protocol.STATUS_QUEUED,
)
SECTION_TITLES = {
    protocol.STATUS_RUNNING: "🟢 Идут",
    protocol.STATUS_WAITING_OWNER: "❓ Ждут ответа",
    protocol.STATUS_QUEUED: "⏳ Очередь",
}


def page_cb(offset: int) -> str:
    return pack(CB_JOBS, CODE_PAGE, offset)


def card_cb(job_id: str, offset: int) -> str:
    return pack(CB_JOBS, CODE_CARD, job_id, offset)


def task_cb(job_id: str) -> str:
    return pack(CB_JOBS, CODE_TASK, job_id)


def link(text: str, data: str) -> str:
    escaped_text = escape(text)
    escaped_data = escape(data, quote=True)
    return (
        f'<tg-button type="callback_data" style="link" data="{escaped_data}">'
        f"{escaped_text}</tg-button>"
    )


def job_type(job: dict[str, Any]) -> str:
    kind = job.get(protocol.FIELD_KIND)
    if kind == protocol.JOB_KIND_TEST:
        return "тест"
    params = job.get(protocol.FIELD_PAYLOAD) or {}
    purpose = params.get(protocol.PARAM_PURPOSE)
    return purpose or protocol.PURPOSE_DEV


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _age_seconds(
    created: str | None, started: str | None, now: datetime, status: str
) -> int | None:
    if status in (protocol.STATUS_RUNNING, protocol.STATUS_WAITING_OWNER):
        dt = _parse_dt(started)
    elif status == protocol.STATUS_QUEUED:
        dt = _parse_dt(created)
    else:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int((now - dt).total_seconds())


def _first_waiting_dep(job: dict[str, Any]) -> str | None:
    deps = job.get(protocol.FIELD_DEPS)
    if not deps:
        return None
    for dep in deps:
        if dep.get("status") != protocol.STATUS_DONE:
            return str(dep.get("id", ""))
    return None


def _role(job: dict[str, Any]) -> str | None:
    params = job.get(protocol.FIELD_PAYLOAD) or {}
    profile = params.get(protocol.PARAM_PROFILE)
    if profile and profile != protocol.DEFAULT_PROFILE:
        return profile
    return None


def job_line(n: int, job: dict[str, Any], offset: int, now: datetime) -> str:
    job_id = str(job.get(protocol.FIELD_ID, ""))
    status = job.get(protocol.FIELD_STATUS, "")
    icon = STATUS_ICONS.get(status, "")
    jtype = job_type(job)
    kind = job.get(protocol.FIELD_KIND)
    params = job.get(protocol.FIELD_PAYLOAD) or {}
    model = params.get(protocol.PAYLOAD_MODEL, "")
    if kind == protocol.JOB_KIND_TEST:
        model = params.get(protocol.FIELD_REF, model)
    title = clip(str(job.get(protocol.FIELD_TITLE, "")), TITLE_CHARS)
    linked_id = link(job_id, card_cb(job_id, offset))

    parts = [f"{n}. {icon} {linked_id} · {jtype} · {model}"]

    age_sec = _age_seconds(
        job.get("created"), job.get("started"), now, status
    )
    if age_sec is not None:
        parts.append(f"· {format_dhms(age_sec)}")

    if status == protocol.STATUS_RUNNING:
        steps = job.get(protocol.FIELD_STEPS, 0)
        if steps:
            parts.append(f"· шаг {steps}")

    dep_id = _first_waiting_dep(job)
    if dep_id:
        parts.append(f"· ⛓ {dep_id}")

    role_str = _role(job)
    if role_str:
        parts.append(f"· {role_str}")

    parts.append(escape(title))
    return f"<p>{' '.join(parts)}</p>"


def page_html(
    jobs: list[dict[str, Any]],
    offset: int,
    total: int,
    size: int,
    now: datetime,
    counts: dict[str, int] | None = None,
) -> str:
    if not jobs:
        return "<p>Активных заданий нет.</p>"
    pages_total = (total + size - 1) // size
    current_page = offset // size + 1

    header_parts = [f"<b>Задания devlab</b>: {total} активных"]
    if counts:
        running_c = counts.get(protocol.STATUS_RUNNING, 0)
        waiting_c = counts.get(protocol.STATUS_WAITING_OWNER, 0)
        queued_c = counts.get(protocol.STATUS_QUEUED, 0)
        if running_c or waiting_c or queued_c:
            header_parts.append(f" (🟢{running_c} ❓{waiting_c} ⏳{queued_c})")
    header_parts.append(f" · стр. {current_page} из {pages_total}")
    lines = ["<p>" + "".join(header_parts) + "</p>"]

    page_jobs = jobs[:size]
    by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTION_ORDER}
    for job in page_jobs:
        status = job.get(protocol.FIELD_STATUS, "")
        if status in by_status:
            by_status[status].append(job)
        else:
            by_status.setdefault(status, []).append(job)

    n = offset + 1
    for status in SECTION_ORDER:
        section_jobs = by_status.get(status, [])
        if not section_jobs:
            continue
        lines.append(f"<p><b>{SECTION_TITLES[status]}</b></p>")
        for job in section_jobs:
            lines.append(job_line(n, job, offset, now))
            n += 1
    return "\n".join(lines)


def render_page(
    jobs: list[dict[str, Any]],
    offset: int,
    total: int,
    now: datetime | None = None,
    counts: dict[str, int] | None = None,
) -> tuple[InputRichMessage, InlineKeyboardMarkup, int]:
    if now is None:
        now = datetime.now(UTC)
    size = PAGE_SIZE
    page_jobs = jobs[offset : offset + size]
    html = page_html(page_jobs, offset, total, size, now, counts)
    if len(html) > TEXT_LIMIT:
        size = SMALL_PAGE_SIZE
        page_jobs = jobs[offset : offset + size]
        html = page_html(page_jobs, offset, total, size, now, counts)
    clamped_offset = clamp_offset(offset, size, total)
    if clamped_offset != offset:
        page_jobs = jobs[clamped_offset : clamped_offset + size]
        html = page_html(page_jobs, clamped_offset, total, size, now, counts)
        offset = clamped_offset
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav = nav_row(offset, size, total, page_cb)
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append(refresh_row(page_cb(offset)))
    return InputRichMessage(html=html), markup(*keyboard_rows), size


def _format_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def _deps_text(job: dict[str, Any]) -> str | None:
    deps = job.get(protocol.FIELD_DEPS)
    if not deps:
        return None
    parts = []
    for dep in deps:
        dep_id = dep.get("id", "")
        dep_status = dep.get("status", "")
        if dep_id:
            parts.append(f"{dep_id} ({dep_status})")
    return ", ".join(parts) if parts else None


def render_card(
    job: dict[str, Any], offset: int, now: datetime | None = None
) -> tuple[InputRichMessage, InlineKeyboardMarkup]:
    if now is None:
        now = datetime.now(UTC)

    job_id = str(job.get(protocol.FIELD_ID, ""))
    status = job.get(protocol.FIELD_STATUS, "")
    jtype = job_type(job)
    project = job.get("project", "")
    title = job.get(protocol.FIELD_TITLE, "")
    params = job.get(protocol.FIELD_PAYLOAD) or {}
    model = params.get(protocol.PAYLOAD_MODEL, "")
    if job.get(protocol.FIELD_KIND) == protocol.JOB_KIND_TEST:
        model = params.get(protocol.FIELD_REF, model)
    created = job.get("created")
    started = job.get("started")
    tokens = job.get(protocol.PAYLOAD_TOKENS, 0)
    question = job.get(protocol.FIELD_QUESTION)
    base_branch = params.get(protocol.PARAM_DEPENDS_ON, "")
    steps = job.get(protocol.FIELD_STEPS, 0)
    last_step = job.get(protocol.FIELD_LAST_STEP, "")
    fallback = params.get(protocol.PARAM_FALLBACK)
    original_model = params.get(protocol.PARAM_ORIGINAL_MODEL)
    role_str = _role(job)

    lines = [
        f"<p>Задание <code>{escape(job_id)}</code> · {jtype} · {escape(status)}</p>",
        f"<p>Проект: {escape(project)}</p>",
        f"<p>Заголовок: {escape(title)}</p>",
        f"<p>Модель: {escape(model)}</p>",
    ]
    if base_branch:
        lines.append(f"<p>Основа: {escape(base_branch)}</p>")
    lines.append(f"<p>Ветка: {escape(protocol.AGENT_BRANCH_PREFIX + job_id)}</p>")
    if role_str:
        lines.append(f"<p>Роль: {escape(role_str)}</p>")
    deps_text = _deps_text(job)
    if deps_text:
        lines.append(f"<p>Зависимости: {escape(deps_text)}</p>")

    if status in (protocol.STATUS_RUNNING, protocol.STATUS_WAITING_OWNER):
        age_sec = _age_seconds(created, started, now, status)
        if age_sec is not None:
            lines.append(f"<p>Идёт {format_dhms(age_sec)}</p>")
    elif status == protocol.STATUS_QUEUED:
        age_sec = _age_seconds(created, started, now, status)
        if age_sec is not None:
            lines.append(f"<p>В очереди {format_dhms(age_sec)}</p>")

    if fallback:
        lines.append(f"<p>Запасная модель: {escape(str(fallback))}</p>")
    if original_model:
        lines.append(f"<p>Взята вместо: {escape(str(original_model))}</p>")

    lines.append(f"<p>Создано / начато: {_format_dt(created)} / {_format_dt(started)}</p>")
    lines.append(f"<p>Токены: {tokens}</p>")
    if steps > 0 and last_step:
        clipped = clip(str(last_step), QUESTION_CHARS)
        lines.append(f"<p>Последний шаг ({steps}): {escape(clipped)}</p>")
    if question:
        clipped = clip(str(question), QUESTION_CHARS)
        lines.append(f"<p>Вопрос: {escape(clipped)}</p>")

    html = "\n".join(lines)

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    keyboard_rows.append([button("📄 Задача", task_cb(job_id))])
    keyboard_rows.append([button("⬅️ К списку", page_cb(offset))])
    if offset > 0:
        keyboard_rows.append([button("⏮ В начало", page_cb(0))])
    keyboard_rows.append(refresh_row(card_cb(job_id, offset)))

    return InputRichMessage(html=html), markup(*keyboard_rows)
