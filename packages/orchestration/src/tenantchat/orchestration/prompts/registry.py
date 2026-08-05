"""The versioned template registry: prompt artifacts are code, and code is reviewed.

Templates live in this repository as immutable :class:`TemplateVersion` values
and are never editable at runtime and never tenant-authored. A registry is
append-only: registering a changed template requires a new, strictly higher
version number, so a version already referenced by a stored turn record keeps
resolving to the exact artifact that version names — changing a template
produces a new version rather than mutating one.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tenantchat.core.tenant import TenantPolicy
from tenantchat.orchestration.model import PromptRegion
from tenantchat.orchestration.prompts.schema import BindingSchema

_TEMPLATE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

# A template binds its slots from these inputs; the callable is versioned code,
# not tenant data. `workflow` is the graph's checkpoint state, which later
# templates (the `AGENT-001` router) bind from.
Bindings = Callable[[TenantPolicy, Mapping[str, object]], Mapping[str, str]]


class TemplateRegistryError(ValueError):
    """The registry refused a registration or a lookup."""


@dataclass(frozen=True, slots=True)
class TemplateSegment:
    """One ordered piece of a template's text, with its ``{slot}`` placeholders.

    ``region`` is the trust marking the template's own text carries. Assembly
    renders the segment and then never promotes untrusted content into it:
    evidence and visitor turns become separate, always-untrusted segments.
    """

    segment_id: str
    region: PromptRegion
    text: str


@dataclass(frozen=True, slots=True)
class TemplateVersion:
    """One immutable version of a prompt template.

    Everything the template is — segment text, segment set, slot schema, and
    the bindings that fill the slots from policy — is fixed when the version is
    created. `FEAT-015` diffs two versions of the same template; it can do so
    because old versions are retained, never edited.
    """

    template_id: str
    version: int
    description: str
    segments: tuple[TemplateSegment, ...]
    schema: BindingSchema
    bindings: Bindings

    def __post_init__(self) -> None:
        if not _TEMPLATE_ID_RE.match(self.template_id):
            raise TemplateRegistryError(f"template id {self.template_id!r} is not a lowercase slug")
        if self.version < 1:
            raise TemplateRegistryError(f"version {self.version} is not positive")
        if not self.description.strip():
            raise TemplateRegistryError("template description is blank")
        seen: set[str] = set()
        for segment in self.segments:
            if segment.segment_id in seen:
                raise TemplateRegistryError(
                    f"duplicate segment id {segment.segment_id!r} in {self.template_id}"
                )
            seen.add(segment.segment_id)
            for placeholder in _PLACEHOLDER_RE.findall(segment.text):
                if self.schema.slot(placeholder) is None:
                    raise TemplateRegistryError(
                        f"segment {segment.segment_id!r} references undeclared slot "
                        f"{placeholder!r}"
                    )
            leftover = _PLACEHOLDER_RE.sub("", segment.text)
            if "{" in leftover or "}" in leftover:
                raise TemplateRegistryError(f"segment {segment.segment_id!r} has unbalanced braces")

    @property
    def ref(self) -> str:
        """The stable reference stored turn records attribute calls to."""
        return f"{self.template_id}@{self.version}"


class TemplateRegistry:
    """Append-only store of template versions, keyed by ``(template_id, version)``."""

    def __init__(self) -> None:
        self._versions: dict[str, dict[int, TemplateVersion]] = {}

    def register(self, version: TemplateVersion) -> None:
        """Add a version. It must be the next sequential version of its template.

        Raises:
            TemplateRegistryError: the ``(template_id, version)`` pair is
                already registered, or the version is not exactly one more than
                the highest registered — the registry is append-only, so a
                stored turn record's reference can never be repointed at
                different content.
        """
        versions = self._versions.setdefault(version.template_id, {})
        if version.version in versions:
            raise TemplateRegistryError(f"{version.ref} is already registered")
        highest = max(versions, default=0)
        if version.version != highest + 1:
            raise TemplateRegistryError(
                f"{version.ref} is not the next version after " f"{version.template_id}@{highest}"
            )
        versions[version.version] = version

    def current(self, template_id: str) -> TemplateVersion:
        """The highest registered version of a template.

        Raises:
            TemplateRegistryError: the template has no registered versions.
        """
        versions = self._versions.get(template_id)
        if not versions:
            raise TemplateRegistryError(f"no versions registered for {template_id!r}")
        return versions[max(versions)]

    def resolve(self, template_id: str, version: int) -> TemplateVersion:
        """An exact version, so a stored turn record can look up what it was.

        Raises:
            TemplateRegistryError: that version is not registered.
        """
        versions = self._versions.get(template_id, {})
        if version not in versions:
            raise TemplateRegistryError(f"{template_id}@{version} is not registered")
        return versions[version]

    def versions(self, template_id: str) -> tuple[TemplateVersion, ...]:
        """Every retained version of a template, ascending by version number."""
        return tuple(
            versions[number]
            for versions in (self._versions.get(template_id, {}),)
            for number in sorted(versions)
        )
