from __future__ import annotations

import re
from datetime import UTC, datetime
from html import escape
from typing import Any

import pytest

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.devlab_jobs_view import (
    CARD_TIME_FORMAT,
    CB_JOBS,
    CODE_ACT_RELEASE,
    CODE_ACT_RESUME,
    CODE_ACT_STOP,
    CODE_ACT_STOP_OK,
    CODE_CARD,
    CODE_PAGE,
    CODE_TASK,
    PAGE_SIZE,
    QUESTION_CHARS,
    TEXT_LIMIT,
    act_cb,
    card_cb,
    job_line,
    job_type,
    link,
    page_cb,
    page_html,
    render_card,
    render_page,
)
from sa_home_bot.devlab import protocol
from sa_home_bot.utils.timefmt import format_local

NOW = datetime(2026, 11, 1, 12, 5, 0, tzinfo=UTC)


def _base_job(
    idx: int,
    kind: str = protocol.JOB_KIND_AGENT,
    purpose: str = protocol.PURPOSE_DEV,
    status: str = protocol.STATUS_RUNNING,
    title: str = "Title",
    model: str = "gpt-4o",
    ref: str | None = None,
) -> dict[str, Any]:
    job = {
        protocol.FIELD_ID: f"job-{idx}",
        protocol.FIELD_KIND: kind,
        protocol.FIELD_STATUS: status,
        protocol.FIELD_TITLE: title,
        protocol.FIELD_MODEL: model,
        protocol.FIELD_PARAMS: {
            protocol.PARAM_PURPOSE: purpose,
        },
        "project": "proj",
        "created": "2026-11-01T12:00:00+00:00",
        "started": "2026-11-01T12:01:00+00:00",
        "executor": "exec-1",
        protocol.PAYLOAD_TOKENS: 1000,
    }
    if kind == protocol.JOB_KIND_TEST and ref:
        job[protocol.FIELD_PARAMS][protocol.FIELD_REF] = ref
    return job


def test_job_type_agent_purpose() -> None:
    job = _base_job(1, purpose=protocol.PURPOSE_FIX)
    assert job_type(job) == "fix"


def test_job_type_agent_dev_default() -> None:
    job = _base_job(1)
    assert job_type(job) == "dev"


def test_job_type_test() -> None:
    job = _base_job(1, kind=protocol.JOB_KIND_TEST)
    assert job_type(job) == "тест"


def test_link_escapes_text_and_data() -> None:
    data = "dj:c:job-1:0"
    text = "job-1"
    result = link(text, data)
    assert 'data="dj:c:job-1:0"' in result
    assert ">job-1<" in result
    assert "tg-button" in result
    assert 'style="link"' in result


def test_link_escapes_html_in_text() -> None:
    result = link("<b>bold</b>", "data")
    assert "<b>bold</b>" not in result
    assert escape("<b>bold</b>") in result


def test_link_escapes_quotes_in_data() -> None:
    result = link("text", 'data"with"quotes')
    assert "data=" in result


def test_page_cb_format() -> None:
    assert page_cb(0) == f"{CB_JOBS}:{CODE_PAGE}:0"
    assert page_cb(20) == f"{CB_JOBS}:{CODE_PAGE}:20"


def test_card_cb_format() -> None:
    assert card_cb("job-1", 0) == f"{CB_JOBS}:{CODE_CARD}:job-1:0"
    assert card_cb("job-1", 20) == f"{CB_JOBS}:{CODE_CARD}:job-1:20"


def test_job_line_agent() -> None:
    job = _base_job(1, purpose=protocol.PURPOSE_FIX, model="gpt-4o")
    line = job_line(1, job, 0, NOW)
    assert "1." in line
    assert "🟢" in line
    assert "fix" in line
    assert "gpt-4o" in line
    assert f'data="{CB_JOBS}:{CODE_CARD}:job-1:0"' in line
    assert "4м" in line


def test_job_line_test_uses_ref() -> None:
    job = _base_job(1, kind=protocol.JOB_KIND_TEST, ref="refs/heads/test-1")
    line = job_line(1, job, 0, NOW)
    assert "тест" in line
    assert "refs/heads/test-1" in line


def test_job_line_status_icons() -> None:
    for status, icon in [
        (protocol.STATUS_RUNNING, "🟢"),
        (protocol.STATUS_WAITING_OWNER, "❓"),
        (protocol.STATUS_QUEUED, "⏳"),
    ]:
        job = _base_job(1, status=status)
        line = job_line(1, job, 0, NOW)
        assert icon in line


def test_job_line_queued_age_from_created() -> None:
    job = _base_job(1, status=protocol.STATUS_QUEUED)
    line = job_line(1, job, 0, NOW)
    assert "5м" in line


def test_job_line_running_shows_step() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    job[protocol.FIELD_STEPS] = 3
    line = job_line(1, job, 0, NOW)
    assert "шаг 3" in line


def test_job_line_waiting_owner_no_step() -> None:
    job = _base_job(1, status=protocol.STATUS_WAITING_OWNER)
    job[protocol.FIELD_STEPS] = 2
    line = job_line(1, job, 0, NOW)
    assert "шаг" not in line


def test_job_line_dependency_waiting() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    job[protocol.FIELD_DEPS] = [
        {"id": "job-5", "status": protocol.STATUS_DONE},
        {"id": "job-6", "status": protocol.STATUS_RUNNING},
    ]
    line = job_line(1, job, 0, NOW)
    assert "⛓ job-6" in line


def test_job_line_dependency_all_done() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    job[protocol.FIELD_DEPS] = [
        {"id": "job-5", "status": protocol.STATUS_DONE},
    ]
    line = job_line(1, job, 0, NOW)
    assert "⛓" not in line


def test_job_line_role_executor_hidden() -> None:
    job = _base_job(1)
    job[protocol.FIELD_PARAMS][protocol.PARAM_PROFILE] = protocol.DEFAULT_PROFILE
    line = job_line(1, job, 0, NOW)
    assert "executor" not in line


def test_job_line_role_docs_visible() -> None:
    job = _base_job(1)
    job[protocol.FIELD_PARAMS][protocol.PARAM_PROFILE] = "docs"
    line = job_line(1, job, 0, NOW)
    assert "docs" in line


def test_page_html_empty() -> None:
    html = page_html([], 0, 0, PAGE_SIZE, NOW)
    assert "Активных заданий нет" in html


def test_page_html_header_and_lines() -> None:
    jobs = [_base_job(i) for i in range(5)]
    html = page_html(jobs, 0, 5, PAGE_SIZE, NOW)
    assert "Задания devlab" in html
    assert "5 активных" in html
    assert "стр. 1 из 1" in html
    for i in range(5):
        assert f"{i + 1}." in html


def test_page_html_sections_order_and_counters() -> None:
    jobs = [
        _base_job(1, status=protocol.STATUS_QUEUED),
        _base_job(2, status=protocol.STATUS_RUNNING),
        _base_job(3, status=protocol.STATUS_WAITING_OWNER),
        _base_job(4, status=protocol.STATUS_QUEUED),
    ]
    counts = {
        protocol.STATUS_RUNNING: 5,
        protocol.STATUS_WAITING_OWNER: 2,
        protocol.STATUS_QUEUED: 3,
    }
    html = page_html(jobs, 0, 4, PAGE_SIZE, NOW, counts)

    assert "🟢 Идут" in html
    assert "❓ Ждут ответа" in html
    assert "⏳ Очередь" in html

    running_pos = html.index("🟢 Идут")
    waiting_pos = html.index("❓ Ждут ответа")
    queued_pos = html.index("⏳ Очередь")
    assert running_pos < waiting_pos < queued_pos

    assert "🟢5" in html
    assert "❓2" in html
    assert "⏳3" in html


def test_page_html_section_empty_not_shown() -> None:
    jobs = [_base_job(1, status=protocol.STATUS_RUNNING)]
    html = page_html(jobs, 0, 1, PAGE_SIZE, NOW)
    assert "❓ Ждут ответа" not in html
    assert "⏳ Очередь" not in html


def test_page_html_offset_pagination() -> None:
    jobs = [_base_job(i) for i in range(25)]
    page_jobs = jobs[20:20 + PAGE_SIZE]
    html = page_html(page_jobs, 20, 25, PAGE_SIZE, NOW)
    assert "стр. 2 из 2" in html
    assert "21." in html
    assert f'data="{CB_JOBS}:{CODE_CARD}:job-20:20"' in html


def test_page_html_long_titles_fallback_to_small_page() -> None:
    long_title = "x" * 300
    jobs = [_base_job(i, title=long_title) for i in range(50)]
    msg, markup_kb, size = render_page(jobs[:PAGE_SIZE], 0, len(jobs), NOW)
    assert size == PAGE_SIZE
    assert len(msg.html) <= TEXT_LIMIT


def test_render_page_size_20_with_nav() -> None:
    jobs = [_base_job(i) for i in range(25)]
    msg, markup_kb, size = render_page(jobs[:PAGE_SIZE], 0, 25, NOW)
    assert size == PAGE_SIZE
    assert isinstance(msg, type(msg))
    assert msg.html is not None
    assert "20." in msg.html
    assert "21." not in msg.html
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values
    link_datas = []
    for match in re.finditer(r'data="([^"]+)"', msg.html):
        link_datas.append(match.group(1))
    assert len(link_datas) == 20
    assert all(d.startswith(f"{CB_JOBS}:{CODE_CARD}:job-") for d in link_datas)
    assert all(d.endswith(":0") for d in link_datas)


def test_render_page_offset_20_links_carry_offset() -> None:
    jobs = [_base_job(i) for i in range(25)]
    msg, markup_kb, _ = render_page(jobs[20:25], 20, 25, NOW)
    for n in range(21, 26):
        assert f"{n}." in msg.html
    link_datas = [m.group(1) for m in re.finditer(r'data="([^"]+)"', msg.html)]
    assert all(d.endswith(":20") for d in link_datas)


def test_render_page_long_titles_no_fallback() -> None:
    long_title = "x" * 500
    jobs = [_base_job(i, title=long_title) for i in range(PAGE_SIZE + 5)]
    msg, markup_kb, size = render_page(jobs[:PAGE_SIZE], 0, len(jobs), NOW)
    assert size == PAGE_SIZE
    assert len(msg.html) <= TEXT_LIMIT
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values


def test_render_page_offset_20_shows_five_jobs() -> None:
    jobs = [_base_job(i) for i in range(5)]
    msg, markup_kb, _ = render_page(jobs, 20, 25, NOW)
    for n in range(21, 26):
        assert f"{n}." in msg.html
    assert "<p>1." not in msg.html
    assert "<p>26." not in msg.html
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values


def test_render_card_buttons_with_offset_20() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 20, NOW)
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values
    assert f"{CB_JOBS}:{CODE_CARD}:job-1:20" in data_values


def test_render_card_buttons_with_offset_0_no_home() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 0, NOW)
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values
    home_buttons = [
        btn
        for row in markup_kb.inline_keyboard
        for btn in row
        if btn.text == "⏮ В начало"
    ]
    assert len(home_buttons) == 0


def test_render_card_includes_all_fields() -> None:
    job = _base_job(1, purpose=protocol.PURPOSE_FIX, model="gpt-4o")
    job[protocol.FIELD_QUESTION] = "Please confirm?"
    job[protocol.FIELD_STEPS] = 2
    job[protocol.FIELD_LAST_STEP] = "Step 2 done"
    job[protocol.FIELD_DEPS] = [{"id": "job-5", "status": protocol.STATUS_RUNNING}]
    job[protocol.FIELD_PARAMS][protocol.PARAM_FALLBACK] = "gpt-4o-mini"
    job[protocol.FIELD_PARAMS][protocol.PARAM_ORIGINAL_MODEL] = "gpt-5"
    job["base_branch"] = "base/main"
    job[protocol.FIELD_PARAMS][protocol.PARAM_DEPENDS_ON] = ["job-5"]
    msg, _ = render_card(job, 0, NOW)

    assert "job-1" in msg.html
    assert "fix" in msg.html
    assert "running" in msg.html
    assert "proj" in msg.html
    assert "Title" in msg.html
    assert "gpt-4o" in msg.html
    assert "base/main" in msg.html
    assert "agent/job-1" in msg.html
    assert "Зависимости: job-5 (running)" in msg.html
    assert "Идёт 4м" in msg.html
    assert "Запасная модель: gpt-4o-mini" in msg.html
    assert "Взята вместо: gpt-5" in msg.html
    created = format_local("2026-11-01T12:00:00+00:00", CARD_TIME_FORMAT)
    started = format_local("2026-11-01T12:01:00+00:00", CARD_TIME_FORMAT)
    assert f"Создано / начато: {created} / {started}" in msg.html
    assert "1000" in msg.html
    assert "Последний шаг (2): Step 2 done" in msg.html
    assert "Please confirm?" in msg.html


def test_render_card_minimal_fields() -> None:
    job = _base_job(1, status=protocol.STATUS_QUEUED)
    msg, _ = render_card(job, 0, NOW)

    assert "job-1" in msg.html
    assert "dev" in msg.html
    assert "queued" in msg.html
    assert "В очереди" in msg.html
    assert "Идёт" not in msg.html
    assert "Роль" not in msg.html
    assert "Зависимости" not in msg.html
    assert "Запасная модель" not in msg.html
    assert "Взята вместо" not in msg.html
    assert "Последний шаг" not in msg.html
    assert "Вопрос" not in msg.html


def test_render_card_question_clipped() -> None:
    long_question = "x" * (QUESTION_CHARS + 100)
    job = _base_job(1)
    job[protocol.FIELD_QUESTION] = long_question
    msg, _ = render_card(job, 0, NOW)
    assert len(msg.html) <= TEXT_LIMIT
    assert msg.html.count("x") <= QUESTION_CHARS + 100


def test_html_escape_in_title() -> None:
    job = _base_job(1, title="<b>bold</b> & <i>italic</i>")
    msg, _, _ = render_page([job], 0, 1, NOW)
    assert "<b>bold</b>" not in msg.html
    assert escape("<b>bold</b>") in msg.html
    assert "&" in msg.html
    assert escape("<i>italic</i>") in msg.html


def test_callback_data_length_limit() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 0, NOW)
    for row in markup_kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode()) <= 64
    for match in re.finditer(r'data="([^"]+)"', msg.html):
        assert len(match.group(1).encode()) <= 64


def test_act_cb_format() -> None:
    assert act_cb(CODE_ACT_STOP, "job-1", 20) == f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:20"
    assert act_cb(CODE_ACT_STOP_OK, "job-1", 0) == f"{CB_JOBS}:{CODE_ACT_STOP_OK}:job-1:0"
    assert act_cb(CODE_ACT_RELEASE, "job-1", 0) == f"{CB_JOBS}:{CODE_ACT_RELEASE}:job-1:0"
    assert act_cb(CODE_ACT_RESUME, "job-1", 0) == f"{CB_JOBS}:{CODE_ACT_RESUME}:job-1:0"


def _action_buttons(job, offset=0, confirm_stop=False):
    _, markup_kb = render_card(job, offset, NOW, confirm_stop=confirm_stop)
    return [btn for row in markup_kb.inline_keyboard for btn in row]


def test_card_action_row_running_has_stop() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    buttons = _action_buttons(job)
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert ai_lines.DEVLAB_BTN_STOP in texts
    assert f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:0" in datas
    assert f"{CB_JOBS}:{CODE_ACT_RELEASE}:job-1:0" not in datas
    assert f"{CB_JOBS}:{CODE_ACT_RESUME}:job-1:0" not in datas


def test_card_action_row_waiting_owner_has_release_and_stop() -> None:
    job = _base_job(1, status=protocol.STATUS_WAITING_OWNER)
    buttons = _action_buttons(job)
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert ai_lines.DEVLAB_BTN_RELEASE in texts
    assert ai_lines.DEVLAB_BTN_STOP in texts
    assert f"{CB_JOBS}:{CODE_ACT_RELEASE}:job-1:0" in datas
    assert f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:0" in datas


@pytest.mark.parametrize(
    "status", [protocol.STATUS_STOPPED, protocol.STATUS_FAILED]
)
def test_card_action_row_stopped_failed_has_resume(status) -> None:
    job = _base_job(1, status=status)
    buttons = _action_buttons(job)
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert ai_lines.DEVLAB_BTN_RESUME in texts
    assert f"{CB_JOBS}:{CODE_ACT_RESUME}:job-1:0" in datas
    assert f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:0" not in datas


def test_card_action_row_queued_none() -> None:
    job = _base_job(1, status=protocol.STATUS_QUEUED)
    buttons = _action_buttons(job)
    for btn in buttons:
        for code in (CODE_ACT_STOP, CODE_ACT_STOP_OK, CODE_ACT_RELEASE, CODE_ACT_RESUME):
            assert not btn.callback_data.startswith(f"{CB_JOBS}:{code}:")


def test_card_confirm_stop_replaces_action_row() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    buttons = _action_buttons(job, confirm_stop=True)
    texts = [btn.text for btn in buttons]
    datas = [btn.callback_data for btn in buttons]
    assert ai_lines.DEVLAB_BTN_STOP_CONFIRM in texts
    assert ai_lines.DEVLAB_BTN_CANCEL in texts
    assert f"{CB_JOBS}:{CODE_ACT_STOP_OK}:job-1:0" in datas
    assert f"{CB_JOBS}:{CODE_CARD}:job-1:0" in datas
    assert f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:0" not in datas


def test_card_action_row_position_before_navigation() -> None:
    job = _base_job(1, status=protocol.STATUS_RUNNING)
    _, markup_kb = render_card(job, 20, NOW)
    rows = markup_kb.inline_keyboard
    assert rows[0][0].callback_data == f"{CB_JOBS}:{CODE_TASK}:job-1"
    assert rows[1][0].callback_data == f"{CB_JOBS}:{CODE_ACT_STOP}:job-1:20"
    assert rows[2][0].callback_data == f"{CB_JOBS}:{CODE_PAGE}:20"
