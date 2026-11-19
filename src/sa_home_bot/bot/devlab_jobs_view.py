from __future__ import annotations

from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

from sa_home_bot.bot.pagination import clamp_offset, nav_row
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import button, markup, refresh_row
from sa_home_bot.devlab import protocol
from sa_home_bot.utils.text import clip

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


def job_line(n: int, job: dict[str, Any], offset: int) -> str:
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
    return f"<p>{n}. {icon} {linked_id} · {jtype} · {model} · {escape(title)}</p>"


def page_html(jobs: list[dict[str, Any]], offset: int, total: int, size: int) -> str:
    if not jobs:
        return "<p>Активных заданий нет.</p>"
    pages_total = (total + size - 1) // size
    current_page = offset // size + 1
    lines = [
        f"<p><b>Задания devlab</b>: {total} активных · стр. {current_page} из {pages_total}</p>"
    ]
    page_jobs = jobs[:size]
    for i, job in enumerate(page_jobs):
        lines.append(job_line(offset + i + 1, job, offset))
    return "\n".join(lines)


def render_page(
    jobs: list[dict[str, Any]], offset: int, total: int
) -> tuple[InputRichMessage, InlineKeyboardMarkup, int]:
    size = PAGE_SIZE
    page_jobs = jobs[offset : offset + size]
    html = page_html(page_jobs, offset, total, size)
    if len(html) > TEXT_LIMIT:
        size = SMALL_PAGE_SIZE
        page_jobs = jobs[offset : offset + size]
        html = page_html(page_jobs, offset, total, size)
    clamped_offset = clamp_offset(offset, size, total)
    if clamped_offset != offset:
        page_jobs = jobs[clamped_offset : clamped_offset + size]
        html = page_html(page_jobs, clamped_offset, total, size)
        offset = clamped_offset
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    nav = nav_row(offset, size, total, page_cb)
    if nav:
        keyboard_rows.append(nav)
    keyboard_rows.append(refresh_row(page_cb(offset)))
    return InputRichMessage(html=html), markup(*keyboard_rows), size


def render_card(
    job: dict[str, Any], offset: int
) -> tuple[InputRichMessage, InlineKeyboardMarkup]:
    job_id = str(job.get(protocol.FIELD_ID, ""))
    status = job.get(protocol.FIELD_STATUS, "")
    jtype = job_type(job)
    project = job.get("project", "")
    title = job.get(protocol.FIELD_TITLE, "")
    params = job.get(protocol.FIELD_PAYLOAD) or {}
    model = params.get(protocol.PAYLOAD_MODEL, "")
    if job.get(protocol.FIELD_KIND) == protocol.JOB_KIND_TEST:
        model = params.get(protocol.FIELD_REF, model)
    executor = job.get("executor", "")
    created = str(job.get("created", ""))
    started = str(job.get("started", ""))
    tokens = job.get(protocol.PAYLOAD_TOKENS, 0)
    question = job.get(protocol.FIELD_QUESTION)

    lines = [
        f"<p>Задание <code>{escape(job_id)}</code> · {jtype} · {escape(status)}</p>",
        f"<p>Проект: {escape(project)}</p>",
        f"<p>Заголовок: {escape(title)}</p>",
        f"<p>Модель: {escape(model)}</p>",
        f"<p>Исполнитель: {escape(executor)}</p>",
        f"<p>Создано / начато: {escape(created)} / {escape(started)}</p>",
        f"<p>Токены: {tokens}</p>",
    ]
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
