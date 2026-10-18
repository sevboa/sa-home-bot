from __future__ import annotations

from tests.unit.support.fakes import FakeUser


def user(first_name, last_name=None, username=None, id=1):
    return FakeUser(id=id, first_name=first_name, last_name=last_name, username=username)


class FakeRichSession:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.partials: list[tuple[str, bool]] = []
        self.finalized_statuses: list[str] = []

    async def push_status(self, text: str) -> None:
        self.statuses.append(text)

    async def on_partial(self, text: str, done: bool) -> None:
        self.partials.append((text, done))

    async def finalize_status(self, markdown: str) -> None:
        self.finalized_statuses.append(markdown)
