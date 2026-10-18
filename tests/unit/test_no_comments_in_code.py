import ast
import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRAGMA = re.compile(r"#\s*(noqa|type:\s*ignore|pragma:|fmt:|ruff:)")
PYTHON_FILES = sorted(
    path
    for folder in ("src", "tests")
    for path in (REPO_ROOT / folder).rglob("*.py")
    if "__pycache__" not in path.parts
)


def _comments(source: str) -> list[str]:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [t.string for t in tokens if t.type == tokenize.COMMENT and not PRAGMA.match(t.string)]


def _docstring_owners(source: str) -> list[str]:
    owners = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                owners.append(getattr(node, "name", "<module>"))
    return owners


def test_code_carries_no_comments_or_docstrings():
    offenders = {
        str(path.relative_to(REPO_ROOT)): _comments(source) + _docstring_owners(source)
        for path in PYTHON_FILES
        if (source := path.read_text(encoding="utf-8"))
        and (_comments(source) or _docstring_owners(source))
    }
    assert offenders == {}, "документация живёт в docs/, а не в комментариях и докстрингах"
