"""Concrete :class:`~tenantchat.orchestration.model.ChatModel` adapters.

The graph depends on the protocol in ``tenantchat.orchestration.model``; this
package holds the providers that speak it, so a provider change lands here and
nowhere in the workflow code (`AI-001`). `AI-002`'s fallback chain and safe
response cache are the same kind of adapter, wrapping a provider rather than
speaking to one.
"""

from tenantchat.orchestration.providers.cache import CachingChatModel
from tenantchat.orchestration.providers.fallback import FallbackChatModel
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tenantchat.orchestration.providers.recording import MetricRecordingChatModel

__all__ = [
    "CachingChatModel",
    "FallbackChatModel",
    "MetricRecordingChatModel",
    "OpenAICompatibleChatModel",
]
