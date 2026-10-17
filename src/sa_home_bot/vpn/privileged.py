from __future__ import annotations

import shutil

from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_NEEDS_PRIVILEGE, ProtoError
from sa_home_bot.utils.process import run
from sa_home_bot.utils.requirements import looks_like_sudo_denied


async def sudo_stdout(program: str, *args: str, needs_privilege_text: str) -> str:
    path = shutil.which(program) or program
    result = await run(["sudo", "-n", path, *args])
    if result.returncode != 0:
        if looks_like_sudo_denied(result.stderr_text):
            raise ProtoError(ERR_NEEDS_PRIVILEGE, needs_privilege_text)
        raise ProtoError(
            ERR_INTERNAL,
            f"{program} {' '.join(args)} завершился ошибкой: {result.stderr_text}",
        )
    return result.stdout.decode()
