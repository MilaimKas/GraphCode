"""Helpers of the sample package.

Covers: a decorator whose inner function is a nested definition
(``timed.<locals>.wrapper``), a private module-level function.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any


def timed(function: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate ``function`` so its run time is stored on the wrapper."""

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        result = function(*args, **kwargs)
        wrapper.last_duration = _elapsed_since(start)
        return result

    return wrapper


def _elapsed_since(start: float) -> float:
    return time.perf_counter() - start
