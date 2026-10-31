from __future__ import annotations

import os
import sys

from sa_home_bot.devlab import protocol

ZERO_SHA = "0" * 40

ALLOW_CREATE = "create"
ALLOW_UPDATE = "update"
ALLOW_DELETE = "delete"

POLICY: dict[str, dict[str, frozenset[str]]] = {
    protocol.ROLE_EXECUTOR: {
        protocol.WIP_BRANCH_PREFIX: frozenset({ALLOW_CREATE, ALLOW_UPDATE, ALLOW_DELETE}),
        protocol.AGENT_BRANCH_PREFIX: frozenset({ALLOW_CREATE, ALLOW_UPDATE}),
        protocol.TEST_BRANCH_PREFIX: frozenset({ALLOW_DELETE}),
    },
    protocol.ROLE_CLIENT: {
        protocol.BASE_BRANCH_PREFIX: frozenset({ALLOW_CREATE, ALLOW_UPDATE, ALLOW_DELETE}),
        protocol.AGENT_BRANCH_PREFIX: frozenset({ALLOW_DELETE}),
    },
}
CLIENT_OWN_TEST_OPS = frozenset({ALLOW_CREATE, ALLOW_UPDATE})


def rules_for(role: str, user: str) -> dict[str, frozenset[str]] | None:
    rules = POLICY.get(role)
    if rules is None:
        return None
    if role == protocol.ROLE_CLIENT and user and "/" not in user:
        return {**rules, f"{protocol.TEST_BRANCH_PREFIX}{user}/": CLIENT_OWN_TEST_OPS}
    return rules


def operation(old: str, new: str) -> str:
    if old == ZERO_SHA:
        return ALLOW_CREATE
    if new == ZERO_SHA:
        return ALLOW_DELETE
    return ALLOW_UPDATE


def check_ref(role: str, ref: str, old: str, new: str, user: str = "") -> str | None:
    rules = rules_for(role, user)
    if rules is None:
        return f"роль {role!r} не может пушить в хаб"
    if not ref.startswith(protocol.HEADS_REF_PREFIX):
        return f"{ref}: можно менять только ветки ({protocol.HEADS_REF_PREFIX}*)"
    branch = ref[len(protocol.HEADS_REF_PREFIX) :]
    op = operation(old, new)
    for prefix, allowed in rules.items():
        if branch.startswith(prefix) and len(branch) > len(prefix):
            if op in allowed:
                return None
            return f"{branch}: роль {role} не может выполнить «{op}» над {prefix}*"
    prefixes = ", ".join(f"{prefix}*" for prefix in rules)
    return f"{branch}: роль {role} пушит только в {prefixes}"


def check_updates(role: str, lines: list[str], user: str = "") -> list[str]:
    errors = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            errors.append(f"непонятная строка обновления: {line!r}")
            continue
        old, new, ref = parts
        error = check_ref(role, ref, old, new, user)
        if error:
            errors.append(error)
    return errors


def main() -> int:
    role = os.environ.get(protocol.GIT_ENV_ROLE, "")
    user = os.environ.get(protocol.GIT_ENV_USER, "")
    lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
    errors = check_updates(role, lines, user)
    for error in errors:
        print(f"devlab: отклонено ({user or 'аноним'}): {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
