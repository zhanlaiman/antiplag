"""Request-local target length policy, entered only after owner authentication.

This removes a character allowance, not HTTP body, source, model or search
budgets. ContextVar keeps simultaneous guest workers under their normal limits.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_OWNER_TARGET = ContextVar("istok_owner_target", default=False)


def target_too_long(text, maximum):
    return not _OWNER_TARGET.get() and len(text) > maximum


@contextmanager
def owner_target_text(enabled):
    if type(enabled) is not bool:
        raise ValueError("Owner target policy requires a boolean")
    token = _OWNER_TARGET.set(enabled)
    try:
        yield
    finally:
        _OWNER_TARGET.reset(token)
