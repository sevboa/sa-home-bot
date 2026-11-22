from pathlib import Path

from sa_home_bot.utils.code_comments import comment_findings

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_FILES = sorted(
    path
    for folder in ("src", "tests")
    for path in (REPO_ROOT / folder).rglob("*.py")
    if "__pycache__" not in path.parts
)


def test_code_carries_no_comments_or_docstrings():
    offenders = {
        str(path.relative_to(REPO_ROOT)): findings
        for path in PYTHON_FILES
        if (findings := comment_findings(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, "документация живёт в docs/, а не в комментариях и докстрингах"
