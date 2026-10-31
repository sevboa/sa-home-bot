from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor import report as reporting
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError
from sa_home_bot.devlab.executor.mirror import (
    ProjectMirror,
    check_project_name,
    remote_url,
)
from sa_home_bot.devlab.executor.runner import JUNIT_FILE, RunRequest, TestRunner
from sa_home_bot.devlab.executor.spec import SPEC_FILE, parse_spec
from sa_home_bot.devlab.hub.queue import Job
from sa_home_bot.utils.files import load_model, save_model

log = logging.getLogger(__name__)

TIMEOUT_EXIT_CODE = 124
STATE_FILE = "state.json"
MIRRORS_DIR = "mirrors"
JOBS_DIR = "jobs"
PYTEST_WORD = "pytest"
JUNIT_FLAG = f"--junitxml={JUNIT_FILE}"
FAILED_TEXT_CHARS = 1000


class ExecutorState(BaseModel):
    cursor: int = 0
    current: str | None = None


@dataclass(frozen=True)
class WorkerSettings:
    hub_url: str
    token: str
    work_dir: Path
    poll_interval_s: float
    heartbeat_interval_s: float


class TestWorker:
    __test__ = False

    def __init__(self, hub: ExecutorHub, runner: TestRunner, settings: WorkerSettings) -> None:
        self._hub = hub
        self._runner = runner
        self._settings = settings
        self._state_path = settings.work_dir / STATE_FILE
        self._state = load_model(ExecutorState, self._state_path)
        self._current: str | None = None
        self._stopped: set[str] = set()

    @property
    def cursor(self) -> int:
        return self._state.cursor

    def _save(self, **changes: int | str | None) -> None:
        self._state = self._state.model_copy(update=changes)
        save_model(self._state, self._state_path)

    def scrub(self, text: str) -> str:
        token = self._settings.token
        return text.replace(token, "***") if token else text

    async def recover(self) -> None:
        orphan = self._state.current
        if orphan is None:
            return
        try:
            await self._hub.post(
                orphan,
                protocol.MSG_FAILED,
                {protocol.PAYLOAD_TEXT: "исполнитель перезапущен во время прогона"},
            )
        except HubRequestError as exc:
            log.warning("Осиротевшее задание %s не закрыто: %s", orphan, self.scrub(str(exc)))
        self._save(current=None)

    async def beat(self) -> None:
        busy = self._current is not None
        jobs = [self._current] if busy else []
        await self._hub.heartbeat({protocol.FIELD_BUSY: busy, protocol.FIELD_JOBS: jobs})

    async def poll_once(self) -> None:
        messages, cursor = await self._hub.poll(self._state.cursor)
        for message in messages:
            if message.kind == protocol.MSG_STOP and message.job_id == self._current:
                self._stopped.add(message.job_id)
                await self._runner.cancel(message.job_id)
        if cursor != self._state.cursor:
            self._save(cursor=cursor)

    async def claim_once(self) -> bool:
        job = await self._hub.claim([protocol.JOB_KIND_TEST])
        if job is None:
            return False
        await self.handle(job)
        return True

    async def handle(self, job: Job) -> None:
        self._current = job.id
        self._save(current=job.id)
        workdir = self._settings.work_dir / JOBS_DIR / job.id
        ref = str(job.params.get(protocol.FIELD_REF, ""))
        mirror: ProjectMirror | None = None
        try:
            mirror = self._mirror(job.project)
            await self._run_job(job, ref, mirror, workdir)
        except Exception as exc:
            text = self.scrub(f"{type(exc).__name__}: {exc}")[:FAILED_TEXT_CHARS]
            log.warning("Задание %s не выполнено: %s", job.id, text)
            await self._post_quietly(job.id, protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: text})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if mirror is not None and ref.startswith(protocol.TEST_BRANCH_PREFIX):
            try:
                await mirror.delete_branch(ref)
            except Exception as exc:
                log.warning("Ветка %s на хабе не удалена: %s", ref, self.scrub(str(exc)))
        self._stopped.discard(job.id)
        self._current = None
        self._save(current=None)

    def _mirror(self, project: str) -> ProjectMirror:
        name = check_project_name(project)
        git_dir = self._settings.work_dir / MIRRORS_DIR / f"{name}{protocol.GIT_REPO_SUFFIX}"
        return ProjectMirror(
            git_dir, remote_url(self._settings.hub_url, name), self._settings.token
        )

    async def _run_job(self, job: Job, ref: str, mirror: ProjectMirror, workdir: Path) -> None:
        await mirror.fetch()
        sha = await mirror.resolve(ref)
        spec = parse_spec(await mirror.read_file(sha, SPEC_FILE)).test
        cmd = str(job.params.get(protocol.FIELD_CMD, "")).strip() or spec.cmd.strip()
        if not cmd:
            raise ValueError(f"не задана команда: ни в задании, ни в [test] cmd файла {SPEC_FILE}")
        extra = list(job.params.get(protocol.FIELD_ARGS) or spec.args)
        argv = [*shlex.split(cmd), *extra]
        if any(PYTEST_WORD in word for word in argv[:3]):
            argv.append(JUNIT_FLAG)
        await mirror.export(sha, workdir)
        if job.id in self._stopped:
            await self._hub.post(job.id, protocol.MSG_STOPPED, {})
            return
        started = time.monotonic()
        result = await self._runner.run(
            RunRequest(
                job_id=job.id,
                workdir=workdir,
                argv=tuple(argv),
                timeout_s=spec.timeout_s,
                image=spec.image,
                setup=tuple(spec.setup),
                setup_files=tuple(spec.setup_files),
                env=tuple(spec.env.items()),
            )
        )
        duration = time.monotonic() - started
        if job.id in self._stopped:
            await self._hub.post(job.id, protocol.MSG_STOPPED, {})
            return
        junit_path = workdir / JUNIT_FILE
        junit = junit_path.read_bytes() if junit_path.is_file() else None
        output = result.output[-protocol.LOG_MAX_BYTES :]
        parsed = reporting.build_report(output.decode("utf-8", errors="replace"), junit)
        await self._hub.upload_log(job.id, protocol.LOG_FILE_OUTPUT, output)
        if junit is not None:
            await self._hub.upload_log(
                job.id, protocol.LOG_FILE_JUNIT, junit[-protocol.LOG_MAX_BYTES :]
            )
        tail = reporting.tail_of(result.output)
        exit_code = result.exit_code
        if result.timed_out:
            exit_code = TIMEOUT_EXIT_CODE
            tail = f"[таймаут {spec.timeout_s:g} с]\n{tail}"
        await self._hub.post(
            job.id,
            protocol.MSG_TEST_DONE,
            {
                protocol.PAYLOAD_EXIT_CODE: exit_code,
                protocol.PAYLOAD_DURATION_S: round(duration, 3),
                protocol.PAYLOAD_SUMMARY: parsed.summary,
                protocol.PAYLOAD_FAILURES: parsed.failures,
                protocol.PAYLOAD_TAIL: tail,
            },
        )

    async def _post_quietly(self, job_id: str, kind: str, payload: dict) -> None:
        try:
            await self._hub.post(job_id, kind, payload)
        except HubRequestError as exc:
            log.warning(
                "Сообщение %s по заданию %s не доставлено: %s", kind, job_id, self.scrub(str(exc))
            )

    async def _forever(self, step, interval_s: float, what: str) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s: %s", what, self.scrub(f"{type(exc).__name__}: {exc}"))
            await asyncio.sleep(interval_s)

    async def _claim_loop(self) -> None:
        while True:
            claimed = False
            try:
                claimed = await self.claim_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Запрос задания: %s", self.scrub(f"{type(exc).__name__}: {exc}"))
            if not claimed:
                await asyncio.sleep(self._settings.poll_interval_s)

    async def run(self) -> None:
        await self.recover()
        tasks = [
            asyncio.create_task(
                self._forever(self.beat, self._settings.heartbeat_interval_s, "Heartbeat")
            ),
            asyncio.create_task(
                self._forever(self.poll_once, self._settings.poll_interval_s, "Опрос хаба")
            ),
            asyncio.create_task(self._claim_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
