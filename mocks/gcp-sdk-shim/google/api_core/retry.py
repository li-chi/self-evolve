"""Minimal `google.api_core.retry` shim (no real backoff needed offline)."""


class Retry:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, func):
        return func


def if_exception_type(*_types):
    return lambda _e: False
