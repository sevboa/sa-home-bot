from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import joblogs, repos
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.store import HubStore

RESOURCES_DIR = Path(__file__).parent / "resources"
MARKDOWN_MIME = "text/markdown"
JSON_TYPES: dict[str, type] = {"string": str, "integer": int, "array": list}


@dataclass(frozen=True)
class Context:
    store: HubStore
    client: str


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    description: str
    required: bool = False


Handler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: tuple[Param, ...]
    handler: Handler

    def schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        for param in self.params:
            spec: dict[str, Any] = {"type": param.type, "description": param.description}
            if param.type == "array":
                spec["items"] = {"type": "string"}
            properties[param.name] = spec
        return {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in self.params if p.required],
        }

    def check(self, arguments: dict[str, Any]) -> dict[str, Any]:
        known = {param.name: param for param in self.params}
        for name in arguments:
            if name not in known:
                raise HubError(f"{self.name}: неизвестный параметр {name!r}")
        for param in self.params:
            if param.required and param.name not in arguments:
                raise HubError(f"{self.name}: нужен параметр {param.name}")
        for name, value in arguments.items():
            expected = JSON_TYPES[known[name].type]
            bad = isinstance(value, bool) or not isinstance(value, expected)
            if known[name].type == "array":
                bad = not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            if bad:
                raise HubError(f"{self.name}: параметр {name} — {known[name].type}")
        return arguments


def job_view(job: Any, with_task: bool = True) -> dict[str, Any]:
    data = asdict(job)
    if not with_task:
        del data["task"]
    return data


async def model_warnings(ctx: Context, model: str) -> list[str]:
    names = {entry[protocol.MODEL_NAME] for entry in await ctx.store.models()}
    if model.strip() in names:
        return []
    return [
        f"Модели {model.strip()!r} сейчас нет в каталоге devlab_models:"
        " задание будет ждать в очереди,"
        " пока шлюз с этой моделью не станет онлайн."
    ]


async def models(ctx: Context) -> Any:
    return await ctx.store.models()


async def project_add(ctx: Context, name: str) -> Any:
    await ctx.store.add_project(name)
    name = name.strip()
    return {"name": name, "git_path": f"{protocol.HTTP_GIT_PREFIX}{name}{protocol.GIT_REPO_SUFFIX}"}


async def projects(ctx: Context) -> Any:
    return await ctx.store.queue.projects()


async def submit(
    ctx: Context,
    project: str,
    model: str,
    task: str,
    base_branch: str,
    priority: int = 0,
) -> Any:
    if await ctx.store.queue.has_project(project) and not await repos.branch_exists(
        ctx.store.git_root, project, base_branch
    ):
        raise HubError(
            f"в хабе нет ветки {base_branch!r}: запушь её, "
            f"git push {protocol.HTTP_GIT_PREFIX}{project}{protocol.GIT_REPO_SUFFIX} "
            f"HEAD:{base_branch}"
        )
    job = await ctx.store.queue.submit(
        ctx.client, project, model, task, priority=priority, base_branch=base_branch
    )
    return {"job": job_view(job, with_task=False), "warnings": await model_warnings(ctx, model)}


async def test_outcome(ctx: Context, job: Any) -> Any:
    if job.status == protocol.STATUS_DONE:
        message = await ctx.store.queue.last_message(job.id, protocol.MSG_TEST_DONE)
    elif job.status in protocol.TERMINAL_STATUSES:
        message = await ctx.store.queue.last_message(job.id, job.status)
    else:
        message = None
    return message.payload if message else None


async def test(
    ctx: Context,
    project: str,
    ref: str,
    cmd: str = "",
    args: list[str] | None = None,
    wait: int = 0,
) -> Any:
    if not 0 <= wait <= protocol.TEST_WAIT_MAX_S:
        raise HubError(f"wait: от 0 до {protocol.TEST_WAIT_MAX_S} секунд")
    if await ctx.store.queue.has_project(project) and not await repos.ref_exists(
        ctx.store.git_root, project, ref
    ):
        raise HubError(
            f"в хабе нет ref {ref!r}: запушь снимок, "
            f"git push {protocol.HTTP_GIT_PREFIX}{project}{protocol.GIT_REPO_SUFFIX} "
            f"HEAD:{protocol.TEST_BRANCH_PREFIX}{ctx.client}/<n>"
        )
    job = await ctx.store.queue.submit_test(ctx.client, project, ref, cmd, args)
    if wait:
        job = await ctx.store.queue.wait_terminal(job.id, wait)
    return {"job": job_view(job, with_task=False), "result": await test_outcome(ctx, job)}


async def test_log(ctx: Context, id: str, file: str = protocol.LOG_FILE_OUTPUT) -> Any:
    job = await ctx.store.queue.get_job(id, ctx.client)
    if job.kind != protocol.JOB_KIND_TEST:
        raise HubError(f"задание {id} не test")
    tail = joblogs.read_tail(ctx.store.logs_root, id, file, protocol.LOG_TAIL_MAX_BYTES)
    if tail is None:
        raise HubError(f"у задания {id} (статус {job.status}) нет файла {file}")
    text, size = tail
    return {
        "id": id,
        "file": file,
        protocol.FIELD_REF: job.params[protocol.FIELD_REF],
        "bytes": size,
        "truncated": size > protocol.LOG_TAIL_MAX_BYTES,
        "text": text,
    }


async def list_jobs(ctx: Context, statuses: list[str] | None = None) -> Any:
    if statuses is not None:
        unknown = [s for s in statuses if s not in protocol.STATUSES]
        if unknown:
            raise HubError(f"неизвестные статусы {unknown}, есть: {', '.join(protocol.STATUSES)}")
    jobs = await ctx.store.queue.list_jobs(ctx.client, statuses)
    return [job_view(job, with_task=False) for job in jobs]


async def status(ctx: Context, id: str) -> Any:
    job = await ctx.store.queue.get_job(id, ctx.client)
    view = job_view(job)
    if job.kind == protocol.JOB_KIND_TEST:
        view["result"] = await test_outcome(ctx, job)
    return view


async def inbox(ctx: Context, since: int = 0, limit: int = 100) -> Any:
    if since < 0 or limit < 1:
        raise HubError("inbox: since не меньше 0, limit не меньше 1")
    messages = await ctx.store.queue.inbox(ctx.client, since, limit)
    return {
        protocol.FIELD_MESSAGES: [asdict(m) for m in messages],
        protocol.FIELD_CURSOR: messages[-1].seq if messages else since,
    }


async def ack(ctx: Context, upto: int) -> Any:
    return {"acked": await ctx.store.queue.ack(ctx.client, upto)}


async def send(ctx: Context, kind: str, job_id: str, payload: dict[str, Any]) -> Any:
    message = await ctx.store.queue.post_from_client(ctx.client, job_id, kind, payload)
    job = await ctx.store.queue.get_job(job_id, ctx.client)
    return {protocol.FIELD_SEQ: message.seq, "status": job.status}


async def answer(ctx: Context, id: str, text: str) -> Any:
    if not text.strip():
        raise HubError("ответ не должен быть пустым")
    return await send(ctx, protocol.MSG_ANSWER, id, {protocol.PAYLOAD_TEXT: text})


async def resume(ctx: Context, id: str, text: str = "", model: str | None = None) -> Any:
    payload: dict[str, Any] = {}
    if text.strip():
        payload[protocol.PAYLOAD_TEXT] = text
    if model is not None:
        payload[protocol.PAYLOAD_MODEL] = model
    result = await send(ctx, protocol.MSG_RESUME, id, payload)
    result["warnings"] = await model_warnings(ctx, model) if model else []
    return result


async def stop(ctx: Context, id: str) -> Any:
    return await send(ctx, protocol.MSG_STOP, id, {})


async def release(ctx: Context, id: str) -> Any:
    return await send(ctx, protocol.MSG_RELEASE, id, {})


ID = Param("id", "string", "Номер задания из devlab_submit.", required=True)

TOOLS: tuple[Tool, ...] = (
    Tool(
        protocol.TOOL_MODELS,
        "Каталог живых моделей: имя, шлюз, цена, окно, остаток лимита. Показывает только модели"
        " шлюзов, приславших heartbeat за последние 2 минуты.",
        (),
        models,
    ),
    Tool(
        protocol.TOOL_PROJECT_ADD,
        "Зарегистрировать проект и создать для него пустой git-репозиторий на хабе. Путь для"
        " git-обмена вернётся в git_path.",
        (
            Param(
                "name",
                "string",
                "Имя проекта: a-z, 0-9, дефис, подчёркивание, до 64 символов.",
                required=True,
            ),
        ),
        project_add,
    ),
    Tool(protocol.TOOL_PROJECTS, "Список зарегистрированных проектов.", (), projects),
    Tool(
        protocol.TOOL_SUBMIT,
        "Поставить задание агенту в очередь. Базу нужно заранее запушить в ветку base/<имя>."
        " Если модели нет в каталоге, задание всё равно встанет в очередь и будет ждать её;"
        " ответ предупредит об этом в warnings.",
        (
            Param("project", "string", "Имя зарегистрированного проекта.", required=True),
            Param("model", "string", "Имя модели из devlab_models.", required=True),
            Param(
                "task",
                "string",
                "Текст task.md целиком (шаблон: ресурс devlab://task-template).",
                True,
            ),
            Param(
                "base_branch",
                "string",
                "Ветка базы в репозитории проекта, уже запушенная: base/<имя>.",
                required=True,
            ),
            Param("priority", "integer", "Приоритет, больше — раньше. По умолчанию 0."),
        ),
        submit,
    ),
    Tool(
        protocol.TOOL_TEST,
        "Прогнать тесты проекта на исполнителе. Снимок рабочей копии заранее пушится в ветку"
        " test/<ваше имя>/<n>, её (или ветку, или sha) передайте в ref. Задания test идут раньше"
        " заданий агентов. Если wait > 0, ждём результат до этого срока, иначе (или если не"
        " дождались) вернётся задание, а результат test_done придёт в devlab_inbox.",
        (
            Param("project", "string", "Имя зарегистрированного проекта.", required=True),
            Param("ref", "string", "Ветка или коммит в репозитории проекта.", required=True),
            Param("cmd", "string", "Команда; пусто: из .devlab.toml проекта."),
            Param("args", "array", "Аргументы команды: пути, -k, -x."),
            Param(
                "wait",
                "integer",
                f"Сколько секунд ждать результата, до {protocol.TEST_WAIT_MAX_S}. По умолчанию 0.",
            ),
        ),
        test,
    ),
    Tool(
        protocol.TOOL_TEST_LOG,
        "Хвост полного лога (или junit.xml) своего задания test: последние 64 КиБ, размер файла"
        " и ref задания.",
        (
            ID,
            Param("file", "string", "output.log (по умолчанию) или junit.xml."),
        ),
        test_log,
    ),
    Tool(
        protocol.TOOL_LIST,
        "Ваши задания (без текста task.md): id, проект, модель, статус, расход.",
        (
            Param(
                "statuses",
                "array",
                "Только эти статусы: queued, running, waiting_owner, done, failed, stopped.",
            ),
        ),
        list_jobs,
    ),
    Tool(
        protocol.TOOL_STATUS,
        "Подробности вашего задания, включая task.md. У test-задания поле result: итог теста"
        " (как у devlab_test), null пока задание не завершено.",
        (ID,),
        status,
    ),
    Tool(
        protocol.TOOL_INBOX,
        "Новые сообщения агентов по вашим заданиям с номером больше since. Курсор в ответе"
        " передайте в следующий вызов и в devlab_ack.",
        (
            Param(
                "since", "integer", "Курсор: номер последнего виденного сообщения, по умолчанию 0."
            ),
            Param("limit", "integer", "Сколько сообщений вернуть, по умолчанию 100."),
        ),
        inbox,
    ),
    Tool(
        protocol.TOOL_ACK,
        "Отметить сообщения с номерами до upto прочитанными. Непрочитанные хаб хранит без срока.",
        (Param("upto", "integer", "Номер последнего прочитанного сообщения.", required=True),),
        ack,
    ),
    Tool(
        protocol.TOOL_ANSWER,
        "Ответить на вопрос агента. Задание должно быть в статусе waiting_owner; оно вернётся в"
        " running.",
        (ID, Param("text", "string", "Текст ответа.", required=True)),
        answer,
    ),
    Tool(
        protocol.TOOL_RESUME,
        "Вернуть завершённое, упавшее, остановленное или ждущее задание в очередь, например с"
        " замечаниями после ревью. Можно сменить модель: новый агент начнёт с wip/<id>, task.md и"
        " журнала шагов, диалог старой модели не переносится.",
        (
            ID,
            Param("text", "string", "Замечания или указания для агента."),
            Param("model", "string", "Другая модель из devlab_models, если нужно сменить."),
        ),
        resume,
    ),
    Tool(
        protocol.TOOL_STOP,
        "Остановить задание в статусе queued, running или waiting_owner.",
        (ID,),
        stop,
    ),
    Tool(
        protocol.TOOL_RELEASE,
        "Сказать исполнителю не держать машину бодрой в ожидании вашего ответа. Только для"
        " задания в статусе waiting_owner; статус не меняется.",
        (ID,),
        release,
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str
    description: str
    filename: str


RESOURCES: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        protocol.RESOURCE_TASK_TEMPLATE,
        "Шаблон task.md",
        "Структура задания для агента: цель, шаги, критерии, тесты, ограничения, бюджет.",
        "task-template.md",
    ),
    ResourceSpec(
        protocol.RESOURCE_USAGE,
        "Как пользоваться мастерской",
        "Цикл: проект, push базы, задание, ящик, ревью, приём.",
        "usage.md",
    ),
)

RESOURCES_BY_URI = {resource.uri: resource for resource in RESOURCES}


def read_resource(spec: ResourceSpec) -> str:
    return (RESOURCES_DIR / spec.filename).read_text(encoding="utf-8")
