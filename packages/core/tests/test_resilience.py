"""The REL-001 resilience toolkit: retry, circuit breaking, and observability.

These tests pin the two structural invariants the toolkit exists for: a
cancelled request is never counted or retried, and a breaker trips only on
outage-shaped failures — never on a contract failure. The retry budget is
bounded (``max_attempts``), backoff is exponential with jitter under a cap, and
every retry and circuit transition lands on the metrics port as identifiers and
bounded enum values, never content.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

import pytest

from tenantchat.core.metrics import MetricName
from tenantchat.core.resilience import (
    AsyncResilientCaller,
    CircuitBreaker,
    CircuitPolicy,
    Dependency,
    DependencyUnavailableError,
    FailureKind,
    ResiliencePolicy,
    RetryPolicy,
)


class _BoomError(Exception):
    """One classified failure kind, so tests control the retry decision."""

    def __init__(self, kind: FailureKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def _classify(exc: Exception) -> FailureKind:
    if isinstance(exc, _BoomError):
        return exc.kind
    return FailureKind.REFUSED


class _RecordingMetrics:
    """Collects every observation the toolkit emits, as (name, value, labels)."""

    def __init__(self) -> None:
        self.observations: list[tuple[str, float, Mapping[str, str]]] = []

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.observations.append((name.value, value, dict(labels or {})))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _policy(
    *,
    max_attempts: int = 3,
    base_delay: float = 0.0,
    jitter: float = 0.0,
    threshold: int = 5,
    cooldown: float = 30.0,
) -> ResiliencePolicy:
    return ResiliencePolicy(
        retries=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay,
            max_delay_seconds=8.0,
            jitter_seconds=jitter,
        ),
        circuit=CircuitPolicy(failure_threshold=threshold, cooldown_seconds=cooldown),
    )


class TestCircuitBreaker:
    def test_starts_closed_and_resets_the_streak_on_success(self) -> None:
        breaker = CircuitBreaker(CircuitPolicy(failure_threshold=3, cooldown_seconds=10))

        assert breaker.state.value == "closed"
        assert breaker.allow() is True
        breaker.on_failure()
        breaker.on_failure()
        assert breaker.consecutive_failures == 2
        breaker.on_success()
        assert breaker.state.value == "closed"
        assert breaker.consecutive_failures == 0

    def test_trips_open_when_the_failure_threshold_is_reached(self) -> None:
        breaker = CircuitBreaker(CircuitPolicy(failure_threshold=3, cooldown_seconds=10))

        breaker.on_failure()
        breaker.on_failure()
        assert breaker.state.value == "closed"
        breaker.on_failure()
        assert breaker.state.value == "open"
        assert breaker.allow() is False

    def test_opens_again_after_the_cooldown_only_when_a_probe_fails(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=2, cooldown_seconds=10), clock=clock
        )
        for _ in range(2):
            breaker.on_failure()
        assert breaker.state.value == "open"

        clock.now += 5
        assert breaker.allow() is False

        clock.now += 5
        assert breaker.allow() is True
        assert breaker.state.value == "half_open"
        breaker.on_failure()
        assert breaker.state.value == "open"

    def test_a_successful_probe_closes_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=2, cooldown_seconds=10), clock=clock
        )
        for _ in range(2):
            breaker.on_failure()
        clock.now += 10

        assert breaker.allow() is True
        breaker.on_success()

        assert breaker.state.value == "closed"
        assert breaker.allow() is True

    def test_non_retryable_failures_never_count_toward_opening(self) -> None:
        breaker = CircuitBreaker(CircuitPolicy(failure_threshold=3, cooldown_seconds=10))

        for _ in range(10):
            breaker.on_non_retryable()

        assert breaker.state.value == "closed"
        assert breaker.consecutive_failures == 0

    def test_half_open_admits_only_one_probe_at_a_time(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=1, cooldown_seconds=10, half_open_attempts=1),
            clock=clock,
        )
        breaker.on_failure()
        clock.now += 10

        assert breaker.allow() is True
        assert breaker.state.value == "half_open"
        assert breaker.allow() is False

    def test_non_retryable_probe_failure_frees_the_probe_slot(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=1, cooldown_seconds=10, half_open_attempts=1),
            clock=clock,
        )
        breaker.on_failure()
        clock.now += 10

        assert breaker.allow() is True
        breaker.on_non_retryable()
        assert breaker.state.value == "half_open"
        assert breaker.allow() is True

    def test_a_cancelled_probe_releases_its_slot_so_the_breaker_recovers(self) -> None:
        """A probe that is cancelled mid-flight must not wedge the breaker.

        ``allow()`` reserves a slot when it admits a HALF_OPEN probe, and only
        the outcome methods return it. A cancelled probe calls none of them, so
        without an explicit release the breaker sits HALF_OPEN with a held slot
        for the process lifetime and refuses every later call — a transient
        outage becomes permanent. This pins the reproduction: probe cancelled,
        then the very next probe is admitted and a success closes the breaker.
        """
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=1, cooldown_seconds=10, half_open_attempts=1),
            clock=clock,
        )
        breaker.on_failure()
        clock.now += 10

        # The probe is admitted and cancelled before any outcome method runs.
        assert breaker.allow() is True
        assert breaker.state.value == "half_open"
        breaker.release_probe()

        # The recovery probe is admitted and a success closes the breaker.
        assert breaker.allow() is True
        breaker.on_success()
        assert breaker.state.value == "closed"
        assert breaker.allow() is True

    def test_release_probe_is_a_no_op_outside_a_held_half_open_probe(self) -> None:
        """Releasing when nothing is held must not corrupt the probe count."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            CircuitPolicy(failure_threshold=1, cooldown_seconds=10, half_open_attempts=1),
            clock=clock,
        )
        breaker.on_failure()
        clock.now += 10

        breaker.release_probe()
        assert breaker.allow() is True
        breaker.release_probe()
        assert breaker.allow() is True


class TestPolicyValidation:
    @pytest.mark.parametrize(
        "factory",
        [
            lambda: RetryPolicy(max_attempts=0),
            lambda: RetryPolicy(base_delay_seconds=-1),
            lambda: RetryPolicy(max_delay_seconds=0, base_delay_seconds=1),
            lambda: RetryPolicy(jitter_seconds=-0.1),
            lambda: CircuitPolicy(failure_threshold=0),
            lambda: CircuitPolicy(cooldown_seconds=-1),
            lambda: CircuitPolicy(half_open_attempts=0),
            lambda: ResiliencePolicy(connect_timeout_seconds=0),
            lambda: ResiliencePolicy(total_deadline_seconds=-5),
        ],
    )
    def test_a_malformed_envelope_is_refused(self, factory: Callable[[], object]) -> None:
        with pytest.raises(ValueError):
            factory()


class TestAsyncResilientCaller:
    def test_retries_retryable_failures_then_raises_the_last_exception(self) -> None:
        attempts: list[int] = []

        async def call() -> str:
            attempts.append(1)
            raise _BoomError(FailureKind.TIMEOUT)

        caller = AsyncResilientCaller(
            dependency=Dependency.LLM, policy=_policy(max_attempts=3), classify=_classify
        )
        with pytest.raises(_BoomError):
            asyncio.run(caller.run(call))
        assert len(attempts) == 3

    def test_recovers_on_a_later_attempt(self) -> None:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise _BoomError(FailureKind.SERVER_ERROR)
            return "ok"

        caller = AsyncResilientCaller(
            dependency=Dependency.SEARCH, policy=_policy(max_attempts=4), classify=_classify
        )
        assert asyncio.run(caller.run(call)) == "ok"
        assert attempts == 3

    def test_contract_failures_are_not_retried(self) -> None:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            raise _BoomError(FailureKind.MALFORMED)

        caller = AsyncResilientCaller(
            dependency=Dependency.LLM, policy=_policy(max_attempts=3), classify=_classify
        )
        with pytest.raises(_BoomError):
            asyncio.run(caller.run(call))
        assert attempts == 1
        assert caller.breaker.state.value == "closed"

    def test_cancellation_is_not_retried_or_counted(self) -> None:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(10)
            return "late"

        caller = AsyncResilientCaller(
            dependency=Dependency.EMBEDDING, policy=_policy(max_attempts=3), classify=_classify
        )

        async def scenario() -> None:
            task = asyncio.create_task(caller.run(call))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert attempts == 1
        assert caller.breaker.state.value == "closed"

    def test_a_cancelled_half_open_probe_does_not_wedge_the_breaker(self) -> None:
        """A probe cancelled mid-flight releases its slot (review finding 1).

        The trigger is routine: FastAPI cancels a request task on client
        disconnect, and disconnects are most likely during recovery when calls
        are slow. Before the fix the breaker was left HALF_OPEN with the probe
        slot held, so every later call to the dependency raised
        DependencyUnavailableError until a restart.
        """
        attempts: list[str] = []

        async def failing() -> str:
            raise _BoomError(FailureKind.CONNECT)

        async def hanging() -> str:
            attempts.append("probe")
            await asyncio.sleep(10)
            return "late"

        async def healthy() -> str:
            attempts.append("recovery")
            return "ok"

        caller = AsyncResilientCaller(
            dependency=Dependency.LLM,
            policy=_policy(max_attempts=1, threshold=1, cooldown=0.05),
            classify=_classify,
        )
        # Trip the breaker.
        with pytest.raises(_BoomError):
            asyncio.run(caller.run(failing))
        assert caller.breaker.state.value == "open"

        async def scenario() -> None:
            # Let the cooldown elapse, then admit a probe and cancel it.
            await asyncio.sleep(0.06)
            task = asyncio.create_task(caller.run(hanging))
            await asyncio.sleep(0.01)
            assert caller.breaker.state.value == "half_open"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        # The dependency is healthy and the next call must reach it and recover.
        assert asyncio.run(caller.run(healthy)) == "ok"
        assert attempts == ["probe", "recovery"]
        assert caller.breaker.state.value == "closed"

    def test_an_open_breaker_fails_fast_without_calling(self) -> None:
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            raise _BoomError(FailureKind.CONNECT)

        caller = AsyncResilientCaller(
            dependency=Dependency.SEARCH,
            policy=_policy(max_attempts=1, threshold=2),
            classify=_classify,
        )
        for _ in range(2):
            with pytest.raises(_BoomError):
                asyncio.run(caller.run(call))
        assert caller.breaker.state.value == "open"

        with pytest.raises(DependencyUnavailableError):
            asyncio.run(caller.run(call))
        assert attempts == 2

    def test_records_retries_and_circuit_state_as_bounded_labels(self) -> None:
        metrics = _RecordingMetrics()
        attempts = 0

        async def call() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise _BoomError(FailureKind.TIMEOUT)
            return "ok"

        caller = AsyncResilientCaller(
            dependency=Dependency.LLM,
            policy=_policy(max_attempts=3, threshold=5),
            classify=_classify,
            metrics=metrics,
        )
        assert asyncio.run(caller.run(call)) == "ok"

        retries = [
            observation
            for observation in metrics.observations
            if observation[0] == MetricName.DEPENDENCY_RETRIES.value
        ]
        assert len(retries) == 2
        assert all(labels == {"dependency": "llm", "reason": "timeout"} for _, _, labels in retries)
        states = {
            labels["state"]: value
            for name, value, labels in metrics.observations
            if name == MetricName.CIRCUIT_STATE.value and labels["dependency"] == "llm"
        }
        assert states == {"closed": 1.0, "open": 0.0, "half_open": 0.0}

    def test_backoff_grows_exponentially_but_is_capped_and_jittered(self) -> None:
        caller = AsyncResilientCaller(
            dependency=Dependency.LLM,
            policy=_policy(base_delay=1.0, jitter=0.5, max_attempts=8),
            classify=_classify,
        )

        first = caller._delay_for(1)
        second = caller._delay_for(2)
        fourth = caller._delay_for(4)
        fifth = caller._delay_for(5)

        assert 1.0 <= first <= 1.5
        assert 2.0 <= second <= 2.5
        assert 8.0 <= fourth <= 8.5
        assert 8.0 <= fifth <= 8.5
