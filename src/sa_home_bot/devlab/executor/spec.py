from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SPEC_FILE = ".devlab.toml"
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_COMMAND_TIMEOUT_S = 300.0
DEFAULT_NARROW_TEST_TIMEOUT_S = 300.0
DEFAULT_OUTPUT_CHARS = 6000


class SpecError(Exception):
    pass


class TestSection(BaseModel):
    __test__ = False

    model_config = ConfigDict(extra="forbid")

    cmd: str = ""
    args: list[str] = Field(default_factory=list)
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0)
    image: str = ""
    setup: list[str] = Field(default_factory=list)
    setup_files: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class AgentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    secret_paths: list[str] = Field(default_factory=list)
    diff_prefixes: list[str] = Field(default_factory=list)
    allow_remote_models: bool = False
    command_timeout_s: float = Field(default=DEFAULT_COMMAND_TIMEOUT_S, gt=0)
    narrow_test_timeout_s: float = Field(default=DEFAULT_NARROW_TEST_TIMEOUT_S, gt=0)
    output_chars: int = Field(default=DEFAULT_OUTPUT_CHARS, gt=0)


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: str = ""
    test: TestSection = Field(default_factory=TestSection)
    agent: AgentSection = Field(default_factory=AgentSection)


def parse_spec(data: bytes | None) -> ProjectSpec:
    if data is None:
        return ProjectSpec()
    try:
        return ProjectSpec.model_validate(tomllib.loads(data.decode("utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise SpecError(f"{SPEC_FILE}: {exc}") from exc


def external_spec(specs_dir: Path | None, project: str) -> Path | None:
    if specs_dir is None:
        return None
    path = specs_dir / f"{project}.toml"
    return path if path.is_file() else None


def read_external_rule(specs_dir: Path, name: str) -> bytes | None:
    root = specs_dir.resolve()
    path = (specs_dir / name).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path.read_bytes() if path.is_file() else None


async def load_project_spec(
    specs_dir: Path | None, project: str, read_repo: Callable[[], Awaitable[bytes | None]]
) -> tuple[ProjectSpec, Path | None]:
    external = external_spec(specs_dir, project)
    data = external.read_bytes() if external is not None else await read_repo()
    return parse_spec(data), external
