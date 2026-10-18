from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from sa_home_bot.node.fixups.core import FixupError
from sa_home_bot.node.fixups.privileged import (
    download,
    file_sha256,
    find_executable,
    install_executable,
    install_package,
    run_sudo,
    run_user,
)

GO_MIN_VERSION = (1, 25)
AMNEZIAWG_GO_REPO = "https://github.com/amnezia-vpn/amneziawg-go"
AMNEZIAWG_TOOLS_REPO = "https://github.com/amnezia-vpn/amneziawg-tools"
LOCAL_BIN = Path("/usr/local/bin")

XRAY_VERSION = "v26.3.27"
XRAY_ZIP_URL = (
    f"https://github.com/XTLS/Xray-core/releases/download/{XRAY_VERSION}/Xray-linux-64.zip"
)
XRAY_BIN_PATH = LOCAL_BIN / "xray"

MTG_VERSION = "2.2.8"
MTG_SHA256 = "7ef19d079d85f4e00d4f8334ec1f3f3c8718e3d0ed1f3109ea9a8673138a2102"
MTG_URL = (
    f"https://github.com/9seconds/mtg/releases/download/v{MTG_VERSION}/"
    f"mtg-{MTG_VERSION}-linux-amd64.tar.gz"
)
MTG_BIN_PATH = LOCAL_BIN / "mtg"


def _go_version_ok(go_bin: str) -> bool:
    try:
        result = subprocess.run([go_bin, "version"], capture_output=True, text=True)
    except OSError:
        return False
    match = re.search(r"go(\d+)\.(\d+)", result.stdout)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= GO_MIN_VERSION


def _ensure_go(build_dir: Path) -> str:
    system_go = find_executable("go")
    if system_go is not None and _go_version_ok(system_go):
        return system_go
    version_probe = subprocess.run(
        ["curl", "-fsSL", "https://go.dev/VERSION?m=text"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version_probe.returncode != 0 or not version_probe.stdout.strip():
        raise FixupError(
            f"не удалось узнать актуальную версию Go: {version_probe.stderr.strip()}"
        )
    go_version = version_probe.stdout.splitlines()[0].strip()
    tarball = build_dir / "go.tar.gz"
    download(f"https://go.dev/dl/{go_version}.linux-amd64.tar.gz", tarball, timeout=180)
    run_user(["tar", "-C", str(build_dir), "-xzf", str(tarball)])
    return str(build_dir / "go" / "bin" / "go")


def _clone_and_make(repo: str, build_dir: Path, name: str, subdir: str, env: dict) -> Path:
    src = build_dir / name
    run_user(["git", "clone", "--depth", "1", repo, str(src)])
    run_user(["make", "-C", str(src / subdir) if subdir else str(src)], env=env)
    return src


def build_amneziawg_tools() -> None:
    for prog in ("git", "curl", "tar"):
        if find_executable(prog) is None:
            raise FixupError(f"{prog} не найден — установите вручную и повторите nodectl fix")
    if find_executable("gcc") is None or find_executable("make") is None:
        install_package(
            "build-essential", "gcc/make не найдены и неизвестен пакетный менеджер для build-essential"
        )

    with tempfile.TemporaryDirectory(prefix="sa-home-awg-build-") as build_dir_str:
        build_dir = Path(build_dir_str)
        go_bin = _ensure_go(build_dir)
        env = dict(os.environ)
        env["PATH"] = f"{Path(go_bin).parent}:{env.get('PATH', '')}"

        if find_executable("amneziawg-go") is None:
            src = _clone_and_make(AMNEZIAWG_GO_REPO, build_dir, "amneziawg-go", "", env)
            install_executable(src / "amneziawg-go", LOCAL_BIN / "amneziawg-go")

        if find_executable("awg") is None:
            src = _clone_and_make(AMNEZIAWG_TOOLS_REPO, build_dir, "amneziawg-tools", "src", env)
            run_sudo(["make", "-C", str(src / "src"), "install"])


def _xray_version_ok() -> bool:
    xray_path = find_executable("xray")
    if xray_path is None:
        return False
    result = subprocess.run([xray_path, "version"], capture_output=True, text=True)
    return result.returncode == 0 and XRAY_VERSION.lstrip("v") in result.stdout.splitlines()[:1][0]


def ensure_xray_binary() -> None:
    if _xray_version_ok():
        return
    if find_executable("unzip") is None:
        install_package(
            "unzip", "unzip не найден и неизвестен пакетный менеджер для его установки"
        )
    with tempfile.TemporaryDirectory(prefix="sa-home-xray-build-") as build_dir_str:
        build_dir = Path(build_dir_str)
        zip_path = build_dir / "xray.zip"
        dgst_path = build_dir / "xray.dgst"
        download(XRAY_ZIP_URL, zip_path, timeout=180)
        download(f"{XRAY_ZIP_URL}.dgst", dgst_path, timeout=30)
        want = next(
            (
                line.split("=", 1)[1].strip()
                for line in dgst_path.read_text().splitlines()
                if line.startswith("SHA2-256=")
            ),
            None,
        )
        if not want:
            raise FixupError("не удалось прочитать SHA2-256 из .dgst xray-core")
        got = file_sha256(zip_path)
        if want != got:
            raise FixupError(f"sha256 архива xray-core не совпал: ждали {want}, получили {got}")
        run_user(["unzip", "-o", str(zip_path), "xray", "-d", str(build_dir)])
        install_executable(build_dir / "xray", XRAY_BIN_PATH)


def install_mtg() -> None:
    if MTG_BIN_PATH.is_file():
        return
    with tempfile.TemporaryDirectory(prefix="sa-home-mtg-") as build_dir_str:
        build_dir = Path(build_dir_str)
        tarball = build_dir / "mtg.tar.gz"
        download(MTG_URL, tarball, timeout=60)
        digest = file_sha256(tarball)
        if digest != MTG_SHA256:
            raise FixupError(
                f"mtg-{MTG_VERSION}-linux-amd64.tar.gz: sha256 не совпал "
                f"(ждали {MTG_SHA256}, получили {digest}) — установка прервана"
            )
        run_user(["tar", "-C", str(build_dir), "-xzf", str(tarball)])
        extracted = build_dir / f"mtg-{MTG_VERSION}-linux-amd64" / "mtg"
        if not extracted.is_file():
            raise FixupError(f"после распаковки не нашёлся бинарь mtg ({extracted})")
        install_executable(extracted, MTG_BIN_PATH)
    setcap = find_executable("setcap")
    if setcap is None:
        raise FixupError("setcap не найден (пакет libcap2-bin)")
    run_sudo([setcap, "cap_net_bind_service=+ep", str(MTG_BIN_PATH)])
