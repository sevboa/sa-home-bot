from __future__ import annotations

from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS

UNREACHABLE_ERRORS = (*SERVICE_CALL_ERRORS, TimeoutError, OSError)
