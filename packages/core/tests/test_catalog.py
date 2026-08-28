"""Service resolution: exact, alias-aware, and deliberately non-fuzzy."""

from __future__ import annotations

import pytest

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition, normalize_term


@pytest.fixture
def catalog() -> ServiceCatalog:
    return ServiceCatalog.from_definitions(
        [
            ServiceDefinition(
                slug="hvac",
                display_name="HVAC",
                aliases=frozenset({"heating", "cooling", "air conditioning", "ac", "furnace"}),
            ),
            ServiceDefinition(
                slug="electrical",
                display_name="Electrical",
                aliases=frozenset({"electric", "wiring", "panel"}),
            ),
            ServiceDefinition(
                slug="window-cleaning",
                display_name="Window Cleaning",
                aliases=frozenset({"windows"}),
            ),
        ]
    )


class TestNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("HVAC", "hvac"),
            ("  Window   Cleaning  ", "window cleaning"),
            ("A/C Repair!", "a c repair"),
            ("Air-Conditioning", "air conditioning"),
        ],
    )
    def test_folds_case_punctuation_and_whitespace(self, raw: str, expected: str) -> None:
        assert normalize_term(raw) == expected


class TestResolution:
    @pytest.mark.parametrize("raw", ["HVAC", "hvac", "  hvac  ", "Hvac"])
    def test_resolves_display_name_case_insensitively(
        self, catalog: ServiceCatalog, raw: str
    ) -> None:
        resolved = catalog.resolve(raw)

        assert resolved is not None
        assert resolved.slug == "hvac"

    @pytest.mark.parametrize(
        ("raw", "slug"),
        [
            ("air conditioning", "hvac"),
            ("furnace", "hvac"),
            ("wiring", "electrical"),
            ("windows", "window-cleaning"),
        ],
    )
    def test_resolves_configured_aliases(
        self, catalog: ServiceCatalog, raw: str, slug: str
    ) -> None:
        resolved = catalog.resolve(raw)

        assert resolved is not None
        assert resolved.slug == slug

    def test_resolves_slug_directly(self, catalog: ServiceCatalog) -> None:
        resolved = catalog.resolve("window-cleaning")

        assert resolved is not None
        assert resolved.display_name == "Window Cleaning"


class TestNoSubstringMatching:
    """Substring containment, pinned as forbidden.

    Under `value in canonical or canonical in value` every case below resolves
    to a real service without complaint, and dispatches a crew for it.
    """

    @pytest.mark.parametrize("raw", ["v", "a", "c", "va", "ele", "clean"])
    def test_fragments_do_not_resolve(self, catalog: ServiceCatalog, raw: str) -> None:
        assert catalog.resolve(raw) is None

    def test_longer_phrase_containing_a_service_does_not_resolve(
        self, catalog: ServiceCatalog
    ) -> None:
        """Resolution is not intent extraction.

        Pulling a service out of a sentence is the router's job (AGENT-001), with
        the conversation as context. Doing it here with `in` would also match
        "my HVAC guy said not to call you", which is not a booking request.
        """
        assert catalog.resolve("I think my HVAC is broken") is None

    @pytest.mark.parametrize("raw", ["", "   ", "plumbing", "roof repair"])
    def test_unknown_and_empty_terms_return_none(self, catalog: ServiceCatalog, raw: str) -> None:
        assert catalog.resolve(raw) is None


class TestConfigurationValidation:
    def test_ambiguous_alias_across_services_is_rejected_at_build_time(self) -> None:
        """A term mapping to two services would resolve non-deterministically."""
        with pytest.raises(ValueError, match="ambiguous service term"):
            ServiceCatalog.from_definitions(
                [
                    ServiceDefinition("hvac", "HVAC", frozenset({"repair"})),
                    ServiceDefinition("electrical", "Electrical", frozenset({"repair"})),
                ]
            )

    def test_the_public_constructor_validates_ambiguity_too(self) -> None:
        """R-44: `from_definitions` was the only validated path, so a catalog
        built through the public constructor could resolve arbitrarily."""
        with pytest.raises(ValueError, match="ambiguous service term"):
            ServiceCatalog(
                (
                    ServiceDefinition("hvac", "HVAC", frozenset({"repair"})),
                    ServiceDefinition("electrical", "Electrical", frozenset({"repair"})),
                )
            )

    def test_alias_colliding_with_another_display_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ambiguous service term"):
            ServiceCatalog.from_definitions(
                [
                    ServiceDefinition("hvac", "HVAC"),
                    ServiceDefinition("electrical", "Electrical", frozenset({"hvac"})),
                ]
            )

    def test_repeated_term_within_one_service_is_allowed(self) -> None:
        """Slug, display name, and alias may coincide; that is not ambiguity."""
        built = ServiceCatalog.from_definitions(
            [ServiceDefinition("hvac", "hvac", frozenset({"HVAC"}))]
        )

        assert built.resolve("hvac") is not None


class TestCatalogValueSemantics:
    def test_a_catalog_is_hashable(self) -> None:
        """R-44: the dict field once made the frozen dataclass unhashable."""
        catalog = ServiceCatalog.from_definitions([ServiceDefinition("hvac", "HVAC")])

        assert len({catalog, catalog}) == 1
        assert hash(catalog) == hash(
            ServiceCatalog.from_definitions([ServiceDefinition("hvac", "HVAC")])
        )

    def test_catalogs_with_equal_definitions_are_equal(self) -> None:
        first = ServiceCatalog.from_definitions([ServiceDefinition("hvac", "HVAC")])
        second = ServiceCatalog.from_definitions([ServiceDefinition("hvac", "HVAC")])

        assert first == second


class TestCatalogInterface:
    def test_offered_names_preserves_definition_order(self, catalog: ServiceCatalog) -> None:
        assert catalog.offered_names() == ("HVAC", "Electrical", "Window Cleaning")

    def test_contains_delegates_to_resolve(self, catalog: ServiceCatalog) -> None:
        assert "furnace" in catalog
        assert "plumbing" not in catalog
