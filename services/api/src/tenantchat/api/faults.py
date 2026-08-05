"""Failures the transport owns, rather than the domain.

``tenantchat.core.errors`` covers everything a business rule can refuse, and
:mod:`tenantchat.api.problems` maps those onto statuses. What is left is the
handful of failures that only exist because this is an HTTP service: nobody
authenticated, the caller may not use this route, a runtime the process needs is
not configured. Putting them in the domain taxonomy would give a worker error
codes about credentials it never sees.

They render as the same RFC 9457 documents, so a client parses one shape.
"""

from __future__ import annotations

from typing import ClassVar


class TransportError(Exception):
    """A failure with a fixed status, code, and safe message.

    Every attribute is a class constant. A fault carries no per-request text
    because that text is published to an unauthenticated caller, and the
    specifics — which header was missing, which role was presented — belong in
    the log line the handler writes instead.
    """

    status: ClassVar[int]
    code: ClassVar[str]
    message: ClassVar[str]


class UnauthenticatedError(TransportError):
    """No usable operator identity reached the service.

    Deliberately indistinguishable from a token that failed to verify: telling a
    caller which half of the check failed tells them what to fix.
    """

    status = 401
    code = "unauthenticated"
    message = "This route requires an authenticated operator."


class ForbiddenError(TransportError):
    """The identity is real and does not hold the required role."""

    status = 403
    code = "forbidden"
    message = "This operator role may not perform that operation."


class TenantAccessDeniedError(TransportError):
    """The identity is real and has no membership in the requested tenant.

    Deliberately indistinguishable from "no such tenant": the same document is
    returned whether the tenant exists or not, so an operator without access
    cannot probe for tenants they must not see.
    """

    status = 403
    code = "tenant_access_denied"
    message = "This operator has no access to the requested tenant."


class CsrfValidationError(TransportError):
    """A state-changing admin request arrived without a valid double-submit token."""

    status = 403
    code = "csrf_validation_failed"
    message = "The request did not carry a valid CSRF token."


class ChatUnavailableError(TransportError):
    """The deployment has no conversation runtime configured.

    Reachable until `AI-001` supplies the model adapter the runtime is composed
    over. A 503 rather than a 500: nothing is broken, the capability is absent,
    and a client is right to retry after the deployment gains one.
    """

    status = 503
    code = "chat_unavailable"
    message = "This deployment cannot answer chat turns."


class SearchIndexUnavailableError(TransportError):
    """The deployment has no retrieval index configured.

    The integrity check must read the index to detect faults, and a deployment
    without one cannot answer either way — it reports the capability as absent
    rather than claiming a clean bill of health.
    """

    status = 503
    code = "search_index_unavailable"
    message = "This deployment has no retrieval index."
