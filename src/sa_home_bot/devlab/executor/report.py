from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

from sa_home_bot.devlab import protocol

MAX_FAILURES = 50
EXCERPT_CHARS = 500
TAIL_BYTES = 4096

DURATION_LINE = re.compile(r"\bin \d+(?:\.\d+)?s\b")
COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped)\b")
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$")
COUNT_KEYS = {
    "passed": protocol.SUMMARY_PASSED,
    "failed": protocol.SUMMARY_FAILED,
    "error": protocol.SUMMARY_ERRORS,
    "errors": protocol.SUMMARY_ERRORS,
    "skipped": protocol.SUMMARY_SKIPPED,
}


@dataclass(frozen=True)
class Report:
    summary: dict[str, int]
    failures: list[dict[str, str]] = field(default_factory=list)


def empty_summary() -> dict[str, int]:
    return dict.fromkeys(protocol.SUMMARY_KEYS, 0)


def failure(name: str, excerpt: str) -> dict[str, str]:
    return {
        protocol.FAILURE_NAME: name,
        protocol.FAILURE_EXCERPT: excerpt.strip()[:EXCERPT_CHARS],
    }


def parse_text(output: str) -> Report:
    lines = output.splitlines()
    summary = empty_summary()
    for line in reversed(lines):
        if DURATION_LINE.search(line) and (COUNT.search(line) or "no tests ran" in line):
            for number, word in COUNT.findall(line):
                summary[COUNT_KEYS[word]] += int(number)
            break
    failures = []
    for line in lines:
        match = FAILED_LINE.match(line.strip())
        if match and len(failures) < MAX_FAILURES:
            failures.append(failure(match.group(1), match.group(2) or ""))
    return Report(summary, failures)


def parse_junit(data: bytes) -> Report | None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    summary = empty_summary()
    failures: list[dict[str, str]] = []
    cases = list(root.iter("testcase"))
    if not cases:
        return None
    for case in cases:
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            key = protocol.SUMMARY_FAILED if problem.tag == "failure" else protocol.SUMMARY_ERRORS
            summary[key] += 1
            if len(failures) < MAX_FAILURES:
                name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
                failures.append(failure(name, problem.get("message") or problem.text or ""))
        elif case.find("skipped") is not None:
            summary[protocol.SUMMARY_SKIPPED] += 1
        else:
            summary[protocol.SUMMARY_PASSED] += 1
    return Report(summary, failures)


def build_report(output: str, junit: bytes | None) -> Report:
    from_junit = parse_junit(junit) if junit else None
    return from_junit or parse_text(output)


def tail_of(output: bytes) -> str:
    return output[-TAIL_BYTES:].decode("utf-8", errors="replace")
