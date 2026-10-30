"""Retry with exponential backoff for flaky network/API calls."""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from loguru import logger

P = ParamSpec("P")
T = TypeVar("T")


def retry(
    exceptions: type[BaseException] | tuple[type[BaseException], ...] = Exception,
    tries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    max_delay: float = 30.0,
    jitter: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Retry the wrapped function on ``exceptions``.

    Waits ``delay``, ``delay*backoff``, ... (capped at ``max_delay``, plus jitter)
    between attempts and re-raises the last exception after ``tries`` attempts.
    """
    if tries < 1:
        raise ValueError("tries must be >= 1")

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            wait = delay
            for attempt in range(1, tries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == tries:
                        logger.error("{} failed after {} attempts: {}", func.__qualname__, tries, exc)
                        raise
                    pause = min(wait, max_delay) * (1 + random.uniform(-jitter, jitter))
                    logger.warning(
                        "{} attempt {}/{} failed ({}); retrying in {:.1f}s",
                        func.__qualname__, attempt, tries, exc, pause,
                    )
                    sleep(max(pause, 0))
                    wait *= backoff
            raise AssertionError("unreachable")  # pragma: no cover

        return wrapper

    return decorator


def call_with_retry(func: Callable[[], T], *, tries: int = 3, delay: float = 1.0, **kwargs) -> T:
    """Functional form: ``call_with_retry(lambda: api.get(...), tries=5)``."""
    return retry(tries=tries, delay=delay, **kwargs)(func)()
