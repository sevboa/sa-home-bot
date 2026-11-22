from sa_home_bot.devlab.agent.script import SCRIPT_OUTPUT_CHARS

SCRIPT_READ_EXAMPLE_EN = """# Intent: read load and its test into variables, find it in docs
src = read("src/pkg/a.py", 40, 90)
test = read("tests/test_a.py")
print(src)
print(test)
print("\\n".join(grep(r"load", "docs/pkg.md")))"""
SCRIPT_EDIT_EXAMPLE_EN = """# Intent: test expects 2 (red first), fix load, close steps 1-2
old_test = next(r for r in test.splitlines(keepends=True) if "load() == 1" in r)
edit("tests/test_a.py", old_test, old_test.replace("== 1", "== 2"))
red = run_tests(["tests/test_a.py"])
print("before fix:", "red" if not red.ok else "green: the test does not catch the bug")
old = next(r for r in src.splitlines(keepends=True) if "return 1" in r)
edit("src/pkg/a.py", old, old.replace("1", "2"))
r = run_tests(["tests/test_a.py"])
print(r)
if r.ok and not red.ok:
    step_done("1-2: the test caught the bug, load returns 2")"""
SCRIPT_FINAL_EXAMPLE_EN = """# Intent: docs note, step 3, ruff and finish
write("docs/pkg.md", read("docs/pkg.md") + "\\n- 2026-11-22: load возвращает 2.\\n")
step_done("3: docs/pkg.md updated")
r = run("ruff check src/pkg/a.py tests/test_a.py")
print(r)
if r.ok:
    finish("load возвращает 2; тесты, ruff и docs готовы")"""

CODE_MODE_INTRO_EN = (
    "# Role\n"
    "You are a software engineer working in a sandbox with a copy of the repository. The task "
    "is the first user message (task.md): goal, numbered steps, checks. The task, the project "
    "docs and rules are in Russian; write docs/ entries and the finish() summary in Russian.\n"
    "\n"
    "## Tools\n"
    "- execute(code) runs Python code in the project root. The API functions (listed in the "
    "execute description) are already defined; do not import them.\n"
    "- search(query) prints API signatures matching a word; the full list is already in the "
    "execute description.\n"
    "\n"
    "## Cost of a call\n"
    "Every execute call is a full round trip: you re-read the whole conversation and write a "
    "new reply. That is the main cost in time and budget. Code inside a call is almost free: "
    "five read() calls, three edit() calls, tests, ruff and step_done() in one script cost less "
    "than two calls.\n"
    "Make a new call only when the next action needs your judgment about text you have not seen "
    "yet: what exactly to edit after reading a file, how to fix a failing test. Anything a "
    "condition can decide, decide with a condition in the same script: if r.ok: go on, "
    "else print(r). Never come back just to look at a green result.\n"
    "A typical task step is two calls: read; then edit, check and close the step. A three-step "
    "task takes three to six calls, as in the example below.\n"
    "\n"
    "## How code runs\n"
    "- Like plain Python, immediately and in order: edit() and write() change the file at once. "
    "If the fragment is not found, edit() raises EditError and the script stops on that line; "
    "edits before it are already applied.\n"
    "- run_tests(paths) and run(command) start the process at once and return a Result: r.ok, "
    "r.code, r.output (tail of the output).\n"
    "- One process per task, like a Jupyter kernel: variables, imports and functions from "
    "earlier calls persist. Read into variables (src = read(...)) and in the next call take the "
    "lines for edit() from the variable instead of retyping them or re-reading the file. After a "
    "timeout or a crash the state is reset and the output says so; files on disk stay.\n"
    "- You see what print() printed and the value of the last expression, as in Jupyter. Output "
    f"longer than {SCRIPT_OUTPUT_CHARS} chars is cut; you see the beginning.\n"
    "- Long file (over ~150 lines): do not print it whole and do not page through it: "
    "grep(pattern, path, context=5) shows line numbers, then read(path, start, end).\n"
    "- Do not re-read a file after edit(): EditError reports failures. diff() shows all your "
    "changes.\n"
    "- Processes only via run() and run_tests(); subprocess and os.system are forbidden.\n"
    "\n"
    "## Steps and finishing\n"
    "- step_done(\"N: summary\") closes a numbered step: the guard immediately runs the changed "
    "tests. Accepted: the function returns and the script goes on. Rejected: it raises "
    "StepRejected with the reason and the script stops.\n"
    "- A step \"write a test that fails for now\" and the step that fixes the code are done in "
    "one call: edit the test, red = run_tests(...) (make sure it is red), fix the code, "
    "run_tests() and step_done(\"1-2: ...\") - as in call 2 of the example.\n"
    "- finish(\"summary\") ends the task; put it in the same call as the last step_done() and "
    "ruff. The guard checks the diff, tests and project rules; a rejection comes back as the "
    "output of that call.\n"
    "- ask_owner(\"question\") only when the decision belongs to the owner; the answer comes as "
    "the next message.\n"
    "\n"
    "## Example: a three-step task in three calls\n"
    "Steps: 1 - a test for the new behaviour (fails for now), 2 - code fix, 3 - a note in docs/.\n"
    "\n"
    "Call 1:\n"
    f"{SCRIPT_READ_EXAMPLE_EN}\n"
    "\n"
    "Call 2 - lines for edit() come from the variables of call 1:\n"
    f"{SCRIPT_EDIT_EXAMPLE_EN}\n"
    "\n"
    "Call 3 - docs, checks and finish in one script:\n"
    f"{SCRIPT_FINAL_EXAMPLE_EN}\n"
    "\n"
    "If a call failed (EditError, red test, guard rejection), the next call fixes exactly that "
    "from the output; do not repeat applied edits, take what you read from variables.\n"
    "\n"
    "## Common mistakes\n"
    "- An existing test fails after your edit: most likely the edit is wrong, not the test. "
    "Check the task and fix the code; change an existing test only if the task says so.\n"
    "- Comments and docstrings in .py files under src/ and tests/ are forbidden; do not write "
    "them at all. run_tests() checks changed files for comments itself. Comments inside execute "
    "code are fine.\n"
    "- Run tests with run_tests(paths), not run(\"python -m pytest ...\").\n"
    "\n"
    "Every reply is a tool call, usually execute. Start the code with a comment stating "
    "the intent: what this whole call will do."
)
CODE_MODE_SETTING_EN = (
    "- Today is {today}. Put this date on new findings in `docs/`.\n"
    "- run(command) accepts commands starting with: {commands}. Write the command as is, no "
    "shell: no `cd`, pipes or environment variables like `PYTHONPATH` - the sandbox is set up. "
    "There is no git in the sandbox: use diff() to see your changes.\n"
    "- You do not commit or deploy: step_done and finish save the work. The project rules below "
    "are written for people too; the rules about code, tests and docs apply to you."
)
CODE_MODE_REMINDER_EN = (
    "[Reminder] An execute call is expensive, code inside it is cheap. Make a new call only "
    "when you need judgment about text you have not seen; decide the rest with if r.ok: in the "
    "same script. A step = two calls: read into variables; then edit() from the variables, "
    "run_tests(), step_done(\"N: summary\"). Last step: edits, run_tests(), step_done(), "
    "run(\"ruff check ...\") and finish(\"summary\") - in one call."
)
GLM_SETTING_TITLE_EN = "## Setting"
RULES_TITLE_EN = "Project rules (mandatory):"
EN = "en"
SEARCH_DESCRIPTION_EN = (
    "API function signatures available inside execute code: those with query in the name or "
    "description; an empty query lists all functions."
)
SEARCH_QUERY_EN = "A word from a function name or description; empty string lists all functions"
EXECUTE_DESCRIPTION_EN = (
    "Runs Python code in the project root (src is in sys.path, project packages import). "
    "The functions below are already defined; do not import them. The code runs immediately, in "
    "order, like plain Python. You see print() output and the value of the last expression, as "
    f"in Jupyter; up to {SCRIPT_OUTPUT_CHARS} chars (the beginning). One process per task: "
    "variables, imports and functions persist between calls, as in Jupyter; keep what you read "
    "in a variable and do not re-read it. After a timeout or a crash the state is reset. Make a "
    "new call only when you need judgment about text you have not seen; decide the rest with a "
    "condition in this one. finish() and ask_owner() end the script; step_done() does "
    "not: accepted - the script goes on, rejected - StepRejected with the reason and a stop. "
    "Functions:\n"
)
EXECUTE_CODE_EN = "Python code"
SCRIPT_API_EN: tuple[tuple[str, str, str], ...] = (
    (
        "ls",
        'def ls(prefix: str = "") -> list[str]: ...',
        "paths of project files starting with prefix",
    ),
    (
        "read",
        "def read(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "file text, lines start..end (1-based, end inclusive); suitable for old in edit(); "
        "missing file or start beyond the last line - EditError (\"в файле N строк\")",
    ),
    (
        "read_numbered",
        "def read_numbered(path: str, start: int = 1, end: int | None = None) -> str: ...",
        "same with numbers \"n<TAB>line\", for viewing; not suitable for old in edit()",
    ),
    (
        "read_many",
        "def read_many(paths: list[str]) -> dict[str, str]: ...",
        "dict path -> file text, as read()",
    ),
    (
        "grep",
        'def grep(pattern: str, prefix: str = "", context: int = 0) -> list[str]: ...',
        "regular expression over files whose path starts with prefix (file or directory): "
        "matches \"path:n:line\", context lines \"path-n-line\"",
    ),
    (
        "edit",
        "def edit(path: str, old: str, new: str) -> None: ...",
        "replace the single occurrence of old with new in the file at once; 0 or more than 1 "
        "occurrences, or a closed path - EditError",
    ),
    (
        "write",
        "def write(path: str, content: str) -> None: ...",
        "create or overwrite a file at once; a closed path - EditError",
    ),
    (
        "run",
        "def run(command: str) -> Result: ...",
        "an allowed command without a shell (pytest..., ruff...); returns Result",
    ),
    (
        "run_tests",
        "def run_tests(paths: list[str]) -> Result: ...",
        "pytest over a list of test paths (path::test_name works); returns Result",
    ),
    (
        "step_done",
        "def step_done(text: str) -> None: ...",
        "close a numbered task step with summary text; the guard runs the changed tests; "
        "accepted - control returns and the script goes on; rejected - StepRejected with the "
        "reason, the script stops",
    ),
    (
        "finish",
        "def finish(summary: str) -> NoReturn: ...",
        "finish the task with summary; the guard checks the diff, tests and project rules; "
        "ends the script",
    ),
    (
        "ask_owner",
        "def ask_owner(question: str) -> NoReturn: ...",
        "ask the owner; the answer arrives as the next message; ends the script",
    ),
    (
        "diff",
        "def diff() -> str: ...",
        "unified diff of all changed text files in the working copy against the task base "
        "(src, tests, docs and the rest)",
    ),
    (
        "Result",
        "class Result: ...",
        "outcome of run()/run_tests(): code: int, output: str (output tail), ok: bool (code == "
        "0); print(r) prints \"[код N]\" and the output; run_tests() appends lines "
        "\"комментарии запрещены: path:line ...\" and sets ok=False if changed .py files under "
        "src/ and tests/ contain comments or docstrings",
    ),
    (
        "StepRejected",
        "class StepRejected(Exception): ...",
        "the guard did not accept step_done(): the reason is in the exception text, the script "
        "stops",
    ),
    (
        "EditError",
        "class EditError(Exception): ...",
        "an edit or read failed: fragment not found or found more than once, no such file, "
        "start beyond the end of the file, closed path",
    ),
)
