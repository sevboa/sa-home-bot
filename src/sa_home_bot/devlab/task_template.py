from __future__ import annotations

from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "hub" / "resources" / "task-template.md"
ARCHITECT_HEADER = "## Требования к задаче разработчика (шаблон task.md)"


def architect_section() -> str:
    return f"{ARCHITECT_HEADER}\n\n{TEMPLATE_PATH.read_text(encoding='utf-8').strip()}"
