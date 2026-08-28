"""Domain error taxonomy: stable codes, and nothing sensitive in printable output.

Every check runs over the *discovered* taxonomy rather than a hand-maintained
list. A hand-maintained list silently stops covering a subclass the moment someone
forgets to add one, which is exactly when a duplicated code or a leaky message
would slip through.
"""

from __future__ import annotations

import pytest

import tenantchat.core as facade
from tenantchat.core.errors import (
    ConflictError,
    DomainError,
    InvalidContactError,
    InvalidVersionTransitionError,
    InvalidVisitorCredentialError,
    MissingRequiredFieldsError,
    NotFoundError,
    ReviewTransitionError,
    UnknownServiceError,
    VisitorCredentialRejection,
    WorkflowTransitionError,
)
from tenantchat.core.fields import RequiredField
from tenantchat.core.lifecycle import VersionState
from tenantchat.core.privacy import ConsentPurpose, ConsentRequiredError
from tenantchat.core.resilience import Dependency, DependencyUnavailableError
from tenantchat.core.workflows import WorkflowStatus, WorkflowTransition

# Strings that must never surface in printable output. Modelled on what really
# ends up in `detail`: connection strings, credentials, internal hostnames, and
# customer contact details pulled from an upstream payload.
SENSITIVE_DETAIL = (
    "postgresql://chat_app:hunter2@postgres.internal:5432/chat_app "
    "sam.lee@example.com +15552221919 Bearer sk-live-abc123"
)
SENSITIVE_MARKERS = (
    "hunter2",
    "postgres.internal",
    "sam.lee@example.com",
    "+15552221919",
    "sk-live-abc123",
)

# Constructor arguments for errors that require more than `detail`. An error added
# without an entry here fails `test_every_error_is_constructible` with a pointer
# to this table, rather than being quietly skipped.
EXTRA_ARGS: dict[type[DomainError], dict[str, object]] = {
    MissingRequiredFieldsError: {"fields": (RequiredField.CUSTOMER_NAME,)},
    UnknownServiceError: {"offered": ("HVAC", "Electrical")},
    InvalidVersionTransitionError: {
        "current": VersionState.DRAFT,
        "permitted": (VersionState.APPROVED,),
    },
    ConsentRequiredError: {"missing_purposes": (ConsentPurpose.BOOKING,)},
    DependencyUnavailableError: {"dependency": Dependency.LLM},
    WorkflowTransitionError: {
        "current": WorkflowStatus.ACTIVE,
        "transition": WorkflowTransition.RESUME,
        "permitted": frozenset({WorkflowTransition.PAUSE}),
    },
    ReviewTransitionError: {
        "current": "open",
        "permitted": frozenset({"in_review"}),
    },
}


def discover_errors() -> list[type[DomainError]]:
    """Every concrete error in the taxonomy, found by walking the class tree."""
    found: dict[str, type[DomainError]] = {DomainError.__name__: DomainError}

    def walk(cls: type[DomainError]) -> None:
        for subclass in cls.__subclasses__():
            found[subclass.__name__] = subclass
            walk(subclass)

    walk(DomainError)
    return sorted(found.values(), key=lambda item: item.__name__)


ALL_ERRORS = discover_errors()


def instantiate(error_type: type[DomainError]) -> DomainError:
    return error_type(detail=SENSITIVE_DETAIL, **EXTRA_ARGS.get(error_type, {}))


def test_discovery_finds_the_whole_taxonomy() -> None:
    """Guard against the parametrized checks below running on an empty set."""
    names = {error_type.__name__ for error_type in ALL_ERRORS}

    assert len(ALL_ERRORS) >= 10
    # Spot-check the subclasses a hand-written list had omitted, including the
    # two that live outside errors.py (R-45): the resilience breaker refusal
    # and the ports retrieval failure.
    assert {
        "PricingNotPermittedError",
        "LeadCaptureNotPermittedError",
        "ConflictError",
        "DependencyUnavailableError",
        "EvidenceUnavailableError",
    } <= names


def test_the_facade_exports_every_taxonomy_error() -> None:
    """R-48: the facade once exported 15 of the ~21 errors — no half state."""
    missing = [
        error_type.__name__
        for error_type in ALL_ERRORS
        if error_type.__name__ not in facade.__all__
        or getattr(facade, error_type.__name__, None) is not error_type
    ]

    assert not missing, f"facade does not export: {missing}"


@pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
def test_every_error_is_constructible(error_type: type[DomainError]) -> None:
    """A new error with required arguments must be registered in EXTRA_ARGS."""
    try:
        instantiate(error_type)
    except TypeError as exc:  # pragma: no cover - only on a taxonomy addition
        pytest.fail(
            f"{error_type.__name__} needs constructor arguments. "
            f"Add an EXTRA_ARGS entry in this module so it is covered. ({exc})"
        )


class TestPrintableOutputIsSafe:
    """`detail` must not reach any output produced by ordinary interpolation."""

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_str_does_not_leak_detail(self, error_type: type[DomainError]) -> None:
        rendered = str(instantiate(error_type))

        for marker in SENSITIVE_MARKERS:
            assert marker not in rendered

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_repr_does_not_leak_detail(self, error_type: type[DomainError]) -> None:
        rendered = repr(instantiate(error_type))

        for marker in SENSITIVE_MARKERS:
            assert marker not in rendered

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_str_is_the_safe_message(self, error_type: type[DomainError]) -> None:
        assert str(instantiate(error_type)) == error_type.message

    def test_repr_signals_that_detail_exists_without_disclosing_it(self) -> None:
        with_detail = repr(InvalidContactError(detail=SENSITIVE_DETAIL))
        without_detail = repr(InvalidContactError())

        assert "<redacted>" in with_detail
        assert "<redacted>" not in without_detail

    def test_detail_remains_reachable_for_structured_logging(self) -> None:
        """Operators opt in explicitly, which routes it through log redaction."""
        error = InvalidContactError(detail=SENSITIVE_DETAIL)

        assert error.detail == SENSITIVE_DETAIL

    def test_chained_cause_is_preserved(self) -> None:
        """Upstream context survives via `raise ... from`, not interpolation."""
        upstream = TimeoutError("calendar provider timed out")

        try:
            try:
                raise upstream
            except TimeoutError as exc:
                raise ConflictError(detail="reserve timed out") from exc
        except ConflictError as error:
            assert error.__cause__ is upstream


class TestCodesAndMessages:
    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_code_and_message_are_present(self, error_type: type[DomainError]) -> None:
        assert error_type.code
        assert error_type.message

    def test_codes_are_unique_across_the_whole_taxonomy(self) -> None:
        """Clients branch on `code`; a duplicate would make that ambiguous."""
        codes = [error_type.code for error_type in ALL_ERRORS]
        duplicates = {code for code in codes if codes.count(code) > 1}

        assert not duplicates, f"duplicate error codes: {sorted(duplicates)}"

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_message_carries_no_internal_vocabulary(self, error_type: type[DomainError]) -> None:
        """Public prose must not name internals or leak the failure's mechanism."""
        lowered = error_type.message.lower()

        for term in ("postgres", "elasticsearch", "traceback", "exception", "http", "sql"):
            assert term not in lowered


class TestTransportIndependence:
    """The domain must not encode transport decisions (see module docstring)."""

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_errors_carry_no_http_status(self, error_type: type[DomainError]) -> None:
        assert not hasattr(error_type, "http_status")

    @pytest.mark.parametrize("error_type", ALL_ERRORS, ids=lambda item: item.__name__)
    def test_errors_do_not_serialize_themselves(self, error_type: type[DomainError]) -> None:
        """Serialization belongs to the API layer's RFC 9457 mapping."""
        assert not hasattr(error_type, "public_payload")
        assert not hasattr(error_type, "to_json")


class TestSemanticFields:
    def test_missing_fields_are_closed_enum_members_not_free_strings(self) -> None:
        error = MissingRequiredFieldsError(fields=(RequiredField.CONTACT,))

        assert all(isinstance(field, RequiredField) for field in error.fields)

    def test_missing_fields_requires_at_least_one_entry(self) -> None:
        with pytest.raises(ValueError, match="at least one field"):
            MissingRequiredFieldsError(fields=())

    def test_unknown_service_carries_the_tenant_offered_names(self) -> None:
        error = UnknownServiceError(offered=("HVAC", "Electrical"))

        assert error.offered == ("HVAC", "Electrical")

    def test_rejected_transition_carries_both_states_as_enum_members(self) -> None:
        error = InvalidVersionTransitionError(
            current=VersionState.DRAFT, permitted=(VersionState.APPROVED, VersionState.PUBLISHED)
        )

        assert error.current is VersionState.DRAFT
        assert all(isinstance(state, VersionState) for state in error.permitted)

    def test_rejected_transition_requires_at_least_one_permitted_state(self) -> None:
        """An empty tuple would tell an operator console nothing to render."""
        with pytest.raises(ValueError, match="at least one permitted state"):
            InvalidVersionTransitionError(current=VersionState.DRAFT, permitted=())

    def test_not_found_message_does_not_confirm_resource_existence(self) -> None:
        """SEC-001: a cross-tenant read must not distinguish absent from forbidden."""
        lowered = NotFoundError.message.lower()

        assert "permission" not in lowered
        assert "forbidden" not in lowered
        assert "access" not in lowered


class TestVisitorCredentialRejectionContract:
    """R-47: the docstring promises `detail` never carries a rejection reason;
    these tests pin both halves of the contract on the type itself."""

    def test_the_rejection_reason_is_a_bounded_vocabulary_not_detail(self) -> None:
        error = InvalidVisitorCredentialError(VisitorCredentialRejection.BAD_SIGNATURE)

        assert error.reason is VisitorCredentialRejection.BAD_SIGNATURE
        assert error.detail is None

    def test_a_reasonless_rejection_stays_constructible(self) -> None:
        """Callers outside the signer (a missing credential header) refuse
        without inventing a reason."""
        error = InvalidVisitorCredentialError()

        assert error.reason is None

    def test_str_is_the_safe_message_whichever_reason_was_recorded(self) -> None:
        for reason in VisitorCredentialRejection:
            assert str(InvalidVisitorCredentialError(reason)) == (
                InvalidVisitorCredentialError.message
            )


class TestDependencyUnavailableTaxonomy:
    def test_the_breaker_refusal_reports_only_the_bounded_dependency(self) -> None:
        error = DependencyUnavailableError(dependency=Dependency.LLM)

        assert error.dependency is Dependency.LLM
        assert str(error) == DependencyUnavailableError.message
        assert error.detail == "llm circuit open"
