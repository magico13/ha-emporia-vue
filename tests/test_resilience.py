"""Tests for cloud-interruption tolerance helpers."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parents[1] / "custom_components" / "emporia_vue" / "resilience.py"
)
SPEC = spec_from_file_location("emporia_vue_resilience", MODULE_PATH)
assert SPEC and SPEC.loader
RESILIENCE = module_from_spec(SPEC)
SPEC.loader.exec_module(RESILIENCE)
TolerantUpdateMethod = RESILIENCE.TolerantUpdateMethod
is_newer_sample = RESILIENCE.is_newer_sample


class RecoverableError(Exception):
    """Test-only recoverable update failure."""


def test_retains_last_data_for_bounded_failures() -> None:
    """The last successful data remains visible during brief failures."""

    async def run_test() -> None:
        results: list[dict[str, float] | BaseException] = [
            {"power": 120.0},
            RecoverableError("timeout one"),
            RecoverableError("timeout two"),
            RecoverableError("timeout three"),
            {"power": 125.0},
            {"power": 125.0},
        ]

        async def update() -> dict[str, float]:
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        tolerant_update = TolerantUpdateMethod(
            update,
            name="minute telemetry",
            logger=logging.getLogger(__name__),
            recoverable_exceptions=(RecoverableError,),
            tolerated_failures=2,
        )
        listener_calls = 0

        def listener() -> None:
            nonlocal listener_calls
            listener_calls += 1

        remove_listener = tolerant_update.add_listener(listener)

        assert await tolerant_update() == {"power": 120.0}
        assert listener_calls == 1
        assert tolerant_update.last_attempt is not None
        assert tolerant_update.last_duration_ms is not None
        assert tolerant_update.last_duration_ms >= 0
        assert await tolerant_update() == {"power": 120.0}
        assert tolerant_update.consecutive_failures == 1
        assert tolerant_update.total_failures == 1
        assert tolerant_update.last_failure is not None
        assert await tolerant_update() == {"power": 120.0}
        assert tolerant_update.consecutive_failures == 2
        assert tolerant_update.total_failures == 2

        with pytest.raises(RecoverableError, match="timeout three"):
            await tolerant_update()

        assert tolerant_update.consecutive_failures == 3
        assert tolerant_update.total_failures == 3
        assert await tolerant_update() == {"power": 125.0}
        assert tolerant_update.consecutive_failures == 0
        assert listener_calls == 5

        remove_listener()
        assert await tolerant_update() == {"power": 125.0}
        assert listener_calls == 5

    asyncio.run(run_test())


def test_first_refresh_failure_is_not_hidden() -> None:
    """Setup still fails when no successful result has ever been cached."""

    async def run_test() -> None:
        async def update() -> dict[str, float]:
            raise RecoverableError("initial timeout")

        tolerant_update = TolerantUpdateMethod(
            update,
            name="minute telemetry",
            logger=logging.getLogger(__name__),
            recoverable_exceptions=(RecoverableError,),
            tolerated_failures=2,
        )

        with pytest.raises(RecoverableError, match="initial timeout"):
            await tolerant_update()

    asyncio.run(run_test())


def test_unexpected_failure_is_never_hidden() -> None:
    """Only explicitly recoverable failures reuse cached data."""

    async def run_test() -> None:
        results: list[dict[str, float] | BaseException] = [
            {"power": 120.0},
            RuntimeError("programming error"),
        ]

        async def update() -> dict[str, float]:
            result = results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        tolerant_update = TolerantUpdateMethod(
            update,
            name="minute telemetry",
            logger=logging.getLogger(__name__),
            recoverable_exceptions=(RecoverableError,),
            tolerated_failures=2,
        )

        assert await tolerant_update() == {"power": 120.0}
        with pytest.raises(RuntimeError, match="programming error"):
            await tolerant_update()

    asyncio.run(run_test())


def test_rejects_negative_tolerance() -> None:
    """A negative failure tolerance is invalid."""

    async def update() -> dict[str, float]:
        return {"power": 120.0}

    with pytest.raises(ValueError, match="must not be negative"):
        TolerantUpdateMethod(
            update,
            name="minute telemetry",
            logger=logging.getLogger(__name__),
            recoverable_exceptions=(RecoverableError,),
            tolerated_failures=-1,
        )


def test_only_integrates_newer_minute_samples() -> None:
    """Cached or out-of-order minute samples must not be counted twice."""
    timestamp = datetime(2026, 8, 4, 20, 0, tzinfo=UTC)
    integrated_samples = {"device-channel": timestamp}

    assert not is_newer_sample(integrated_samples, "device-channel", timestamp)
    assert not is_newer_sample(
        integrated_samples,
        "device-channel",
        timestamp - timedelta(minutes=1),
    )
    assert is_newer_sample(
        integrated_samples,
        "device-channel",
        timestamp + timedelta(minutes=1),
    )
    assert is_newer_sample(integrated_samples, "new-channel", timestamp)


def test_records_end_to_end_update_duration(monkeypatch) -> None:
    """Update duration is measured using a monotonic clock."""
    ticks = iter([10.0, 10.123])
    monkeypatch.setattr(RESILIENCE, "monotonic", lambda: next(ticks))

    async def run_test() -> None:
        async def update() -> dict[str, float]:
            return {"power": 120.0}

        tolerant_update = TolerantUpdateMethod(
            update,
            name="minute telemetry",
            logger=logging.getLogger(__name__),
            recoverable_exceptions=(RecoverableError,),
            tolerated_failures=2,
        )

        assert await tolerant_update() == {"power": 120.0}
        assert tolerant_update.last_duration_ms == pytest.approx(123.0)
        assert tolerant_update.last_attempt is not None

    asyncio.run(run_test())
