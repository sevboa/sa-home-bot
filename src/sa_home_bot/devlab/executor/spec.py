from __future__ import annotations

import tomllib

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SPEC_FILE = ".devlab.toml"
DEFAULT_TIMEOUT_S = 1800.0


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


class ProjectSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: str = ""
    test: TestSection = Field(default_factory=TestSection)


def parse_spec(data: bytes | None) -> ProjectSpec:
    if data is None:
        return ProjectSpec()
    try:
        return ProjectSpec.model_validate(tomllib.loads(data.decode("utf-8")))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise SpecError(f"{SPEC_FILE}: {exc}") from exc
