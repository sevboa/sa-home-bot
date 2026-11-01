from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import AgentLimits, AgentLoop, AgentResult, AgentStatus, Crane
from sa_home_bot.devlab.agent.model import ModelFactory
from sa_home_bot.devlab.agent.prompt import build_system, build_task
from sa_home_bot.devlab.agent.repo import PUSH_INTERVAL_S, AgentRepo, agent_author
from sa_home_bot.devlab.agent.sandbox import LocalSandbox, ManagedSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.spend import JobSpend, MeteredChat, SpendState
from sa_home_bot.devlab.agent.tools import ToolBox
from sa_home_bot.devlab.executor.agent_sandbox import SandboxFactory, SandboxRequest
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError
from sa_home_bot.devlab.executor.mirror import (
    GitError,
    ProjectMirror,
    check_project_name,
    remote_url,
)
from sa_home_bot.devlab.executor.routing import Catalog
from sa_home_bot.devlab.executor.spec import SPEC_FILE, ProjectSpec, parse_spec
from sa_home_bot.devlab.executor.worker import WorkerLoops, WorkerSettings
from sa_home_bot.devlab.hub.queue import Job, Message
from sa_home_bot.utils.files import load_model, save_model

log = logging.getLogger(__name__)

T = TypeVar("T")

AGENT_STATE_FILE = "agent-state.json"
OUTBOX_FILE = "agent-outbox.json"
DIALOG_FILE = "dialog.json"
AGENT_DIR = "agent"
MIRROR_DIR = "mirror.git"
WORK_DIR = "work"
GIT_DIR = "git"
BASE_DIR = "base"
RETRY_INTERVAL_S = 5.0
ABANDON_ATTEMPTS = 3
FAILED_TEXT_CHARS = 1000
STOPPED_REASON = "остановлено владельцем"
REMOTE_FORBIDDEN_REASON = (
    "модель {model!r} внешняя, а проект запрещает внешние модели (allow_remote_models = false)"
)
MODEL_MISSING_REASON = "модели {model!r} нет на живых шлюзах"
SPEND_CRANES = (Crane.COST, Crane.TOKENS)
LOST_DIALOG_REASON = "диалог агента потерян, ответ владельца некуда вернуть"
FAILURE_PREFIX = {
    AgentStatus.MODEL_FAILED: "модель",
    AgentStatus.CONTEXT_OVERFLOW: "контекст",
    AgentStatus.STUCK: "агент застрял",
}


class ModelUnavailable(Exception):
    pass


class ParkedEntry(BaseModel):
    job: dict[str, Any]
    answered: bool = False


class AgentState(BaseModel):
    cursor: int = 0
    job: dict[str, Any] | None = None
    parked: dict[str, ParkedEntry] = Field(default_factory=dict)
    spend: dict[str, SpendState] = Field(default_factory=dict)


class OutboxItem(BaseModel):
    job_id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Outbox(BaseModel):
    items: list[OutboxItem] = Field(default_factory=list)


@dataclass(frozen=True)
class AgentSettings(WorkerSettings):
    limits: AgentLimits = AgentLimits()
    push_interval_s: float = PUSH_INTERVAL_S
    retry_interval_s: float = RETRY_INTERVAL_S


@dataclass
class JobRun:
    job: Job
    root: Path
    mirror: ProjectMirror
    repo: AgentRepo | None = None
    sandbox: ManagedSandbox | None = None
    parked: bool = False


class AgentWorker(WorkerLoops):
    def __init__(
        self,
        hub: ExecutorHub,
        models: ModelFactory,
        sandboxes: SandboxFactory,
        settings: AgentSettings,
        catalog: Catalog,
    ) -> None:
        super().__init__(hub, settings)
        self._settings: AgentSettings = settings
        self._models = models
        self._sandboxes = sandboxes
        self._catalog = catalog
        self._spends: dict[str, JobSpend] = {}
        self._state_path = settings.work_dir / AGENT_STATE_FILE
        self._outbox_path = settings.work_dir / OUTBOX_FILE
        self._state = load_model(AgentState, self._state_path)
        self._outbox = load_model(Outbox, self._outbox_path)
        self._resume = Job(**self._state.job) if self._state.job else None
        self._current = self._resume.id if self._resume else None
        self._task: asyncio.Task[None] | None = None
        self._stopped: set[str] = set()
        self._parked_sandboxes: dict[str, ManagedSandbox] = {}
        self._drain_lock = asyncio.Lock()

    @property
    def cursor(self) -> int:
        return self._state.cursor

    @property
    def parked_jobs(self) -> list[str]:
        return list(self._state.parked)

    @property
    def pending_messages(self) -> list[OutboxItem]:
        return list(self._outbox.items)

    def _save(self, **changes: Any) -> None:
        self._state = self._state.model_copy(update=changes)
        save_model(self._state, self._state_path)

    def _save_outbox(self) -> None:
        save_model(self._outbox, self._outbox_path)

    async def send(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._outbox.items.append(OutboxItem(job_id=job_id, kind=kind, payload=payload))
        self._save_outbox()
        await self.drain()

    async def drain(self) -> None:
        async with self._drain_lock:
            while self._outbox.items:
                item = self._outbox.items[0]
                try:
                    await self._hub.post(item.job_id, item.kind, item.payload)
                except HubRequestError as exc:
                    text = self.scrub(str(exc))
                    if not exc.permanent:
                        log.info(
                            "Сообщение %s по заданию %s задержано: %s", item.kind, item.job_id, text
                        )
                        return
                    log.warning(
                        "Сообщение %s по заданию %s отвергнуто: %s", item.kind, item.job_id, text
                    )
                self._outbox.items.pop(0)
                self._save_outbox()

    async def poll_once(self) -> None:
        messages, cursor = await self._hub.poll(self._state.cursor)
        for message in messages:
            await self._on_message(message)
        if cursor != self._state.cursor:
            self._save(cursor=cursor)

    async def _on_message(self, message: Message) -> None:
        job_id = message.job_id
        text = message.payload.get(protocol.PAYLOAD_TEXT)
        text = text if isinstance(text, str) else ""
        if message.kind == protocol.MSG_STOP:
            if job_id == self._current:
                self._stopped.add(job_id)
                if self._task is not None:
                    self._task.cancel()
            elif job_id in self._state.parked:
                await self._close_parked(job_id, protocol.MSG_STOPPED, STOPPED_REASON)
        elif message.kind == protocol.MSG_ANSWER and job_id in self._state.parked:
            await self._accept_answer(job_id, text)
        elif message.kind == protocol.MSG_RESUME:
            if job_id in self._state.parked:
                await self._forget(job_id)

    def _job_root(self, job_id: str) -> Path:
        return self._settings.work_dir / AGENT_DIR / job_id

    async def _accept_answer(self, job_id: str, text: str) -> None:
        path = self._job_root(job_id) / DIALOG_FILE
        if not path.exists():
            await self._close_parked(job_id, protocol.MSG_FAILED, LOST_DIALOG_REASON)
            return
        dialog = load_model(Dialog, path)
        save_model(dialog.model_copy(update={"answer": text}), path)
        entry = self._state.parked[job_id].model_copy(update={"answered": True})
        self._save(parked={**self._state.parked, job_id: entry})

    def _without_parked(self, job_id: str) -> dict[str, ParkedEntry]:
        return {key: value for key, value in self._state.parked.items() if key != job_id}

    def _spend_of(self, job: Job) -> JobSpend:
        spend = self._spends.get(job.id)
        if spend is None:
            state = self._state.spend.get(job.id) or SpendState(
                base_cost=job.cost_usd, base_tokens=job.tokens
            )
            spend = JobSpend(state, lambda saved: self._save_spend(job.id, saved))
            self._spends[job.id] = spend
        return spend

    def _save_spend(self, job_id: str, state: SpendState) -> None:
        self._save(spend={**self._state.spend, job_id: state})

    def _drop_spend(self, job_id: str) -> None:
        self._spends.pop(job_id, None)
        if job_id in self._state.spend:
            self._save(spend={k: v for k, v in self._state.spend.items() if k != job_id})

    async def _forget(self, job_id: str) -> None:
        self._drop_spend(job_id)
        self._save(parked=self._without_parked(job_id))
        sandbox = self._parked_sandboxes.pop(job_id, None)
        if sandbox is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(sandbox.aclose())
        shutil.rmtree(self._job_root(job_id), ignore_errors=True)

    async def _close_parked(self, job_id: str, kind: str, text: str) -> None:
        job = Job(**self._state.parked[job_id].job)
        self._save(parked=self._without_parked(job_id))
        root = self._job_root(job_id)
        run = JobRun(job, root, self._mirror(job, root))
        run.repo = self._make_repo(run)
        try:
            await run.repo.open()
            await self._abandon(run, text)
        except (GitError, HubRequestError, OSError) as exc:
            log.warning("wip задания %s не закрыта: %s", job_id, self.scrub(str(exc)))
        finally:
            await run.repo.aclose()
        await self._forget(job_id)
        payload = {protocol.PAYLOAD_TEXT: text} if kind == protocol.MSG_FAILED else {}
        await self.send(job_id, kind, payload)

    async def claim_once(self) -> bool:
        resumed, self._resume = self._resume, None
        if resumed is None:
            ready = next((key for key, entry in self._state.parked.items() if entry.answered), None)
            if ready is not None:
                resumed = Job(**self._state.parked[ready].job)
        available: list[str] = []
        if resumed is None:
            try:
                available = await self._catalog.names()
            except HubRequestError as exc:
                log.info("Каталог моделей недоступен: %s", self.scrub(str(exc)))
                return False
        job = resumed or await self._hub.claim([protocol.JOB_KIND_AGENT], available)
        if job is None:
            return False
        await self.handle(job, resumed=resumed is not None)
        return True

    async def handle(self, job: Job, resumed: bool = False) -> None:
        self._current = job.id
        self._save(job=asdict(job), parked=self._without_parked(job.id))
        root = self._job_root(job.id)
        if not resumed:
            await self._forget(job.id)
            await self.send(job.id, protocol.MSG_STARTED, {protocol.PAYLOAD_MODEL: job.model})
        if job.id in self._stopped:
            await self.send(job.id, protocol.MSG_STOPPED, {})
        else:
            self._task = asyncio.create_task(self._work(job, root))
            try:
                await self._task
            finally:
                self._task = None
        if job.id not in self._state.parked:
            shutil.rmtree(root, ignore_errors=True)
            self._drop_spend(job.id)
        self._stopped.discard(job.id)
        self._current = None
        self._save(job=None)

    def _mirror(self, job: Job, root: Path) -> ProjectMirror:
        name = check_project_name(job.project)
        return ProjectMirror(
            root / MIRROR_DIR, remote_url(self._settings.hub_url, name), self._settings.token
        )

    async def _work(self, job: Job, root: Path) -> None:
        run = JobRun(job, root, self._mirror(job, root))
        try:
            result = await self._execute(run)
            await self._conclude(run, result)
        except asyncio.CancelledError:
            if job.id not in self._stopped:
                await asyncio.shield(self._suspend(run))
                raise
            await self._fail_or_stop(run, protocol.MSG_STOPPED, STOPPED_REASON)
        except ModelUnavailable as exc:
            await self._fail_or_stop(run, protocol.MSG_FAILED, str(exc))
        except Exception as exc:
            text = self.scrub(f"{type(exc).__name__}: {exc}")[:FAILED_TEXT_CHARS]
            log.warning("Задание %s не выполнено: %s", job.id, text)
            await self._fail_or_stop(run, protocol.MSG_FAILED, text)
        finally:
            await self._release(run)

    def _make_repo(self, run: JobRun) -> AgentRepo:
        job = run.job
        return AgentRepo(
            run.mirror,
            run.root / WORK_DIR,
            run.root / GIT_DIR,
            job.id,
            agent_author(job.model),
            push_interval_s=self._settings.push_interval_s,
            base_branch=job.base_branch,
        )

    async def _execute(self, run: JobRun) -> AgentResult:
        job = run.job
        repo = self._make_repo(run)
        run.repo = repo
        await repo.open()
        project = parse_spec(await run.mirror.read_file(repo.base, SPEC_FILE))
        spec = AgentSpec.from_project(project)
        await self._check_model(job, project)
        baseline = await self._baseline(run, repo.base)
        held = self._parked_sandboxes.pop(job.id, None)
        sandbox = held or self._sandboxes(
            SandboxRequest(job.id, repo.workdir, project, spec, baseline)
        )
        run.sandbox = sandbox
        await (sandbox.resume() if held else sandbox.start())
        spend = self._spend_of(job)
        loop = AgentLoop(
            MeteredChat(self._models(job.model, job.id), spend),
            ToolBox(sandbox, spec),
            FinishGuard(sandbox, spec),
            self._limits_for(spend),
            on_change=repo.on_change,
            on_step=lambda title: self._step_done(run, title),
            spend=spend.total_cost,
            tokens=spend.total_tokens,
        )
        system = build_system(await self._rules(run, project, repo.base))
        dialog = self._answered_dialog(run)
        if dialog is not None:
            return await loop.run(system, job.task, resume=dialog)
        task = build_task(
            job.task,
            await sandbox.changed_paths(),
            await repo.milestones(),
            job.params.get(protocol.PARAM_REMARKS, ""),
        )
        return await loop.run(system, task)

    def _answered_dialog(self, run: JobRun) -> Dialog | None:
        path = run.root / DIALOG_FILE
        if not path.exists():
            return None
        dialog = load_model(Dialog, path)
        return dialog if dialog.answer is not None else None

    def _limits_for(self, spend: JobSpend) -> AgentLimits:
        limits = self._settings.limits
        factor = 1 + spend.extensions
        return replace(
            limits,
            max_cost_usd=limits.max_cost_usd * factor,
            max_tokens=limits.max_tokens * factor,
        )

    async def _check_model(self, job: Job, project: ProjectSpec) -> None:
        entries = await self._catalog.entries(job.model)
        if not entries:
            raise ModelUnavailable(MODEL_MISSING_REASON.format(model=job.model))
        if not project.agent.allow_remote_models and any(e.remote for e in entries):
            raise ModelUnavailable(REMOTE_FORBIDDEN_REASON.format(model=job.model))

    async def _step_done(self, run: JobRun, title: str) -> None:
        assert run.repo is not None
        await self._retry(lambda: run.repo.milestone(title), ABANDON_ATTEMPTS)
        report = self._spend_of(run.job).take_report()
        await self.send(
            run.job.id, protocol.MSG_STEP_DONE, {protocol.PAYLOAD_TEXT: title, **report}
        )

    async def _baseline(self, run: JobRun, base: str) -> dict[str, str]:
        target = run.root / BASE_DIR
        shutil.rmtree(target, ignore_errors=True)
        await run.mirror.export(base, target)
        snapshot = await asyncio.to_thread(LocalSandbox(target).snapshot)
        shutil.rmtree(target, ignore_errors=True)
        return snapshot

    async def _rules(self, run: JobRun, project: ProjectSpec, base: str) -> list[tuple[str, str]]:
        rules = []
        for name in project.agent.rules:
            data = await run.mirror.read_file(base, name)
            if data is not None:
                rules.append((name, data.decode("utf-8", errors="replace")))
        return rules

    async def _conclude(self, run: JobRun, result: AgentResult) -> None:
        job = run.job
        assert run.repo is not None
        spend = self._spend_of(job)
        usage = {**spend.take_report(), protocol.PAYLOAD_MODEL: job.model}
        if result.status is AgentStatus.ASKED:
            assert result.dialog is not None
            if result.crane in SPEND_CRANES:
                spend.extend()
            await self._park(run, result.dialog, usage)
            return
        if result.status is AgentStatus.FINISHED:
            await self._retry(lambda: run.repo.finish(result.summary), None)
            await self.send(
                job.id, protocol.MSG_DONE, {protocol.PAYLOAD_SUMMARY: result.summary, **usage}
            )
            return
        reason = f"{FAILURE_PREFIX[result.status]}: {result.reason}"
        await self._abandon(run, reason)
        await self.send(job.id, protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: reason, **usage})

    async def _park(self, run: JobRun, dialog: Dialog, usage: dict[str, Any]) -> None:
        job = run.job
        assert run.repo is not None and run.sandbox is not None
        save_model(dialog, run.root / DIALOG_FILE)
        try:
            await run.sandbox.stop()
        except Exception as exc:
            log.warning("Контейнер задания %s не остановлен: %s", job.id, self.scrub(str(exc)))
        await self._retry(run.repo.flush, None)
        self._parked_sandboxes[job.id] = run.sandbox
        run.parked = True
        parked = {**self._state.parked, job.id: ParkedEntry(job=asdict(job))}
        self._save(job=None, parked=parked)
        await self.send(
            job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: dialog.question, **usage}
        )

    async def _fail_or_stop(self, run: JobRun, kind: str, text: str) -> None:
        await self._abandon(run, text)
        payload = {protocol.PAYLOAD_TEXT: text} if kind == protocol.MSG_FAILED else {}
        payload.update(self._spend_of(run.job).take_report())
        await self.send(run.job.id, kind, payload)

    async def _abandon(self, run: JobRun, reason: str) -> None:
        repo = run.repo
        if repo is None:
            return
        try:
            await self._retry(lambda: repo.abandon(reason), ABANDON_ATTEMPTS)
        except (GitError, HubRequestError, OSError) as exc:
            log.warning("wip задания %s не закрыта: %s", run.job.id, self.scrub(str(exc)))

    async def _retry(self, operation: Callable[[], Awaitable[T]], attempts: int | None) -> T:
        tried = 0
        while True:
            tried += 1
            try:
                return await operation()
            except (GitError, HubRequestError, OSError) as exc:
                if attempts is not None and tried >= attempts:
                    raise
                log.warning(
                    "Повтор через %g с: %s", self._settings.retry_interval_s, self.scrub(str(exc))
                )
            await asyncio.sleep(self._settings.retry_interval_s)

    async def _suspend(self, run: JobRun) -> None:
        if run.repo is not None:
            with contextlib.suppress(Exception):
                await run.repo.flush()

    async def _release(self, run: JobRun) -> None:
        if run.repo is not None:
            await asyncio.shield(run.repo.aclose())
        if run.sandbox is not None and not run.parked:
            await asyncio.shield(run.sandbox.aclose())

    async def run(self, heartbeat: bool = True) -> None:
        await self.drain()
        await self.run_loops(extra=[(self.drain, "Исходящие сообщения")], heartbeat=heartbeat)
