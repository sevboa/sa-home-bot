from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from sa_home_bot.node.fixups.core import FixupError
from sa_home_bot.utils.requirements import install_argv

SUDOERS_DIR = Path("/etc/sudoers.d")
SBIN_FALLBACK_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def find_executable(name: str, *, exclude_dir: str | None = None) -> str | None:
    if exclude_dir is None:
        found = shutil.which(name)
    else:
        dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d != exclude_dir]
        found = shutil.which(name, path=os.pathsep.join(dirs))
    if found is not None:
        return found
    for directory in SBIN_FALLBACK_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_sudo(argv: list[str]) -> None:
    try:
        result = subprocess.run(["sudo", *argv])
    except OSError as exc:
        raise FixupError(f"не удалось запустить sudo {' '.join(argv)}: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(f"sudo {' '.join(argv)} завершился кодом {result.returncode}")


def run_user(argv: list[str], **kwargs: object) -> None:
    try:
        result = subprocess.run(argv, **kwargs)
    except OSError as exc:
        raise FixupError(f"не удалось запустить {' '.join(argv)}: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(f"{' '.join(argv)} завершился кодом {result.returncode}")


def path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except PermissionError:
        pass
    try:
        result = subprocess.run(["sudo", "test", "-e", str(path)])
    except OSError as exc:
        raise FixupError(f"не удалось проверить {path} через sudo: {exc}") from exc
    return result.returncode == 0


def read_text(path: Path) -> str:
    result = subprocess.run(["sudo", "cat", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise FixupError(f"не удалось прочитать {path} через sudo: {result.stderr.strip()}")
    return result.stdout


def read_text_if_exists(path: Path) -> str | None:
    return read_text(path) if path_exists(path) else None


def file_matches(path: Path, expected: str) -> bool:
    if not path_exists(path):
        return False
    try:
        return read_text(path) == expected
    except FixupError:
        return False


@contextlib.contextmanager
def temp_file(content: str, suffix: str = "") -> Iterator[Path]:
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


def install_from(source: Path, dest: Path, *, mode: str, make_parents: bool = False) -> None:
    run_sudo(
        [
            "install",
            *(["-D"] if make_parents else []),
            "-m",
            mode,
            "-o",
            "root",
            "-g",
            "root",
            str(source),
            str(dest),
        ]
    )


def install_root_file(path: Path, content: str, *, mode: str, make_parents: bool = False) -> None:
    with temp_file(content) as tmp:
        install_from(tmp, path, mode=mode, make_parents=make_parents)


def install_executable(source: Path, dest: Path | str) -> None:
    run_sudo(["install", "-m", "0755", str(source), str(dest)])


def install_sudoers_snippet(name: str, content: str) -> None:
    visudo = find_executable("visudo")
    if visudo is None:
        raise FixupError("visudo не найден (проверьте установку пакета sudo)")
    with temp_file(content, ".sudoers") as tmp:
        try:
            check = subprocess.run([visudo, "-cf", str(tmp)], capture_output=True, text=True)
        except OSError as exc:
            raise FixupError(f"не удалось запустить visudo: {exc}") from exc
        if check.returncode != 0:
            raise FixupError(f"visudo отверг сниппет {name}: {check.stderr.strip()}")
        install_from(tmp, SUDOERS_DIR / name, mode="0440")


def install_package(package: str, no_manager_message: str) -> None:
    argv = install_argv(package)
    if argv is None:
        raise FixupError(no_manager_message)
    run_sudo(argv)


def daemon_reload() -> None:
    run_sudo(["systemctl", "daemon-reload"])


def enable_now(unit_name: str) -> None:
    run_sudo(["systemctl", "enable", "--now", unit_name])


def unit_is_active(unit_name: str, *, user: bool = False) -> bool:
    argv = ["systemctl", *(["--user"] if user else []), "is-active", "--quiet", unit_name]
    return subprocess.run(argv).returncode == 0


def replace_unit(path: Path, content: str) -> None:
    current = read_text_if_exists(path)
    if current == content:
        return
    if current is not None:
        run_sudo(["systemctl", "stop", path.name])
    install_root_file(path, content, mode="0644")
    daemon_reload()


def download(url: str, dest: Path, timeout: int) -> None:
    run_user(["curl", "-fsSL", url, "-o", str(dest)], timeout=timeout)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
