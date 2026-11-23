from __future__ import annotations

STYLE_CB_V5 = "cb_v5"
UNCHANGED = (
    "[unchanged since call {call} — the content is above in this conversation or in your "
    "script variables]"
)


class RepeatLog:
    def __init__(self) -> None:
        self._files: dict[str, tuple[str, list[tuple[int, int, int]]]] = {}
        self._marked: dict[str, int] = {}

    def reset(self) -> None:
        self._files.clear()
        self._marked.clear()

    def forget_call(self, call: int) -> None:
        for _digest, spans in self._files.values():
            spans[:] = [span for span in spans if span[2] != call]
        for path in [path for path, marked in self._marked.items() if marked == call]:
            del self._marked[path]

    def check(self, path: str, digest: str, start: int, end: int, call: int) -> str:
        known = self._files.get(path)
        if known is None or known[0] != digest:
            known = (digest, [])
            self._files[path] = known
        spans = known[1]
        covering = _covering_call(spans, start, end)
        if covering is not None and self._marked.get(path) != call - 1:
            self._marked[path] = call
            return UNCHANGED.format(call=covering)
        self._marked.pop(path, None)
        spans.append((start, end, call))
        return ""


def _covering_call(spans: list[tuple[int, int, int]], start: int, end: int) -> int | None:
    reach = start
    calls: list[int] = []
    for span_start, span_end, call in sorted(spans):
        if span_start <= reach and span_end >= reach:
            reach = span_end + 1
            calls.append(call)
    return max(calls) if calls and reach > end else None
