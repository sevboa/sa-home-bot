from sa_home_bot.devlab.agent.notes import NOTES_LIMIT_CHARS
from sa_home_bot.devlab.agent.script import SCRIPT_OUTPUT_CHARS

SCRIPT_READ_EXAMPLE_EN = """# Intent: read load and its test into variables, find it in docs
src = read("src/pkg/a.py", 40, 90)
test = read("tests/test_a.py")
print(src)
print(test)
print("\\n".join(grep(r"load", "docs/pkg.md")))"""
SCRIPT_TEST_EXAMPLE_EN = """# Intent: test expects 2, check it is red for the right reason, step 1
old_test = next(r for r in test.splitlines(keepends=True) if "load() == 1" in r)
edit("tests/test_a.py", old_test, old_test.replace("== 1", "== 2"))
red = run_tests(["tests/test_a.py"])
print("before fix:", "red" if not red.ok else "green: the test does not catch the bug")
print(red)"""
SCRIPT_EDIT_EXAMPLE_EN = """# Intent: test expects 2 (red first), fix load, steps 1-2
old_test = next(r for r in test.splitlines(keepends=True) if "load() == 1" in r)
edit("tests/test_a.py", old_test, old_test.replace("== 1", "== 2"))
red = run_tests(["tests/test_a.py"])
print("before fix:", "red" if not red.ok else "green: the test does not catch the bug")
old = next(r for r in src.splitlines(keepends=True) if "return 1" in r)
edit("src/pkg/a.py", old, old.replace("1", "2"))
r = run_tests(["tests/test_a.py"])
print(r)"""
SCRIPT_CODE_EXAMPLE_EN = """# Intent: fix load from the red output, run the test, step 2
old = next(r for r in src.splitlines(keepends=True) if "return 1" in r)
edit("src/pkg/a.py", old, old.replace("1", "2"))
r = run_tests(["tests/test_a.py"])
print(r)"""
SCRIPT_FINAL_EXAMPLE_EN = """# Intent: docs note, step 3, ruff and finish
write("docs/pkg.md", read("docs/pkg.md") + "\\n- 2026-11-22: load возвращает 2.\\n")
r = run("ruff check src/pkg/a.py tests/test_a.py")
print(r)
if r.ok:
    finish("load возвращает 2; тесты, ruff и docs готовы")"""

_CODE_MODE_HEAD_EN = (
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
    "five read() calls, three edit() calls, tests and ruff in one script cost less "
    "than two calls.\n"
    "Make a new call only when the next action needs your judgment about text you have not seen "
    "yet: what exactly to edit after reading a file, how to fix a failing test. Anything a "
    "condition can decide, decide with a condition in the same script: if r.ok: go on, "
    "else print(r). Never come back just to look at a green result.\n"
)
_CODE_MODE_COST_GLM_EN = (
    "Read and explore in batches: several read() and grep() calls in one call. Write in "
    "portions: one call is one complete portion of writing with a check. Typically a call "
    "\"test and red = run_tests(...)\", the next call \"code fix and run_tests()\", then docs "
    "and the finish (ruff, finish) in a short call or together with the last edit.\n"
    "Do not design the whole solution in one reply: write the first portion, look at the "
    "result, decide the next portion from it. An extra call is cheaper than long deliberation. "
    "A three-step task usually takes four to eight calls, as in the example below.\n"
    "Thinking before a call is a short plan of a few lines: what this call does and why. Do "
    "not write code in your thinking, write it straight into execute: a draft in thinking is "
    "double work. What code can check (library behavior, ruff or pytest settings, dates and "
    "time zones), check in the script instead of reasoning it out.\n"
)
_CODE_MODE_COST_REMOTE_EN = (
    "Requests are limited, thinking is free: every extra call is a loss, long thinking is "
    "not. Read in batches: all the read() and grep() calls you need in one call. Think in "
    "detail before writing: work out the whole solution - all files, the test, the code, "
    "docs, edge cases, the lines for edit(). Accumulate all the edits and apply them in one "
    "call with the checks inside the script: edit the test, a red run before the code fix, "
    "the code fix, a run, docs, ruff, finish on success. Code in your thinking is fine: a "
    "draft of the whole script is cheaper than an extra call.\n"
    "Guideline: a three-step task takes two to three calls: reading; writing everything with "
    "checks and finish; if it fails, a fix from the output.\n"
)
_CODE_MODE_NEIGHBORS_EN = (
    "Before writing, use grep() to find how it is already done nearby, and repeat it: ready "
    "helpers (in the same module and in utils/), the comments above neighboring tables in "
    "schema.sql, the style of neighboring methods. Something similar already exists - use "
    "it instead of writing your own; never write one thing twice. Look for the example in "
    "the same read call, batched with read(), not in a separate call.\n"
)
_CODE_MODE_CALC_EN = (
    "Expected values for a test (counter deltas, percentages, sums, dates, parsing a "
    "reference) are not to be worked out in your thinking: compute them with a script from "
    "the input data or the reference and print them, and put the result into the test. "
    "For example:\n"
    "```\n"
    "import json\n"
    "data = json.loads(read(\"tests/fixtures/x/in.json\"))\n"
    "a, b = data[\"before\"], data[\"after\"]\n"
    "print({k: b[k] - a[k] for k in a})\n"
    "```\n"
    "A minute in a script instead of ten in thinking.\n"
)
_CODE_MODE_RUNS_EN = (
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
    "- Output is cut by characters, not lines: a file over ~6000 chars (about 150 lines) - "
    "do not print it whole and do not page through it: "
    "grep(pattern, path, context=5) shows line numbers, then read(path, start, end).\n"
    "- Notes: learned something important (a signature, a path, an expected value, a decision, "
    "what is already done) - write it at once with note(\"...\"). The conversation history gets "
    "folded and script variables stay, but notes survive folding and restarts and are shown to "
    "you at the start of the task. Do not re-read what the notes already hold. Notes full - "
    "compress them with notes_set(\"...\"); notes() returns the current text.\n"
    "- Do not re-read a file after edit(): EditError reports failures. Use diff() only if you "
    "do not remember what you changed. If the last run is green and only docs/ changed since, "
    "call finish() at once: no rerun, no diff().\n"
    "- Processes only via run() and run_tests(); subprocess and os.system are forbidden.\n"
    "\n"
    "## Steps and finishing\n"
    "- finish(\"summary\") closes all steps of the task and ends it; put it in the same call as "
    "the last edits, tests and ruff. The guard checks the diff, tests and project rules; a "
    "rejection comes back as the output of that call.\n"
    "- step_done(\"N: summary\") is an optional intermediate check of a step on long tasks: the "
    "guard runs the changed tests. Accepted: the script goes on. Rejected: it raises "
    "StepRejected with the reason and the script stops.\n"
)
_CODE_MODE_RED_GLM_EN = (
    "- If a step needs a red test, verify it yourself: one call edits the test and runs "
    "red = run_tests(...), the next call, based on its output, fixes the code and runs "
    "run_tests() - as in calls 2 and 3 of the example.\n"
)
_CODE_MODE_RED_REMOTE_EN = (
    "- If a step needs a red test, verify it yourself in the same script: edit the test, "
    "red = run_tests(...), fix the code, run_tests() - as in call 2 of the example.\n"
)
_CODE_MODE_ASK_EN = (
    "- ask_owner(\"question\") only when the decision belongs to the owner; the answer comes as "
    "the next message.\n"
    "\n"
)
_CODE_MODE_EXAMPLE_GLM_EN = (
    "## Example: a three-step task in four calls\n"
    "Steps: 1 - a test for the new behaviour (fails for now), 2 - code fix, 3 - a note in docs/.\n"
    "\n"
    "Call 1:\n"
    f"{SCRIPT_READ_EXAMPLE_EN}\n"
    "\n"
    "Call 2 - the test and a red run; lines for edit() come from the variables of call 1:\n"
    f"{SCRIPT_TEST_EXAMPLE_EN}\n"
    "\n"
    "Call 3 - fix the code from the red output and run:\n"
    f"{SCRIPT_CODE_EXAMPLE_EN}\n"
    "\n"
    "Call 4 - docs, checks and finish in one script (finish closes steps 1-3):\n"
    f"{SCRIPT_FINAL_EXAMPLE_EN}\n"
    "\n"
)
_CODE_MODE_EXAMPLE_REMOTE_EN = (
    "## Example: a three-step task in three calls\n"
    "Steps: 1 - a test for the new behaviour (fails for now), 2 - code fix, 3 - a note in docs/.\n"
    "\n"
    "Call 1:\n"
    f"{SCRIPT_READ_EXAMPLE_EN}\n"
    "\n"
    "Call 2 - the test, a red run, the code fix and a run in one script; lines for edit() "
    "come from the variables of call 1:\n"
    f"{SCRIPT_EDIT_EXAMPLE_EN}\n"
    "\n"
    "Call 3 - docs, checks and finish in one script (finish closes steps 1-3):\n"
    f"{SCRIPT_FINAL_EXAMPLE_EN}\n"
    "\n"
)
_CODE_MODE_TAIL_EN = (
    "If a call failed (EditError, red test, guard rejection), the next call fixes exactly that "
    "from the output; do not repeat applied edits, take what you read from variables.\n"
    "\n"
    "## Common mistakes\n"
    "- An existing test fails after your edit: most likely the edit is wrong, not the test. "
    "Check the task and fix the code; change an existing test only if the task says so.\n"
    "- Comments and docstrings in .py files under src/ and tests/ are forbidden. The intent "
    "comment is only the first line of the script itself; text that goes into a file "
    "(new_test = '''...''', edit() and write() arguments) has no # and no docstrings. "
    "run_tests() checks changed files for comments itself.\n"
    "- A docs/ note is usually appended to the end of the file, as in the final call of the "
    "example; for a section use grep(heading, path, context=3), not reading the whole file.\n"
    "- ruff checks line length: wrap long signatures, tuples and parametrize right away.\n"
    "- Run tests with run_tests(paths), not run(\"python -m pytest ...\").\n"
    "\n"
    "Every reply is a tool call, usually execute. Start the code with a comment stating "
    "the intent: what this whole call will do."
)
CODE_MODE_INTRO_CB_V1_EN = (
    _CODE_MODE_HEAD_EN
    + _CODE_MODE_COST_GLM_EN
    + _CODE_MODE_RUNS_EN
    + _CODE_MODE_RED_GLM_EN
    + _CODE_MODE_ASK_EN
    + _CODE_MODE_EXAMPLE_GLM_EN
    + _CODE_MODE_TAIL_EN
)
CODE_MODE_INTRO_CB_V2_EN = (
    _CODE_MODE_HEAD_EN
    + _CODE_MODE_COST_GLM_EN
    + _CODE_MODE_NEIGHBORS_EN
    + _CODE_MODE_RUNS_EN
    + _CODE_MODE_RED_GLM_EN
    + _CODE_MODE_ASK_EN
    + _CODE_MODE_EXAMPLE_GLM_EN
    + _CODE_MODE_TAIL_EN
)
CODE_MODE_INTRO_CB_V3_EN = (
    _CODE_MODE_HEAD_EN
    + _CODE_MODE_COST_GLM_EN
    + _CODE_MODE_CALC_EN
    + _CODE_MODE_NEIGHBORS_EN
    + _CODE_MODE_RUNS_EN
    + _CODE_MODE_RED_GLM_EN
    + _CODE_MODE_ASK_EN
    + _CODE_MODE_EXAMPLE_GLM_EN
    + _CODE_MODE_TAIL_EN
)
CODE_MODE_INTRO_REMOTE_EN = (
    _CODE_MODE_HEAD_EN
    + _CODE_MODE_COST_REMOTE_EN
    + _CODE_MODE_RUNS_EN
    + _CODE_MODE_RED_REMOTE_EN
    + _CODE_MODE_ASK_EN
    + _CODE_MODE_EXAMPLE_REMOTE_EN
    + _CODE_MODE_TAIL_EN
)
CODE_MODE_SETTING_EN = (
    "- Today is {today}. Put this date on new findings in `docs/`.\n"
    "- run(command) accepts commands starting with: {commands}. Write the command as is, no "
    "shell: no `cd`, pipes or environment variables like `PYTHONPATH` - the sandbox is set up. "
    "There is no git in the sandbox: use diff() to see your changes.\n"
    "- You do not commit or deploy: step_done and finish save the work. The project rules below "
    "are written for people too; the rules about code, tests and docs apply to you."
)
_REMINDER_GLM_HEAD_EN = (
    "[Reminder] An execute call is expensive, code inside it is cheap. Make a new call only "
    "when you need judgment about text you have not seen; decide the rest with if r.ok: in the "
    "same script. Read in batches, write in portions: one call is one portion of writing "
    "with a check (test and red = run_tests(...); then code fix and run_tests()). Do not design "
    "the whole solution at once: first portion, result, next portion from it; an extra call is "
    "cheaper than long deliberation; in thinking, a short plan without code, check what you can "
    "with a script. "
)
_REMINDER_GLM_NEIGHBORS_EN = (
    "Before writing, use grep() in the same read call to find how it is done nearby "
    "(helpers in the module and utils/, comments in schema.sql, the style of neighboring "
    "methods) and repeat it; never write one thing twice. "
)
_REMINDER_GLM_FINISH_EN = (
    "Finish: last edits, run_tests(), run(\"ruff check ...\") and "
    "finish(\"summary\") - in one call; finish closes all steps, step_done() is optional."
)
_REMINDER_GLM_HEAD_CB_V3_EN = (
    _REMINDER_GLM_HEAD_EN.removesuffix("with a script. ")
    + "with a script, and compute the expected test values with a script too. "
)
CODE_MODE_REMINDER_CB_V1_EN = _REMINDER_GLM_HEAD_EN + _REMINDER_GLM_FINISH_EN
CODE_MODE_REMINDER_CB_V3_EN = (
    _REMINDER_GLM_HEAD_CB_V3_EN + _REMINDER_GLM_NEIGHBORS_EN + _REMINDER_GLM_FINISH_EN
)
CODE_MODE_REMINDER_CB_V2_EN = (
    _REMINDER_GLM_HEAD_EN + _REMINDER_GLM_NEIGHBORS_EN + _REMINDER_GLM_FINISH_EN
)
CODE_MODE_REMINDER_REMOTE_EN = (
    "[Reminder] Requests are limited, thinking is free. Think in detail: work out the whole "
    "solution - all files, the test, the code, docs. Accumulate all the edits and apply them "
    "in one call with the checks inside the script (a red run of the test before the code "
    "fix, the fix, run_tests(), if r.ok: go on); every extra call is a loss. Read in "
    "batches. Finish: last edits, run_tests(), run(\"ruff check ...\") and finish(\"summary\") "
    "- in one call; finish closes all steps, step_done() is optional."
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
    "condition in this one. finish() closes all task steps; finish() and ask_owner() end the "
    "script; step_done() is optional and does not: accepted - the script goes on, rejected - "
    "StepRejected with the reason and a stop. "
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
        "occurrences (the text lists match line numbers), a closed path, or new comments or "
        "docstrings in .py under src/ and tests/ - EditError, the edit is not applied",
    ),
    (
        "write",
        "def write(path: str, content: str) -> None: ...",
        "create or overwrite a file at once; a closed path or new comments or docstrings in .py "
        "under src/ and tests/ - EditError, the edit is not applied",
    ),
    (
        "run",
        "def run(command: str) -> Result: ...",
        "an allowed command without a shell (pytest..., ruff...); returns Result",
    ),
    (
        "run_tests",
        "def run_tests(paths: list[str]) -> Result: ...",
        "pytest over a list of test paths (path::test_name works); returns Result; if a test "
        "that existed in the task base fails, output gets a line telling to check the code, "
        "not the test",
    ),
    (
        "step_done",
        "def step_done(text: str) -> None: ...",
        "optional intermediate check of a step with summary text (finish() closes all steps "
        "itself); the guard runs the changed tests; "
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
        "note",
        "def note(text: str) -> None: ...",
        "append a note to the task notes file: it survives history folding and restarts and its "
        "text is shown to you at the start; write signatures, paths, expected values, decisions, "
        f"what is already done; limit {NOTES_LIMIT_CHARS} chars, when full the note is not "
        "written and a request to compress via notes_set() is printed",
    ),
    (
        "notes",
        "def notes() -> str: ...",
        "current text of the task notes",
    ),
    (
        "notes_set",
        "def notes_set(text: str) -> None: ...",
        "replace all notes at once, to compress them when they are full",
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
