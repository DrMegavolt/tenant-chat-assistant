"""The tenant precondition every authoritative write shares.

Kept in one place because the check is a security boundary, not a convenience:
each adapter that writes a tenant-owned row must refuse a tenant that is absent
or suspended, and a second hand-written copy is how one of them ends up checking
only for existence.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tenantchat.core.errors import NotFoundError


async def require_active_tenant(connection: AsyncConnection, tenant_id: str) -> None:
    """Refuse work for a tenant that is absent, suspended, or disabled.

    Raises:
        NotFoundError: deliberately without distinguishing which, so a caller
            cannot probe for the existence of tenants it may not see.
    """
    result = await connection.execute(
        text("SELECT id FROM tenants WHERE id = :tenant_id AND status = 'active'"),
        {"tenant_id": tenant_id},
    )
    if result.first() is None:
        raise NotFoundError(detail="tenant absent or inactive")
