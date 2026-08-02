"""HTTP edge for the tenant chat platform.

This package translates HTTP into domain calls and domain results back into HTTP.
Business rules live in ``tenantchat.core``; what belongs here is request parsing,
bounds, authentication wiring, serialization, and the mapping from the domain
error taxonomy onto RFC 9457 responses.

The test for whether code belongs here: if removing FastAPI would delete a rule
the business depends on, that rule is in the wrong package.
"""
