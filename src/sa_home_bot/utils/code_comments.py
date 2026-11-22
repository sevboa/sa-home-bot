import ast
import io
import re
import tokenize

PRAGMA = re.compile(r"#\s*(noqa|type:\s*ignore|pragma:|fmt:|ruff:)")


def comment_findings(source: str) -> list[tuple[int, str]]:
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    found = [
        (token.start[0], token.string)
        for token in tokens
        if token.type == tokenize.COMMENT and not PRAGMA.match(token.string)
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                name = getattr(node, "name", "<module>")
                found.append((node.body[0].lineno, f"докстринг {name}"))
    return sorted(found)
