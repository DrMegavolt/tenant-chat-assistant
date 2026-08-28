"""Resolution of free-text service names against a tenant's offered services.

Matching is exact against a normalized alias index, deliberately boring, because
a mis-resolved service dispatches the wrong crew. Substring containment is the
tempting alternative and is wrong in both directions: ``"v"`` would resolve to
``"HVAC"`` and ``"cal"`` to ``"Electrical"``, while ``"electrical panel
replacement for my rental"`` would resolve to nothing.

**There is no fuzzy fallback.** Unresolved text returns ``None`` and the caller
asks a clarifying question. Guessing is strictly worse than asking: a wrong guess
produces a confidently incorrect booking, a question costs one conversational
turn. Aliases cover the real vocabulary mismatches.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_term(raw: str) -> str:
    """Fold a service term to its comparison key.

    Case-insensitive, punctuation-insensitive, and whitespace-collapsed, so
    ``"A/C  Repair!"`` and ``"ac repair"`` agree.
    """
    folded = _PUNCTUATION_RE.sub(" ", raw.casefold())
    return _WHITESPACE_RE.sub(" ", folded).strip()


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """One service a tenant offers.

    ``slug`` is the stable identifier used in storage, metrics, and provider
    mappings; ``display_name`` is what a customer sees. Renaming the display name
    must never change the slug, or historical bookings and metric series break.
    """

    slug: str
    display_name: str
    aliases: frozenset[str] = field(default_factory=frozenset)

    def match_keys(self) -> frozenset[str]:
        """Every normalized term that resolves to this service."""
        terms = {self.slug, self.display_name, *self.aliases}
        return frozenset(normalize_term(term) for term in terms if term.strip())


@dataclass(frozen=True, slots=True)
class ServiceCatalog:
    """A tenant's offered services, indexed for exact-match resolution.

    Every construction path validates ambiguity: the index is derived here, in
    ``__post_init__``, so the public constructor cannot produce a catalog
    :meth:`from_definitions` would have refused. ``_index`` is excluded from
    equality and hashing, so a catalog compares and hashes on its definitions
    alone instead of failing on an unhashable dict field.
    """

    definitions: tuple[ServiceDefinition, ...]
    _index: Mapping[str, ServiceDefinition] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        index: dict[str, ServiceDefinition] = {}
        for definition in self.definitions:
            for key in definition.match_keys():
                existing = index.get(key)
                if existing is not None and existing.slug != definition.slug:
                    raise ValueError(
                        f"ambiguous service term {key!r}: maps to both "
                        f"{existing.slug!r} and {definition.slug!r}"
                    )
                index[key] = definition
        # Frozen behind a mapping proxy, so a holder cannot mutate their way
        # past the exact-match guarantee the index was built for.
        object.__setattr__(self, "_index", MappingProxyType(index))

    @classmethod
    def from_definitions(cls, definitions: Iterable[ServiceDefinition]) -> ServiceCatalog:
        """Build a catalog, rejecting ambiguous configuration at construction time.

        A term that maps to two services is a tenant-configuration bug. Failing
        here surfaces it when the tenant is published (FEAT-006) rather than
        mid-conversation, where it would resolve non-deterministically. The
        ambiguity rule itself lives in ``__post_init__``, so this factory and
        the public constructor enforce one definition of it.
        """
        return cls(tuple(definitions))

    def resolve(self, raw: str) -> ServiceDefinition | None:
        """Resolve free text to exactly one service, or ``None`` to ask.

        ``None`` is a normal outcome, not an error. See the module docstring for
        why there is no fuzzy fallback.
        """
        if not raw.strip():
            return None
        return self._index.get(normalize_term(raw))

    def offered_names(self) -> tuple[str, ...]:
        """Display names, for clarifying questions and error payloads."""
        return tuple(definition.display_name for definition in self.definitions)

    def __contains__(self, raw: str) -> bool:
        return self.resolve(raw) is not None
