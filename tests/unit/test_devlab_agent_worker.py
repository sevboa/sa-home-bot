from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import pytest

from sa_home_bot.config import DevlabProfileConfig
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.loop import AgentLimits, AgentLoop, AgentResult, AgentStatus
from sa_home_bot.devlab.agent.model import ModelError
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spend import JobSpend, SpendState
from sa_home_bot.devlab.agent.trace import StepTrace
from sa_home_bot.devlab.executor import agent_worker as agent_worker_module
from sa_home_bot.devlab.executor.agent_worker import (
    AGENT_DIR,
    AGENT_STATE_FILE,
    AgentSettings,
    AgentState,
    AgentWorker,
    ParkedEntry,
    SlotRegistry,
    rules_hint,
)
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError
from sa_home_bot.devlab.executor.routing import CatalogEntry
from sa_home_bot.utils.files import load_model
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel, StaticCatalog, local_entry, tool_reply
from tests.unit.test_devlab_agent_repo import hub_branch, tracked_files

env = executor_tests.env
PROJECT = executor_tests.PROJECT
MODEL = "flint"
BASE = "base/feature"
SPEC = (
    b'[test]\ncmd = "sh test.sh"\ntimeout_s = 60\n'
    b'[agent]\nrules = ["RULES.md"]\nforbidden_paths = ["test.sh"]\n'
)
BASE_FILES = {
    ".devlab.toml": SPEC,
    "RULES.md": "правило-один\n".encode(),
    "test.sh": b"grep -q done out.txt\n",
    "out.txt": b"todo\n",
}
EXTERNAL_AGENT_SPEC = (
    b'[agent]\nrules = ["EXT.md"]\nforbidden_paths = ["test.sh"]\n'
)
WRITE_DONE = tool_reply("write_file", "w1", path="out.txt", content="done\n")
FINISH = tool_reply("finish", "f1", summary="сделано")
STARTED_DONE = [protocol.MSG_STARTED, protocol.MSG_DONE]


class BlockingModel(ScriptedChatModel):
    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self.waiting = asyncio.Event()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        if not self.script:
            self.waiting.set()
            await asyncio.Event().wait()
        return await super().complete(messages, tools)


class FlakyHub(ExecutorHub):
    down = False

    async def post(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.down:
            raise HubRequestError("/api/post: ClientConnectorError")
        await super().post(job_id, kind, payload)


def make_worker(
    env,
    model,
    hub_class=ExecutorHub,
    models=(MODEL,),
    sandboxes=None,
    limits=None,
    catalog=None,
    busy=None,
    work_dir="exec",
    slots=None,
    specs_dir=None,
    profiles=None,
) -> AgentWorker:
    hub = hub_class(env.hub_url, env.executor_token)
    env.hubs.append(hub)
    return AgentWorker(
        hub,
        lambda model_name, job_id, style="cb_v3", reminder=None: model,
        sandboxes or (lambda request: LocalSandbox(request.workdir, request.baseline)),
        AgentSettings(
            hub_url=env.hub_url,
            token=env.executor_token,
            work_dir=env.tmp_path / work_dir,
            poll_interval_s=0.01,
            heartbeat_interval_s=0.01,
            **({"limits": limits} if limits else {}),
            **({"profiles": profiles} if profiles else {}),
            push_interval_s=0.0,
            retry_interval_s=0.01,
            **({"slots": slots} if slots else {}),
            specs_dir=specs_dir,
        ),
        catalog or StaticCatalog(*(local_entry(name) for name in models)),
        busy,
    )


async def submit(env, base=BASE, push=True) -> str:
    if push:
        await env.push(base, BASE_FILES)
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=PROJECT,
        model=MODEL,
        task="# сделай out.txt",
        base_branch=base,
    )
    return data["job"]["id"]


async def start_blocked(worker: AgentWorker, model: BlockingModel) -> asyncio.Task:
    running = asyncio.create_task(worker.claim_once())
    async with asyncio.timeout(20):
        await model.waiting.wait()
    return running


def wip_of(job_id: str) -> str:
    return f"{protocol.WIP_BRANCH_PREFIX}{job_id}"


def agent_of(job_id: str) -> str:
    return f"{protocol.AGENT_BRANCH_PREFIX}{job_id}"


async def test_full_path_to_agent_branch(env):
    job_id = await submit(env)
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
    agent_sha = await hub_branch(env, agent_of(job_id))
    assert await tracked_files(env, agent_sha) == sorted(BASE_FILES)
    assert "правило-один" in model.seen[0][0]["content"]
    assert model.seen[0][1]["content"] == "# сделай out.txt"
    assert (await env.store.queue.get_job(job_id)).tokens == 220
    done = await env.store.queue.last_message(job_id, protocol.MSG_DONE)
    assert done.payload[protocol.PAYLOAD_SUMMARY] == "сделано"
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()
    assert load_model(AgentState, env.tmp_path / "exec" / AGENT_STATE_FILE).job is None
    assert not worker.pending_messages


async def test_external_spec_and_rules_win_over_repo(env):
    specs = env.tmp_path / "specs"
    specs.mkdir()
    (specs / f"{PROJECT}.toml").write_bytes(EXTERNAL_AGENT_SPEC)
    (specs / "EXT.md").write_bytes("правило-внешнее\n".encode())
    await env.push(BASE, BASE_FILES)
    job_id = await submit(env, push=False)
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model, specs_dir=specs)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert "правило-внешнее" in model.seen[0][0]["content"]
    assert "правило-один" not in model.seen[0][0]["content"]


async def test_external_rule_outside_specs_dir_is_skipped(env):
    specs = env.tmp_path / "specs-escape"
    specs.mkdir()
    (specs / f"{PROJECT}.toml").write_bytes(b'[agent]\nrules = ["../secret.md"]\n')
    (env.tmp_path / "secret.md").write_bytes("секрет\n".encode())
    await env.push(BASE, BASE_FILES)
    job_id = await submit(env, push=False)
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model, specs_dir=specs)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert "секрет" not in model.seen[0][0]["content"]


async def test_claim_returns_false_without_jobs(env):
    assert not await make_worker(env, ScriptedChatModel()).claim_once()


async def test_stop_closes_job_and_keeps_partial_branch(env):
    job_id = await submit(env)
    model = BlockingModel(WRITE_DONE)
    worker = make_worker(env, model)
    running = await start_blocked(worker, model)

    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()
    assert await running

    assert await env.status(job_id) == protocol.STATUS_STOPPED
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
    assert worker.cursor > 0
    assert not worker.pending_messages


async def test_restart_continues_from_wip(env):
    job_id = await submit(env)
    first_model = BlockingModel(WRITE_DONE)
    running = await start_blocked(make_worker(env, first_model), first_model)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await hub_branch(env, wip_of(job_id))
    assert await env.status(job_id) == protocol.STATUS_RUNNING

    model = ScriptedChatModel(FINISH)
    assert await make_worker(env, model).claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE
    assert "out.txt" in model.seen[0][1]["content"]
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))


async def test_outbox_survives_unreachable_hub_and_restart(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), FlakyHub)
    worker._hub.down = True

    assert await worker.claim_once()

    assert [item.kind for item in worker.pending_messages] == STARTED_DONE
    assert await env.status(job_id) == protocol.STATUS_RUNNING

    restarted = make_worker(env, ScriptedChatModel())
    assert [item.kind for item in restarted.pending_messages] == STARTED_DONE
    await restarted.drain()
    assert not restarted.pending_messages
    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE


async def test_model_failure_requeues_job_and_keeps_wip(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, ModelError("шлюз лёг")))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_QUEUED
    requeued = await env.store.queue.last_message(job_id, protocol.MSG_REQUEUE)
    assert "шлюз лёг" in requeued.payload[protocol.PAYLOAD_TEXT]
    assert await hub_branch(env, wip_of(job_id))


async def test_non_model_failure_still_fails_job(env, monkeypatch):
    job_id = await submit(env)

    class StuckLoop(AgentLoop):
        async def run(self, system: str, task: str, resume: Dialog | None = None) -> AgentResult:
            return AgentResult(AgentStatus.STUCK, reason="застрял")

    monkeypatch.setattr(agent_worker_module, "AgentLoop", StuckLoop)
    worker = make_worker(env, ScriptedChatModel())

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "застрял" in failed.payload[protocol.PAYLOAD_TEXT]


class LimitsProbe(AgentLoop):
    seen: list[AgentLimits] = []

    def __init__(self, model, tools, guard, limits, **kwargs) -> None:
        LimitsProbe.seen.append(limits)
        super().__init__(model, tools, guard, limits, **kwargs)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([CatalogEntry(MODEL, "or", "http://o", remote=True, context=65536)], 58982),
        ([local_entry(MODEL, context=32768)], 16000),
        ([local_entry(MODEL, context=8192)], 7372),
        ([CatalogEntry(MODEL, "or", "http://o", remote=True)], 16000),
        (
            [
                CatalogEntry(MODEL, "a", "http://a", remote=True, context=65536),
                CatalogEntry(MODEL, "b", "http://b", remote=True, context=32768),
            ],
            29491,
        ),
        ([CatalogEntry(MODEL, "or", "http://o", remote=True, context=-5)], 16000),
    ],
)
async def test_agent_window_comes_from_catalog(env, monkeypatch, entries, expected):
    monkeypatch.setattr(agent_worker_module, "AgentLoop", LimitsProbe)
    LimitsProbe.seen = []
    spec = SPEC.replace(b"[agent]\n", b"[agent]\nallow_remote_models = true\n")
    monkeypatch.setitem(BASE_FILES, ".devlab.toml", spec)
    job_id = await submit(env)
    worker = make_worker(
        env,
        ScriptedChatModel(WRITE_DONE, FINISH),
        catalog=StaticCatalog(*entries),
        limits=AgentLimits(context_tokens=16000),
    )

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert LimitsProbe.seen[0].context_tokens == expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([], 1000),
        ([CatalogEntry(MODEL, "or", "http://o", remote=True, free=True)], 0),
        ([CatalogEntry(MODEL, "or", "http://o", remote=False, free=True)], 1000),
        ([CatalogEntry(MODEL, "or", "http://o", remote=True, free=False)], 1000),
        (
            [
                CatalogEntry(MODEL, "or", "http://o", remote=True, free=True),
                CatalogEntry(MODEL, "or", "http://o2", remote=False, free=True),
            ],
            1000,
        ),
    ],
)
def test_token_ceiling_by_catalog_entry(entries, expected):
    assert agent_worker_module.token_ceiling(entries, 1000) == expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([CatalogEntry(MODEL, "or", "http://o", remote=True, free=True)], 0),
        ([CatalogEntry(MODEL, "or", "http://o", remote=False, free=True)], 3000),
    ],
)
async def test_limits_for_token_ceiling(env, entries, expected):
    worker = make_worker(env, ScriptedChatModel(), limits=AgentLimits(max_tokens=1000))
    spend = JobSpend(SpendState(extensions=2), lambda state: None)
    assert worker._limits_for(spend, entries).max_tokens == expected


async def submit_profile(env, profile) -> str:
    await env.push(BASE, BASE_FILES)
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=PROJECT,
        model=MODEL,
        task="# сделай out.txt",
        base_branch=BASE,
        profile=profile,
    )
    return data["job"]["id"]


@pytest.mark.parametrize(
    ("profile_settings", "submitted_profile", "max_steps", "max_wall_s"),
    [
        ({"max_steps": 3}, "custom", 3, 8 * 3600.0),
        ({"max_wall_s": 100.0}, "custom", 60, 100.0),
        (None, None, 60, 8 * 3600.0),
    ],
)
async def test_profile_limits_override_general(
    env, monkeypatch, profile_settings, submitted_profile, max_steps, max_wall_s
):
    monkeypatch.setattr(agent_worker_module, "AgentLoop", LimitsProbe)
    LimitsProbe.seen = []
    job_id = (
        await submit_profile(env, submitted_profile)
        if submitted_profile
        else await submit(env)
    )
    profiles = (
        {"custom": DevlabProfileConfig(**profile_settings)} if profile_settings else None
    )
    worker = make_worker(
        env,
        ScriptedChatModel(WRITE_DONE, FINISH),
        limits=AgentLimits(),
        profiles=profiles,
    )

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert LimitsProbe.seen[0].max_steps == max_steps
    assert LimitsProbe.seen[0].max_wall_s == max_wall_s


async def test_profile_quick_settings_reach_limits(env, monkeypatch):
    monkeypatch.setattr(agent_worker_module, "AgentLoop", LimitsProbe)
    LimitsProbe.seen = []
    await submit_profile(env, "custom")
    params = {"chat_template_kwargs": {"enable_thinking": False}}
    profiles = {
        "custom": DevlabProfileConfig(
            quick_steps=2, quick_params=params, params={"reasoning": {"enabled": True}}
        )
    }
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), profiles=profiles)

    assert await worker.claim_once()

    assert LimitsProbe.seen[0].quick_steps == 2
    assert LimitsProbe.seen[0].quick_params == params
    assert LimitsProbe.seen[0].params == {"reasoning": {"enabled": True}}


async def test_profile_role_text_joins_system_prompt(env):
    path = env.tmp_path / "role.md"
    path.write_text("роль-текст\n", encoding="utf-8")
    job_id = await submit_profile(env, "custom")
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(
        env, model, profiles={"custom": DevlabProfileConfig(prompt_file=str(path))}
    )

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    system, task = model.seen[0]
    assert system["content"].split("\n\n")[-1] == "роль-текст"
    assert task["content"] == "# сделай out.txt"


@pytest.mark.parametrize(
    ("profile", "write_prefixes", "code_mode", "prompt_lang", "prompt_style"),
    [("custom", ("docs/",), True, "en", "cb_v1"), (None, (), False, "ru", "cb_v3")],
)
async def test_profile_fields_reach_toolbox_spec(
    env, monkeypatch, profile, write_prefixes, code_mode, prompt_lang, prompt_style
):
    captured_spec = {}

    class CaptureLoop(AgentLoop):
        def __init__(self, model, tools, guard, limits, **kwargs) -> None:
            captured_spec["spec"] = tools._spec
            super().__init__(model, tools, guard, limits, **kwargs)

        async def run(self, system: str, task: str, resume: Dialog | None = None) -> AgentResult:
            return AgentResult(AgentStatus.FINISHED, summary="ok")

    job_id = await (submit_profile(env, profile) if profile else submit(env))
    model = ScriptedChatModel()
    worker = make_worker(
        env,
        model,
        profiles={
            "custom": DevlabProfileConfig(
                write_prefixes=["docs/"],
                code_mode=True,
                prompt_lang="en",
                prompt_style="cb_v1",
            )
        },
    )
    styles = []
    worker._models = lambda name, jid, style, reminder: styles.append(style) or model
    monkeypatch.setattr(agent_worker_module, "AgentLoop", CaptureLoop)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert captured_spec["spec"].write_prefixes == write_prefixes
    assert captured_spec["spec"].code_mode is code_mode
    assert captured_spec["spec"].prompt_lang == prompt_lang
    assert captured_spec["spec"].prompt_style == prompt_style
    assert styles == [prompt_style]


async def test_profile_intro_and_reminder_files_replace_code_mode_texts(env):
    intro = env.tmp_path / "intro.md"
    intro.write_text("свой вводный\n", encoding="utf-8")
    reminder = env.tmp_path / "reminder.md"
    reminder.write_text("своё напоминание\n", encoding="utf-8")
    job_id = await submit_profile(env, "custom")
    model = ScriptedChatModel()
    worker = make_worker(
        env,
        model,
        profiles={
            "custom": DevlabProfileConfig(
                code_mode=True, intro_file=str(intro), reminder_file=str(reminder)
            )
        },
    )
    reminders = []
    worker._models = lambda name, jid, style, rem: reminders.append(rem) or model

    assert await worker.claim_once()

    assert await env.status(job_id) is not None
    assert reminders == ["своё напоминание"]
    assert model.seen[0][0]["content"].startswith("свой вводный")


@pytest.mark.parametrize("field", ["intro_file", "reminder_file"])
async def test_missing_intro_or_reminder_file_fails_job(env, field):
    job_id = await submit_profile(env, "custom")
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(
        env,
        model,
        profiles={"custom": DevlabProfileConfig(code_mode=True, **{field: "missing-text.md"})},
    )

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "missing-text.md" in failed.payload[protocol.PAYLOAD_TEXT]
    assert not model.seen


async def test_unreadable_profile_role_fails_job_with_profile_name(env):
    job_id = await submit_profile(env, "custom")
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(
        env, model, profiles={"custom": DevlabProfileConfig(prompt_file="missing-role.md")}
    )

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "custom" in failed.payload[protocol.PAYLOAD_TEXT]
    assert "missing-role.md" in failed.payload[protocol.PAYLOAD_TEXT]
    assert not model.seen


async def test_held_jobs_are_parked_ones(env):
    state = AgentState(
        parked={"p-1": ParkedEntry(job={}), "p-2": ParkedEntry(job={}, answered=True)}
    )
    path = env.tmp_path / "exec" / AGENT_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json())
    assert make_worker(env, ScriptedChatModel()).held_jobs == ["p-1", "p-2"]


async def test_held_jobs_include_resumed_job(env):
    await submit(env)
    job = await env.store.queue.claim("pc", [MODEL], [protocol.JOB_KIND_AGENT])
    state = AgentState(job=asdict(job), parked={"p-1": ParkedEntry(job={})})
    path = env.tmp_path / "exec" / AGENT_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json())
    assert make_worker(env, ScriptedChatModel()).held_jobs == [job.id, "p-1"]


async def test_huge_rules_file_fails_before_model_with_its_name(env):
    files = {**BASE_FILES, "RULES.md": ("правило\n" * 20000).encode()}
    await env.push(BASE, files)
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=PROJECT,
        model=MODEL,
        task="# сделай out.txt",
        base_branch=BASE,
    )
    job_id = data["job"]["id"]
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "RULES.md" in failed.payload[protocol.PAYLOAD_TEXT]
    assert not model.seen


def test_rules_hint_empty_and_filled():
    assert rules_hint([]) == ""
    assert "RULES.md: 5 симв." in rules_hint([("RULES.md", "abcde")])


def uploaded_steps(env, job_id) -> list[dict]:
    path = env.store.logs_root / job_id / protocol.LOG_FILE_STEPS
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_steps_journal_is_uploaded_after_done(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH))

    assert await worker.claim_once()

    entries = uploaded_steps(env, job_id)
    assert [e["step"] for e in entries if "step" in e] == [1, 2]
    assert entries[-1]["status"] == "finished"
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()
    tail = await env.mcp.call(protocol.TOOL_TEST_LOG, id=job_id, file=protocol.LOG_FILE_STEPS)
    assert '"write_file"' in tail["text"]


async def test_steps_journal_is_uploaded_after_model_failure(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, ModelError("шлюз лёг")))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_QUEUED
    entries = uploaded_steps(env, job_id)
    assert entries[-1]["status"] == "model_failed"
    assert entries[1]["calls"][0]["name"] == "write_file"


async def test_steps_journal_upload_failure_does_not_change_outcome(env, monkeypatch):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH))

    async def broken(self, job, name, data):
        raise HubRequestError("лог: ClientConnectorError")

    monkeypatch.setattr(ExecutorHub, "upload_log", broken)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE


def test_step_trace_keeps_tail_within_limit(tmp_path):
    trace = StepTrace(
        tmp_path / "steps.jsonl", max_bytes=2000, scrub=lambda text: text.replace("x", "*")
    )
    for number in range(200):
        trace.record({"step": number, "text": "xxxxxxxxxx"})
    data = trace.tail(10_000)
    assert data is not None and len(data) <= 2000
    lines = data.decode().splitlines()
    assert json.loads(lines[-1])["step"] == 199
    assert "xxx" not in data.decode()
    assert all(json.loads(line) for line in lines)
    assert len(trace.tail(100) or b"") <= 100


def test_step_trace_scrubs_values_before_serialization(tmp_path):
    secret = 'pa"ss\\word'
    trace = StepTrace(tmp_path / "steps.jsonl", scrub=lambda text: text.replace(secret, "***"))
    trace.record({"calls": [{"args": {"command": f"echo {secret}"}}], "n": 1})
    text = (tmp_path / "steps.jsonl").read_text()
    assert "pa" not in text.replace("***", "")
    assert json.loads(text)["calls"][0]["args"]["command"] == "echo ***"


def test_step_trace_oversized_entry_is_shortened_not_dropped(tmp_path):
    trace = StepTrace(tmp_path / "steps.jsonl", max_bytes=1000)
    trace.record({"step": 1, "text": "я" * 3000, "calls": [{"name": "a"}, {"name": "b"}]})
    path = tmp_path / "steps.jsonl"
    assert 0 < path.stat().st_size <= 500
    entry = json.loads(path.read_text())
    assert entry["step"] == 1
    assert entry["calls_dropped"] == 2
    assert "calls" not in entry


async def test_repeat_crane_does_not_extend_budget_and_resumes_cleanly(env):
    job_id = await submit(env)
    read = tool_reply("read_file", "r1", path="out.txt")
    model = ScriptedChatModel(read, read, read, read, read, WRITE_DONE, FINISH)
    worker = make_worker(env, model)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert "модель повторяет read_file out.txt" in question.payload[protocol.PAYLOAD_TEXT]
    state = load_model(AgentState, env.tmp_path / "exec" / AGENT_STATE_FILE)
    assert state.spend[job_id].extensions == 0

    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="продолжай")
    await worker.poll_once()
    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE


async def stop_running(env, worker: AgentWorker, job_id: str, running: asyncio.Task) -> None:
    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()
    assert await running


async def test_second_slot_skips_job_on_busy_base_until_released(env):
    slots = SlotRegistry()
    first_model = BlockingModel(WRITE_DONE)
    first = make_worker(env, first_model, slots=slots)
    second = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), work_dir="exec-1", slots=slots)
    first_id = await submit(env)
    second_id = await submit(env, push=False)
    running = await start_blocked(first, first_model)
    assert slots.busy_bases == [BASE]
    assert await second.claim_once() is False
    assert await env.status(second_id) == protocol.STATUS_QUEUED
    await stop_running(env, first, first_id, running)
    assert slots.busy_bases == []
    assert await second.claim_once()
    assert await env.status(second_id) == protocol.STATUS_DONE


async def test_second_slot_takes_job_on_other_base(env):
    slots = SlotRegistry()
    first_model, second_model = BlockingModel(WRITE_DONE), BlockingModel(WRITE_DONE)
    first = make_worker(env, first_model, slots=slots)
    second = make_worker(env, second_model, work_dir="exec-1", slots=slots)
    first_id = await submit(env)
    second_id = await submit(env, base="base/other")
    first_run = await start_blocked(first, first_model)
    second_run = await start_blocked(second, second_model)
    assert slots.busy_bases == sorted([BASE, "base/other"])
    await stop_running(env, first, first_id, first_run)
    await stop_running(env, second, second_id, second_run)
    assert slots.busy_bases == []


def test_slot_registry_counts_same_base_twice():
    slots = SlotRegistry()
    slots.occupy("b")
    slots.occupy("b")
    slots.release("b")
    assert slots.busy_bases == ["b"]
    slots.release("b")
    assert slots.busy_bases == []
