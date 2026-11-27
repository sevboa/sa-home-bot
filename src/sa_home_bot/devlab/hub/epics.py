from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from sa_home_bot.db.connection import Database
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import repos
from sa_home_bot.devlab.hub.epic_plan import (
    PlanTask,
    check_plan,
    later_test_files,
    parse_red_tests,
    red_tests_note,
    tester_locked_files,
)
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.queue import (
    JOB_ID_ALPHABET,
    JOB_ID_LENGTH,
    PROJECT_NAME_RE,
    HubQueue,
    Job,
    _valid_base_branch,
)
from sa_home_bot.utils.clock import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS epics (
    id TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    project TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    finished TEXT
);
CREATE TABLE IF NOT EXISTS epic_tasks (
    epic_id TEXT NOT NULL REFERENCES epics(id),
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    profile TEXT NOT NULL,
    paths TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    job_id TEXT,
    outcome TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    marked TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (epic_id, idx)
);
"""

OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_STOPPED = "stopped"
EPIC_ID_ATTEMPTS = 20


@dataclass(frozen=True)
class EpicTask:
    idx: int
    title: str
    task: str
    model: str
    profile: str
    paths: list[str]
    depends_on: list[int]
    job_id: str | None
    outcome: str
    commit: str
    marked: str | None
    failures: int


@dataclass(frozen=True)
class Epic:
    id: str
    client: str
    project: str
    base_branch: str
    branch: str
    status: str
    question: str
    created: str
    finished: str | None


def _epic(row: aiosqlite.Row) -> Epic:
    return Epic(**{key: row[key] for key in row.keys()})


def _epic_task(row: aiosqlite.Row) -> EpicTask:
    return EpicTask(
        idx=row["idx"],
        title=row["title"],
        task=row["task"],
        model=row["model"],
        profile=row["profile"],
        paths=json.loads(row["paths"]),
        depends_on=json.loads(row["depends_on"]),
        job_id=row["job_id"],
        outcome=row["outcome"],
        commit=row["commit_sha"],
        marked=row["marked"],
        failures=row["failures"],
    )


def _str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse_tasks(raw: Sequence[Any]) -> list[PlanTask]:
    reasons: list[str] = []
    tasks: list[PlanTask] = []
    if not raw:
        raise HubError("эпик без задач")
    if len(raw) > protocol.EPIC_MAX_TASKS:
        raise HubError(f"в эпике больше {protocol.EPIC_MAX_TASKS} задач")
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            reasons.append(f"задача {i}: ожидался объект")
            continue
        unknown = sorted(set(item) - set(protocol.EPIC_TASK_KEYS))
        if unknown:
            reasons.append(f"задача {i}: неизвестные поля {', '.join(unknown)}")
        title, task, model = item.get("title", ""), item.get("task", ""), item.get("model", "")
        profile = item.get("profile", protocol.DEFAULT_PROFILE)
        paths, deps = item.get("paths", []), item.get("depends_on", [])
        if not all(isinstance(v, str) for v in (title, task, model, profile)):
            reasons.append(f"задача {i}: title, task, model, profile — строки")
            continue
        if not task.strip() or not model.strip() or not title.strip():
            reasons.append(f"задача {i}: нужны title, task и model")
        if not PROJECT_NAME_RE.match(profile.strip()):
            reasons.append(f"задача {i}: профиль {profile!r} недопустим")
        if not _str_list(paths):
            reasons.append(f"задача {i}: paths — список строк")
            continue
        if not isinstance(deps, list) or not all(
            isinstance(d, int) and not isinstance(d, bool) for d in deps
        ):
            reasons.append(f"задача {i}: depends_on — список целых индексов")
            continue
        tasks.append(
            PlanTask(
                title.strip(),
                task,
                model.strip(),
                profile.strip(),
                tuple(p.strip() for p in paths),
                tuple(deps),
            )
        )
    if reasons:
        raise HubError("план отклонён:\n- " + "\n- ".join(reasons))
    return tasks


class HubEpics:
    def __init__(self, db: Database, queue: HubQueue, git_root: Path) -> None:
        self._db = db
        self._queue = queue
        self._git_root = git_root
        self._lock = asyncio.Lock()
        queue.observers.append(self.sync)

    async def submit(
        self, client: str, project: str, base_branch: str, raw_tasks: Sequence[Any]
    ) -> dict[str, Any]:
        tasks = parse_tasks(raw_tasks)
        if not await self._queue.has_project(project):
            raise HubError(f"проекта {project!r} нет")
        if not _valid_base_branch(base_branch):
            raise HubError(
                f"базовая ветка: {protocol.BASE_BRANCH_PREFIX}<имя>, "
                f"{protocol.AGENT_BRANCH_PREFIX}<id> или {protocol.EPIC_BRANCH_PREFIX}<id>"
            )
        if not await repos.branch_exists(self._git_root, project, base_branch):
            raise HubError(f"в хабе нет ветки {base_branch!r}")
        existing = await repos.tests_listing(self._git_root, project, base_branch)
        reasons = check_plan(tasks, existing)
        if reasons:
            raise HubError("план отклонён:\n- " + "\n- ".join(reasons))
        async with self._lock:
            epic_id = await self._free_id()
            branch = protocol.EPIC_BRANCH_PREFIX + epic_id
            await repos.create_branch(self._git_root, project, branch, base_branch)
            try:
                async with self._db.transaction() as conn:
                    await conn.execute(
                        "INSERT INTO epics (id, client, project, base_branch, branch, status,"
                        " created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            epic_id,
                            client,
                            project,
                            base_branch,
                            branch,
                            protocol.EPIC_RUNNING,
                            utc_now().isoformat(),
                        ),
                    )
                    for i, t in enumerate(tasks):
                        await conn.execute(
                            "INSERT INTO epic_tasks (epic_id, idx, title, task, model, profile,"
                            " paths, depends_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                epic_id,
                                i,
                                t.title,
                                t.task,
                                t.model,
                                t.profile,
                                json.dumps(list(t.paths), ensure_ascii=False),
                                json.dumps(list(t.depends_on)),
                            ),
                        )
            except BaseException:
                await repos.delete_branch(self._git_root, project, branch)
                raise
            await self._advance(epic_id)
        return await self.view(client, epic_id)

    async def _free_id(self) -> str:
        for _ in range(EPIC_ID_ATTEMPTS):
            epic_id = "".join(secrets.choice(JOB_ID_ALPHABET) for _ in range(JOB_ID_LENGTH))
            cur = await self._db.conn.execute("SELECT 1 FROM epics WHERE id = ?", (epic_id,))
            if not await cur.fetchone():
                return epic_id
        raise HubError("не удалось подобрать свободный id эпика")

    async def _load(self, epic_id: str) -> Epic:
        cur = await self._db.conn.execute("SELECT * FROM epics WHERE id = ?", (epic_id,))
        row = await cur.fetchone()
        if row is None:
            raise HubError(f"эпика {epic_id!r} нет")
        return _epic(row)

    async def _tasks(self, epic_id: str) -> list[EpicTask]:
        cur = await self._db.conn.execute(
            "SELECT * FROM epic_tasks WHERE epic_id = ? ORDER BY idx", (epic_id,)
        )
        return [_epic_task(row) for row in await cur.fetchall()]

    async def view(self, client: str, epic_id: str) -> dict[str, Any]:
        epic = await self._load(epic_id)
        if epic.client != client:
            raise HubError(f"эпика {epic_id!r} нет")
        tasks = await self._tasks(epic_id)
        statuses = await self._queue.job_statuses([t.job_id for t in tasks if t.job_id])
        current = next((t.idx for t in tasks if t.outcome != OUTCOME_DONE), None)
        return {
            "id": epic.id,
            "project": epic.project,
            "base_branch": epic.base_branch,
            "branch": epic.branch,
            "status": epic.status,
            "question": epic.question,
            "created": epic.created,
            "finished": epic.finished,
            "current": current,
            "tasks": [
                {
                    "index": t.idx,
                    "title": t.title,
                    "model": t.model,
                    "profile": t.profile,
                    "paths": t.paths,
                    "depends_on": t.depends_on,
                    "job_id": t.job_id,
                    "job_status": statuses.get(t.job_id or "", ""),
                    "outcome": t.outcome,
                    "commit": t.commit,
                    "marked": t.marked,
                    "failures": t.failures,
                }
                for t in tasks
            ],
        }

    async def task_totals(self, epic_ids: Collection[str]) -> dict[str, int]:
        if not epic_ids:
            return {}
        marks = ", ".join("?" for _ in epic_ids)
        cur = await self._db.conn.execute(
            f"SELECT epic_id, COUNT(*) AS total FROM epic_tasks WHERE epic_id IN ({marks})"
            " GROUP BY epic_id",
            tuple(epic_ids),
        )
        return {row["epic_id"]: row["total"] for row in await cur.fetchall()}

    async def active_summaries(self) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT id FROM epics WHERE status IN (?, ?) ORDER BY created",
            (protocol.EPIC_RUNNING, protocol.EPIC_WAITING_OWNER),
        )
        summaries = []
        for row in await cur.fetchall():
            epic = await self._load(row["id"])
            tasks = await self._tasks(epic.id)
            current = next((t for t in tasks if t.outcome != OUTCOME_DONE), None)
            summaries.append(
                {
                    protocol.FIELD_ID: epic.id,
                    protocol.FIELD_TITLE: tasks[0].title if tasks else "",
                    "project": epic.project,
                    protocol.FIELD_STATUS: epic.status,
                    protocol.FIELD_QUESTION: epic.question,
                    "done": sum(1 for t in tasks if t.outcome == OUTCOME_DONE),
                    "total": len(tasks),
                    "current_title": current.title if current else "",
                    "current_job": current.job_id if current else None,
                }
            )
        return summaries

    async def resume(self, epic_id: str) -> dict[str, Any]:
        async with self._lock:
            epic = await self._load(epic_id)
            if epic.status == protocol.EPIC_DONE:
                raise HubError(f"эпик {epic_id!r} уже завершён")
            tasks = await self._tasks(epic_id)
            current = next((t for t in tasks if t.outcome != OUTCOME_DONE), None)
            if current is not None and current.job_id is not None:
                job = await self._queue.get_job(current.job_id)
                if job.status in (protocol.STATUS_FAILED, protocol.STATUS_STOPPED):
                    await self._queue.post_from_client(
                        job.client,
                        job.id,
                        protocol.MSG_RESUME,
                        {protocol.PAYLOAD_TEXT: "Продолжение по слову владельца."},
                        observe=False,
                    )
                await self._update_task(epic_id, current.idx, outcome="", marked=None, failures=0)
            await self._set_epic(epic_id, protocol.EPIC_RUNNING)
            await self._advance(epic_id)
            status = (await self._load(epic_id)).status
            return {protocol.FIELD_ID: epic_id, protocol.FIELD_STATUS: status}

    async def stop(self, epic_id: str) -> dict[str, Any]:
        async with self._lock:
            epic = await self._load(epic_id)
            if epic.status == protocol.EPIC_DONE:
                raise HubError(f"эпик {epic_id!r} уже завершён")
            await self._set_epic(epic_id, protocol.EPIC_STOPPED)
            await self._stop_current(epic_id)
            return {protocol.FIELD_ID: epic_id, protocol.FIELD_STATUS: protocol.EPIC_STOPPED}

    async def delete(self, epic_id: str) -> dict[str, Any]:
        async with self._lock:
            epic = await self._load(epic_id)
            await self._stop_current(epic_id)
            async with self._db.transaction() as conn:
                await conn.execute("DELETE FROM epic_tasks WHERE epic_id = ?", (epic_id,))
                await conn.execute("DELETE FROM epics WHERE id = ?", (epic_id,))
            await repos.delete_branch(self._git_root, epic.project, epic.branch)
            return {protocol.FIELD_ID: epic_id, protocol.FIELD_STATUS: protocol.EPIC_DELETED}

    async def _stop_current(self, epic_id: str) -> None:
        tasks = await self._tasks(epic_id)
        current = next((t for t in tasks if t.outcome != OUTCOME_DONE), None)
        if current is None or current.job_id is None:
            return
        job = await self._queue.get_job(current.job_id)
        if job.status in (
            protocol.STATUS_QUEUED,
            protocol.STATUS_RUNNING,
            protocol.STATUS_WAITING_OWNER,
        ):
            await self._queue.post_from_client(job.client, job.id, protocol.MSG_STOP, observe=False)

    async def sync(self, job_id: str) -> None:
        cur = await self._db.conn.execute(
            "SELECT epic_id FROM epic_tasks WHERE job_id = ?", (job_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return
        async with self._lock:
            await self._advance(row["epic_id"])

    async def resume_all(self) -> None:
        cur = await self._db.conn.execute(
            "SELECT id FROM epics WHERE status NOT IN (?, ?)",
            (protocol.EPIC_DONE, protocol.EPIC_STOPPED),
        )
        async with self._lock:
            for row in await cur.fetchall():
                await self._advance(row["id"])

    async def _set_epic(
        self, epic_id: str, status: str, question: str = "", finished: str | None = None
    ) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE epics SET status = ?, question = ?, finished = ? WHERE id = ?",
                (status, question, finished, epic_id),
            )

    async def _update_task(self, epic_id: str, idx: int, **fields: Any) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        async with self._db.transaction() as conn:
            await conn.execute(
                f"UPDATE epic_tasks SET {columns} WHERE epic_id = ? AND idx = ?",
                (*fields.values(), epic_id, idx),
            )

    async def _red_tests_before(self, task: EpicTask, tasks: Sequence[EpicTask]) -> list[str]:
        red: list[str] = []
        for earlier in (tasks[i] for i in task.depends_on):
            if earlier.profile != protocol.PROFILE_TESTER or earlier.job_id is None:
                continue
            message = await self._queue.last_message(earlier.job_id, protocol.MSG_DONE)
            summary = message.payload.get(protocol.PAYLOAD_SUMMARY) if message else None
            if isinstance(summary, str):
                red += parse_red_tests(summary)
        return list(dict.fromkeys(red))

    async def _enqueue(self, epic: Epic, task: EpicTask, tasks: Sequence[EpicTask]) -> None:
        is_tester = task.profile == protocol.PROFILE_TESTER
        red = [] if is_tester else await self._red_tests_before(task, tasks)
        async with self._db.transaction() as conn:
            job = await HubQueue._insert_agent_job(
                conn,
                epic.client,
                epic.project,
                task.model,
                task.task + red_tests_note(red),
                epic.branch,
                0,
                task.profile,
                protocol.PURPOSE_DEV,
                extra_params={
                    protocol.PARAM_EPIC: epic.id,
                    protocol.PARAM_EPIC_INDEX: task.idx,
                    protocol.PARAM_PATHS: task.paths,
                    protocol.PARAM_EPIC_SKIP_TESTS: later_test_files(tasks, task.idx),
                    protocol.PARAM_EPIC_LOCKED: tester_locked_files(tasks, task.idx),
                    protocol.PARAM_EPIC_RED_TESTS: red,
                },
            )
            await conn.execute(
                "UPDATE epic_tasks SET job_id = ? WHERE epic_id = ? AND idx = ?",
                (job.id, epic.id, task.idx),
            )

    async def _wait(self, epic: Epic, job: Job, question: str) -> None:
        if epic.status == protocol.EPIC_WAITING_OWNER and epic.question == question:
            return
        await self._set_epic(epic.id, protocol.EPIC_WAITING_OWNER, question)
        await self._queue.notify_client(
            job.id,
            protocol.MSG_EPIC_WAITING,
            {protocol.PAYLOAD_TEXT: question, protocol.PARAM_EPIC: epic.id},
        )

    async def _advance(self, epic_id: str) -> None:
        while True:
            epic = await self._load(epic_id)
            if epic.status == protocol.EPIC_DONE:
                return
            tasks = await self._tasks(epic_id)
            current = next((t for t in tasks if t.outcome != OUTCOME_DONE), None)
            if epic.status == protocol.EPIC_STOPPED:
                return
            if current is None:
                await self._set_epic(epic_id, protocol.EPIC_DONE, finished=utc_now().isoformat())
                return
            if current.job_id is None:
                await self._enqueue(epic, current, tasks)
                return
            job = await self._queue.get_job(current.job_id)
            if job.status == protocol.STATUS_DONE:
                if not await self._merge(epic, current, job):
                    return
                continue
            await self._react(epic, current, job)
            return

    async def _merge(self, epic: Epic, task: EpicTask, job: Job) -> bool:
        source = protocol.AGENT_BRANCH_PREFIX + job.id
        try:
            sha = await repos.merge_branch(
                self._git_root,
                epic.project,
                epic.branch,
                source,
                f"epic {epic.id}: задача {task.idx} ({task.title}), задание {job.id}",
            )
        except HubError as exc:
            await self._wait(
                epic,
                job,
                f"Эпик {epic.id}, задача {task.idx}: слить {source} в {epic.branch} не "
                f"вышло ({exc}). Исправь ветку и верни задание в очередь или останови.",
            )
            return False
        await self._update_task(
            epic.id,
            task.idx,
            outcome=OUTCOME_DONE,
            commit_sha=sha,
            marked=utc_now().isoformat(),
        )
        if epic.status != protocol.EPIC_RUNNING or epic.question:
            await self._set_epic(epic.id, protocol.EPIC_RUNNING)
        return True

    async def _react(self, epic: Epic, task: EpicTask, job: Job) -> None:
        if job.status in (
            protocol.STATUS_QUEUED,
            protocol.STATUS_RUNNING,
            protocol.STATUS_WAITING_OWNER,
        ):
            if task.outcome:
                await self._update_task(epic.id, task.idx, outcome="", marked=None)
            if epic.status == protocol.EPIC_WAITING_OWNER:
                await self._update_task(epic.id, task.idx, failures=0)
                await self._set_epic(epic.id, protocol.EPIC_RUNNING)
            return
        outcome = OUTCOME_FAILED if job.status == protocol.STATUS_FAILED else OUTCOME_STOPPED
        failures = task.failures
        if task.outcome != outcome:
            failures += 1 if outcome == OUTCOME_FAILED else 0
            await self._update_task(
                epic.id,
                task.idx,
                outcome=outcome,
                failures=failures,
                marked=utc_now().isoformat(),
            )
        number = f"Эпик {epic.id}, задача {task.idx} ({task.title}), задание {job.id}"
        if outcome == OUTCOME_STOPPED:
            await self._wait(
                epic, job, f"{number}: остановлено. Верни в очередь (devlab_resume) или правь план."
            )
        elif failures < protocol.EPIC_MAX_TASK_FAILURES:
            await self._queue.post_from_client(
                job.client,
                job.id,
                protocol.MSG_RESUME,
                {protocol.PAYLOAD_TEXT: "Повтор после провала: прошлая попытка не удалась."},
                observe=False,
            )
            await self._update_task(epic.id, task.idx, outcome="", marked=None)
        else:
            await self._wait(
                epic,
                job,
                f"{number}: провал {failures} раз подряд. Остальные задачи не стартуют: "
                "смени модель или поправь задачу и верни задание (devlab_resume).",
            )
