"""Bounded retry and circuit breaking for dependency clients (REL-001).

Every external dependency the platform calls — the LLM, the retrieval index,
the embedding service — is reached through one bounded envelope so that an
outage fails within a documented budget instead of tying up workers until
something gives. The envelope has three layers, each with a single owner:

- **Timeouts.** The client applies the connect/read/write/pool deadlines of
  :class:`ResiliencePolicy` to every request, and the caller enforces the
  total deadline across the whole logical call, cancelling an in-flight
  attempt that overruns it. A request that exceeds its envelope is a failed
  attempt, never an unheld worker.
- **Retries.** Only :class:`FailureKind` values that describe an outage-shaped
  failure are retried, bounded by :attr:`RetryPolicy.max_attempts` with
  exponential backoff plus jitter. A contract failure (a malformed response, a
  non-``429`` refusal) is never retried: a second attempt cannot fix a shape
  change, and a broken release must not hammer the dependency.
- **Circuit breaking.** After :attr:`CircuitPolicy.failure_threshold`
  consecutive outage-shaped failures the breaker opens and the client fails
  fast without touching the network; after the cooldown one half-open probe
  attempt decides whether the dependency recovered.

Why hand-rolled rather than ``tenacity``: tenacity is a transitive dependency
of ``langchain-core``, not a declared one, and ADR-0001 keeps this package
stdlib-only. The piece of retry this platform needs is small, and the parts
that matter here — cancellation that is never swallowed, and circuit state that
is observable as a closed-vocabulary metric — are exactly the parts a generic
library exposes as hooks rather than guarantees.

Two invariants are structural rather than convention:

- ``asyncio.CancelledError`` passes straight through. A cancelled request must
  never be counted against the breaker, slept through, or retried.
- The breaker trips only on outage-shaped failures, never on a contract
  failure, so a broken release does not take a healthy dependency offline.

Observability follows the metric contract in :mod:`tenantchat.core.metrics`:
retry counts and circuit state carry identifiers and bounded enum values only.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, TypeVar

from tenantchat.core.errors import DomainError
from tenantchat.core.metrics import MetricLabelName, MetricName, MetricsReporter

T = TypeVar("T")


class Dependency(StrEnum):
    """The closed set of resilient dependencies, as a metric label vocabulary."""

    LLM = "llm"
    SEARCH = "search"
    EMBEDDING = "embedding"


class FailureKind(StrEnum):
    """How one attempt failed, classified for the retry decision.

    ``TIMEOUT``, ``CONNECT``, ``RESET``, ``RATE_LIMITED``, and ``SERVER_ERROR``
    are outage-shaped and retryable; ``MALFORMED`` and ``REFUSED`` are contract
    or policy failures that a retry cannot fix.
    """

    TIMEOUT = "timeout"
    CONNECT = "connect"
    RESET = "reset"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    MALFORMED = "malformed"
    REFUSED = "refused"


class CircuitState(StrEnum):
    """Where a breaker sits, as a metric label vocabulary.

    ``CLOSED`` passes traffic, ``OPEN`` refuses it during the cooldown, and
    ``HALF_OPEN`` admits a bounded probe that decides the next state.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


_RETRYABLE_KINDS: Final[frozenset[FailureKind]] = frozenset(
    {
        FailureKind.TIMEOUT,
        FailureKind.CONNECT,
        FailureKind.RESET,
        FailureKind.RATE_LIMITED,
        FailureKind.SERVER_ERROR,
    }
)


def is_retryable(kind: FailureKind) -> bool:
    """Whether a failure kind describes an outage a retry or fallback can fix.

    The one predicate the retry loop and the `AI-002` model-fallback chain both
    consult, so a new failure kind is either retryable everywhere or nowhere.
    """
    return kind in _RETRYABLE_KINDS


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retries with exponential backoff and per-attempt jitter."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    jitter_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("retry delays must be non-negative and ordered")
        if self.jitter_seconds < 0:
            raise ValueError("jitter must be non-negative")


@dataclass(frozen=True, slots=True)
class CircuitPolicy:
    """When the breaker opens and how it probes for recovery."""

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    half_open_attempts: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown must be non-negative")
        if self.half_open_attempts < 1:
            raise ValueError("half_open_attempts must be at least 1")


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    """The complete envelope one dependency client runs under.

    The connect/read/write/pool fields feed the transport's per-request
    deadlines; ``total_deadline_seconds`` is a hard wall-clock deadline across
    the whole logical call (every attempt and every backoff), enforced by
    cancelling an in-flight attempt that overruns it. ``retries`` and
    ``circuit`` bound how many attempts a logical call spends and how fast the
    client gives up on a dead dependency.
    """

    retries: RetryPolicy = field(default_factory=RetryPolicy)
    circuit: CircuitPolicy = field(default_factory=CircuitPolicy)
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 10.0
    total_deadline_seconds: float = 180.0

    def __post_init__(self) -> None:
        for name, value in (
            ("connect", self.connect_timeout_seconds),
            ("read", self.read_timeout_seconds),
            ("write", self.write_timeout_seconds),
            ("pool", self.pool_timeout_seconds),
            ("total_deadline", self.total_deadline_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name}_timeout_seconds must be positive")


class CircuitBreaker:
    """A per-dependency trip switch: CLOSED, OPEN, or HALF_OPEN.

    CLOSED passes every call and counts consecutive outage-shaped failures.
    Reaching :attr:`CircuitPolicy.failure_threshold` opens it; while OPEN,
    :meth:`allow` refuses everything until the cooldown has elapsed, then one
    probe is admitted as HALF_OPEN: a success closes the breaker, a failure
    reopens it. The breaker runs on one event loop, so its transitions are
    atomic between awaits.

    Args:
        policy: The open/cooldown/probe policy.
        clock: Injectable monotonic clock for deterministic tests; defaults to
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        policy: CircuitPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._opened_at: float = 0.0
        self._probes_in_flight: int = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow(self) -> bool:
        """Whether a call may go out now, possibly admitting a half-open probe."""
        if self._state is CircuitState.OPEN:
            if self._clock() - self._opened_at >= self._policy.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._probes_in_flight = 0
            else:
                return False
        if self._state is CircuitState.HALF_OPEN:
            if self._probes_in_flight >= self._policy.half_open_attempts:
                return False
            self._probes_in_flight += 1
            return True
        return True

    def on_success(self) -> None:
        """A call succeeded: close the breaker and reset the failure streak."""
        if self._state is CircuitState.HALF_OPEN:
            self._probes_in_flight = 0
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0

    def on_failure(self) -> None:
        """An outage-shaped failure occurred: count it, or reopen the breaker."""
        if self._state is CircuitState.HALF_OPEN:
            self._probes_in_flight = 0
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._policy.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()

    def on_non_retryable(self) -> None:
        """A contract or refusal failure: not evidence of an outage.

        The failure streak is left untouched, so an auth error neither counts
        toward opening the breaker nor clears a streak an outage is building.
        """
        if self._state is CircuitState.HALF_OPEN:
            self._probes_in_flight = 0

    def release_probe(self) -> None:
        """Give back a half-open probe slot that never resolved.

        ``allow`` reserves a slot for every admitted probe, and the outcome
        methods return it. The one exit path that calls none of them is
        cancellation — the caller aborts before any result exists — and a slot
        left reserved wedges the breaker HALF_OPEN for the process lifetime,
        because the cooldown recovery only runs from the OPEN branch. A cancelled
        probe must never count against the breaker, but the slot still has to be
        released; the caller invokes this from a ``finally``.
        """
        if self._state is CircuitState.HALF_OPEN and self._probes_in_flight > 0:
            self._probes_in_flight -= 1


class DependencyUnavailableError(DomainError):
    """The dependency was not reached: the breaker refused the call.

    Carries only a dependency identifier and a bounded reason, so it is safe to
    surface in logs and metrics. It is deliberately not raised when retries
    exhaust themselves — that path re-raises the underlying exception so a
    caller's timeout/error classification is preserved. A taxonomy member
    because a refused call is an expected failure, and expected failures are
    discoverable, coded, and safe to describe.
    """

    code = "dependency_unavailable"
    message = "A required dependency is not accepting requests right now."

    def __init__(self, *, dependency: Dependency, detail: str | None = None) -> None:
        self.dependency = dependency
        # Only the safe message reaches the exception args; the dependency
        # name is operator context and travels in `detail` through redaction.
        super().__init__(detail if detail is not None else f"{dependency.value} circuit open")


class AsyncResilientCaller:
    """Runs one logical dependency call under retry and circuit breaking.

    ``call`` performs a single attempt and raises on failure; the caller
    decides from ``classify``'s bounded kind whether to retry. On the last
    attempt the exception from ``call`` propagates unchanged, so the transport
    adapter keeps its existing error mapping.

    Args:
        dependency: Which dependency the call belongs to, for observability.
        policy: The retry and circuit envelope.
        classify: Maps an exception raised by ``call`` to a bounded
            :class:`FailureKind`.
        metrics: Optional reporter for retry counts and circuit state. When
            ``None``, nothing is recorded.
    """

    def __init__(
        self,
        *,
        dependency: Dependency,
        policy: ResiliencePolicy,
        classify: Callable[[Exception], FailureKind],
        metrics: MetricsReporter | None = None,
    ) -> None:
        self._dependency = dependency
        self._policy = policy
        self._classify = classify
        self._metrics = metrics
        self._breaker = CircuitBreaker(policy.circuit)
        self._observed_state: CircuitState | None = None

    @property
    def breaker(self) -> CircuitBreaker:
        """The breaker this caller drives; exposed for observability and tests."""
        return self._breaker

    async def run(self, call: Callable[[], Awaitable[T]]) -> T:
        """Execute ``call`` once per attempt until it succeeds or is exhausted.

        Each attempt runs under the remaining total deadline: an attempt that
        overruns it is cancelled and the logical call fails with ``TimeoutError``.
        ``TimeoutError`` classifies as non-retryable, so a spent budget never
        spawns another attempt.

        Raises:
            DependencyUnavailableError: the breaker refused the call, so no
                attempt was made.
            TimeoutError: the total deadline expired while an attempt was
                in flight.
            Exception: the exception of the final failed attempt.
        """
        self._observe_state()
        if not self._breaker.allow():
            raise DependencyUnavailableError(dependency=self._dependency)
        deadline = time.monotonic() + self._policy.total_deadline_seconds
        attempt = 0
        try:
            while True:
                attempt += 1
                try:
                    result = await asyncio.wait_for(call(), timeout=deadline - time.monotonic())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    kind = self._classify(exc)
                    if not is_retryable(kind):
                        self._breaker.on_non_retryable()
                        raise
                    self._breaker.on_failure()
                    self._observe_state()
                    if attempt >= self._policy.retries.max_attempts:
                        raise
                    delay = self._delay_for(attempt)
                    if deadline - time.monotonic() <= delay:
                        raise
                    self._observe_retry(kind)
                    await asyncio.sleep(delay)
                else:
                    self._breaker.on_success()
                    self._observe_state()
                    return result
        finally:
            # The outcome methods already returned a half-open probe slot; this
            # releases the one reserved by allow() on the cancellation path,
            # where no outcome method runs. A cancelled probe must never count
            # against the breaker, but the slot it held has to come back or the
            # breaker wedges HALF_OPEN for the process lifetime.
            self._breaker.release_probe()

    def _delay_for(self, attempt: int) -> float:
        backoff: float = min(
            self._policy.retries.max_delay_seconds,
            self._policy.retries.base_delay_seconds * (2 ** (attempt - 1)),
        )
        # Jitter is deliberately non-cryptographic: it exists to desynchronise
        # a herd of retries, and a secret-graded generator would make the delay
        # unobservable and the tests flaky.
        jitter: float = random.uniform(0.0, self._policy.retries.jitter_seconds)  # noqa: S311
        return backoff + jitter

    def _observe_retry(self, kind: FailureKind) -> None:
        if self._metrics is None:
            return
        self._metrics.observe(
            MetricName.DEPENDENCY_RETRIES,
            1.0,
            labels={
                MetricLabelName.DEPENDENCY.value: self._dependency.value,
                MetricLabelName.REASON.value: kind.value,
            },
        )

    def _observe_state(self) -> None:
        if self._metrics is None:
            return
        state = self._breaker.state
        if state is self._observed_state:
            return
        self._observed_state = state
        for candidate in CircuitState:
            self._metrics.observe(
                MetricName.CIRCUIT_STATE,
                1.0 if candidate is state else 0.0,
                labels={
                    MetricLabelName.DEPENDENCY.value: self._dependency.value,
                    MetricLabelName.STATE.value: candidate.value,
                },
            )
