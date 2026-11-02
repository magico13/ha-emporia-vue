"""Helpers for tolerating brief Emporia cloud interruptions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Generic, TypeVar, cast

_DataT = TypeVar("_DataT")
_MISSING = object()


def is_newer_sample(
    integrated_samples: dict[str, datetime],
    identifier: str,
    timestamp: datetime,
) -> bool:
    """Return whether a minute sample is newer than the last integrated one."""
    previous_timestamp = integrated_samples.get(identifier)
    return previous_timestamp is None or timestamp > previous_timestamp


class TolerantUpdateMethod(Generic[_DataT]):
    """Reuse the last successful result during a bounded run of failures."""

    def __init__(
        self,
        update_method: Callable[[], Awaitable[_DataT]],
        *,
        name: str,
        logger: logging.Logger,
        recoverable_exceptions: tuple[type[BaseException], ...],
        tolerated_failures: int,
    ) -> None:
        """Initialize a timeout-tolerant update method."""
        if tolerated_failures < 0:
            raise ValueError("tolerated_failures must not be negative")

        self._update_method = update_method
        self._name = name
        self._logger = logger
        self._recoverable_exceptions = recoverable_exceptions
        self._tolerated_failures = tolerated_failures
        self._consecutive_failures = 0
        self._total_failures = 0
        self._last_failure: datetime | None = None
        self._last_attempt: datetime | None = None
        self._last_duration_ms: float | None = None
        self._last_data: _DataT | object = _MISSING
        self._listeners: set[Callable[[], None]] = set()

    @property
    def consecutive_failures(self) -> int:
        """Return the current number of consecutive recoverable failures."""
        return self._consecutive_failures

    @property
    def total_failures(self) -> int:
        """Return the total recoverable failures since initialization."""
        return self._total_failures

    @property
    def last_failure(self) -> datetime | None:
        """Return the timestamp of the most recent recoverable failure."""
        return self._last_failure

    @property
    def last_attempt(self) -> datetime | None:
        """Return the timestamp of the most recent update attempt."""
        return self._last_attempt

    @property
    def last_duration_ms(self) -> float | None:
        """Return the duration of the most recent update attempt."""
        return self._last_duration_ms

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a listener for retry telemetry changes."""
        self._listeners.add(listener)

        def remove_listener() -> None:
            self._listeners.discard(listener)

        return remove_listener

    def _notify_listeners(self) -> None:
        """Notify retry telemetry listeners without disrupting updates."""
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # pylint: disable=broad-exception-caught
                self._logger.exception("Error notifying %s retry listener", self._name)

    async def __call__(self) -> _DataT:
        """Fetch fresh data or briefly retain the last successful result."""
        started = monotonic()
        try:
            data = await self._update_method()
        except self._recoverable_exceptions as err:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure = datetime.now(UTC)
            if (
                self._last_data is _MISSING
                or self._consecutive_failures > self._tolerated_failures
            ):
                raise

            self._logger.warning(
                "%s update failed (%d/%d tolerated); retaining the last "
                "successful data: %s",
                self._name,
                self._consecutive_failures,
                self._tolerated_failures,
                err,
            )
            return cast(_DataT, self._last_data)
        else:
            if self._consecutive_failures:
                self._logger.info(
                    "%s update recovered after %d failed attempt(s)",
                    self._name,
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            self._last_data = data
            return data
        finally:
            self._last_duration_ms = (monotonic() - started) * 1000
            self._last_attempt = datetime.now(UTC)
            self._notify_listeners()
