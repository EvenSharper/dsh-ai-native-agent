"""Cooperative cancellation shared by the Harness bridge and workspace."""
from threading import Event


class RunCancelled(BaseException):
    """Control flow, deliberately not swallowed by model/error recovery."""


def check_cancelled(event: Event | None) -> None:
    if event is not None and event.is_set():
        raise RunCancelled("Harness invocation cancelled")
