from __future__ import annotations

from sa_home_bot.proto.messages import ERR_UNAVAILABLE, ERR_UNKNOWN_DST, ProtoError


class ServiceUnavailableError(RuntimeError):
    pass


SERVICE_CALL_ERRORS = (ServiceUnavailableError, ProtoError)


def is_service_unavailable(exc: Exception) -> bool:
    if isinstance(exc, ServiceUnavailableError):
        return True
    return isinstance(exc, ProtoError) and exc.code in (ERR_UNAVAILABLE, ERR_UNKNOWN_DST)
