"""Domain error taxonomy: stable codes, and nothing sensitive in printable output.

Every check runs over the *discovered* taxonomy rather than a hand-maintained
list. A hand-maintained list silently stops covering a subclass the moment someone
forgets to add one, which is exactly when a duplicated code or a leaky message
would slip through.
"""

from __future__ import annotations

import pytest

from tenantchat.core.errors import (
    ConflictError,
    DomainError,
    InvalidContactError,
    MissingRequiredFieldsError,
    NotFoundError,
    UnknownServiceError,
)
from tenantchat.core.fields import RequiredField

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
    # Spot-check the three subclasses a hand-written list had omitted.
    assert {"PricingNotPermittedError", "LeadCaptureNotPermittedError", "ConflictError"} <= names


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

    def test_not_found_message_does_not_confirm_resource_existence(self) -> None:
        """SEC-001: a cross-tenant read must not distinguish absent from forbidden."""
        lowered = NotFoundError.message.lower()

        assert "permission" not in lowered
        assert "forbidden" not in lowered
        assert "access" not in lowered
