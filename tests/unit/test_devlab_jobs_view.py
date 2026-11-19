from __future__ import annotations

import re
from html import escape
from typing import Any

from sa_home_bot.bot.devlab_jobs_view import (
    CB_JOBS,
    CODE_CARD,
    CODE_PAGE,
    PAGE_SIZE,
    QUESTION_CHARS,
    TEXT_LIMIT,
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
        protocol.FIELD_PAYLOAD: {
            protocol.PAYLOAD_MODEL: model,
            protocol.PARAM_PURPOSE: purpose,
        },
        "project": "proj",
        "created": "2026-11-01T12:00:00",
        "started": "2026-11-01T12:01:00",
        "executor": "exec-1",
        protocol.PAYLOAD_TOKENS: 1000,
    }
    if kind == protocol.JOB_KIND_TEST and ref:
        job[protocol.FIELD_PAYLOAD][protocol.FIELD_REF] = ref
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
    line = job_line(1, job, 0)
    assert "1." in line
    assert "🟢" in line
    assert "fix" in line
    assert "gpt-4o" in line
    assert f'data="{CB_JOBS}:{CODE_CARD}:job-1:0"' in line


def test_job_line_test_uses_ref() -> None:
    job = _base_job(1, kind=protocol.JOB_KIND_TEST, ref="refs/heads/test-1")
    line = job_line(1, job, 0)
    assert "тест" in line
    assert "refs/heads/test-1" in line


def test_job_line_status_icons() -> None:
    for status, icon in [
        (protocol.STATUS_RUNNING, "🟢"),
        (protocol.STATUS_WAITING_OWNER, "❓"),
        (protocol.STATUS_QUEUED, "⏳"),
    ]:
        job = _base_job(1, status=status)
        line = job_line(1, job, 0)
        assert icon in line


def test_page_html_empty() -> None:
    html = page_html([], 0, 0, PAGE_SIZE)
    assert "Активных заданий нет" in html


def test_page_html_header_and_lines() -> None:
    jobs = [_base_job(i) for i in range(5)]
    html = page_html(jobs, 0, 5, PAGE_SIZE)
    assert "Задания devlab" in html
    assert "5 активных" in html
    assert "стр. 1 из 1" in html
    for i in range(5):
        assert f"{i + 1}." in html


def test_page_html_offset_pagination() -> None:
    jobs = [_base_job(i) for i in range(25)]
    page_jobs = jobs[20:20 + PAGE_SIZE]
    html = page_html(page_jobs, 20, 25, PAGE_SIZE)
    assert "стр. 2 из 2" in html
    assert "21." in html
    assert f'data="{CB_JOBS}:{CODE_CARD}:job-20:20"' in html


def test_page_html_long_titles_fallback_to_small_page() -> None:
    long_title = "x" * 300
    jobs = [_base_job(i, title=long_title) for i in range(50)]
    msg, markup_kb, size = render_page(jobs, 0, len(jobs))
    assert size == PAGE_SIZE
    assert len(msg.html) <= TEXT_LIMIT


def test_render_page_size_20_with_nav() -> None:
    jobs = [_base_job(i) for i in range(25)]
    msg, markup_kb, size = render_page(jobs, 0, 25)
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
    msg, markup_kb, _ = render_page(jobs, 20, 25)
    link_datas = [m.group(1) for m in re.finditer(r'data="([^"]+)"', msg.html)]
    assert all(d.endswith(":20") for d in link_datas)


def test_render_page_long_titles_no_fallback() -> None:
    long_title = "x" * 500
    jobs = [_base_job(i, title=long_title) for i in range(PAGE_SIZE + 5)]
    msg, markup_kb, size = render_page(jobs, 0, len(jobs))
    assert size == PAGE_SIZE
    assert len(msg.html) <= TEXT_LIMIT
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values


def test_render_page_clamp_offset() -> None:
    jobs = [_base_job(i) for i in range(5)]
    msg, markup_kb, _ = render_page(jobs, 100, 5)
    assert "стр. 1 из 1" in msg.html


def test_render_card_buttons_with_offset_20() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 20)
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:20" in data_values
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values
    assert f"{CB_JOBS}:{CODE_CARD}:job-1:20" in data_values


def test_render_card_buttons_with_offset_0_no_home() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 0)
    data_values = [btn.callback_data for row in markup_kb.inline_keyboard for btn in row]
    assert f"{CB_JOBS}:{CODE_PAGE}:0" in data_values
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
    msg, _ = render_card(job, 0)
    assert "job-1" in msg.html
    assert "fix" in msg.html
    assert "running" in msg.html
    assert "proj" in msg.html
    assert "Title" in msg.html
    assert "gpt-4o" in msg.html
    assert "exec-1" in msg.html
    assert "2026-11-01T12:00:00" in msg.html
    assert "1000" in msg.html
    assert "Please confirm?" in msg.html


def test_render_card_question_clipped() -> None:
    long_question = "x" * (QUESTION_CHARS + 100)
    job = _base_job(1)
    job[protocol.FIELD_QUESTION] = long_question
    msg, _ = render_card(job, 0)
    assert len(msg.html) <= TEXT_LIMIT
    assert msg.html.count("x") <= QUESTION_CHARS + 100


def test_html_escape_in_title() -> None:
    job = _base_job(1, title="<b>bold</b> & <i>italic</i>")
    msg, _, _ = render_page([job], 0, 1)
    assert "<b>bold</b>" not in msg.html
    assert escape("<b>bold</b>") in msg.html
    assert "&" in msg.html
    assert escape("<i>italic</i>") in msg.html


def test_callback_data_length_limit() -> None:
    job = _base_job(1)
    msg, markup_kb = render_card(job, 0)
    for row in markup_kb.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode()) <= 64
    for match in re.finditer(r'data="([^"]+)"', msg.html):
        assert len(match.group(1).encode()) <= 64
