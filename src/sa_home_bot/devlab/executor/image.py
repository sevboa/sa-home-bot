from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sa_home_bot.devlab.executor.spec import TestSection
from sa_home_bot.devlab.executor.wrapper import SYNC_TIMEOUT_S, RunnerError, WrapperClient

log = logging.getLogger(__name__)

CONTAINERFILE_VERSION = 1
IMAGE_TAG_PREFIX = "t"
IMAGE_TAG_HASH_CHARS = 24
SETUP_DIR = "/setup"
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ImageRecipe:
    image: str
    setup: tuple[str, ...] = ()
    setup_files: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_section(cls, section: TestSection, default_image: str) -> ImageRecipe:
        return cls(
            image=section.image or default_image,
            setup=tuple(section.setup),
            setup_files=tuple(section.setup_files),
            env=tuple(section.env.items()),
        )


def read_setup_files(workdir: Path, names: Sequence[str]) -> dict[str, bytes]:
    root = workdir.resolve()
    files: dict[str, bytes] = {}
    for name in names:
        rel = PurePosixPath(name)
        if not name or rel.is_absolute() or ".." in rel.parts or "\n" in name:
            raise RunnerError(f"setup_files: недопустимый путь {name!r}")
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise RunnerError(f"setup_files: нет файла {name!r} в коде задания")
        files[rel.as_posix()] = path.read_bytes()
    return files


def check_image(image: str) -> str:
    if not IMAGE_RE.match(image):
        raise RunnerError(f"недопустимое имя образа: {image!r}")
    return image


def check_env(env: Sequence[tuple[str, str]]) -> None:
    for key, _ in env:
        if not ENV_KEY_RE.match(key):
            raise RunnerError(f"недопустимое имя переменной окружения: {key!r}")


def containerfile(
    image: str, setup: Sequence[str], files: dict[str, bytes], env: Sequence[tuple[str, str]]
) -> str:
    lines = [f"FROM {check_image(image)}", f"WORKDIR {SETUP_DIR}"]
    check_env(env)
    lines += [f"ENV {key}={json.dumps(value)}" for key, value in env]
    for name in sorted(files):
        lines.append(f"COPY {json.dumps([name, f'{SETUP_DIR}/{name}'])}")
    lines += [f"RUN {json.dumps(['sh', '-c', command])}" for command in setup]
    lines.append("WORKDIR /work")
    return "\n".join(lines) + "\n"


def image_tag(
    image: str, setup: Sequence[str], files: dict[str, bytes], env: Sequence[tuple[str, str]]
) -> str:
    digest = hashlib.sha256()
    payload = {
        "version": CONTAINERFILE_VERSION,
        "containerfile": containerfile(image, setup, files, env),
        "files": {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(files)},
    }
    digest.update(json.dumps(payload, sort_keys=True).encode())
    return IMAGE_TAG_PREFIX + digest.hexdigest()[:IMAGE_TAG_HASH_CHARS]


def image_names(listing: str) -> set[str]:
    try:
        entries = json.loads(listing or "[]") or []
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for entry in entries:
        for raw in (entry.get("Names") or []) if isinstance(entry, dict) else []:
            names.add(raw.rsplit(":", 1)[0].rsplit("/", 1)[-1])
    return names


async def ensure_image(
    wrapper: WrapperClient, name: str, recipe: ImageRecipe, workdir: Path, build_timeout_s: float
) -> str:
    files = read_setup_files(workdir, recipe.setup_files)
    tag = image_tag(recipe.image, recipe.setup, files, recipe.env)
    listing = await wrapper.call("images", timeout=SYNC_TIMEOUT_S)
    if listing.returncode == 0 and tag in image_names(listing.stdout_text):
        return tag
    text = containerfile(recipe.image, recipe.setup, files, recipe.env)
    log.info("Сборка образа devlab/%s для задания %s", tag, name)
    await wrapper.checked(
        "сборка образа", "build", name, tag, input=text.encode(), timeout=build_timeout_s
    )
    return tag
