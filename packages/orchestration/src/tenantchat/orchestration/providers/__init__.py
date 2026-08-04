"""Concrete :class:`~tenantchat.orchestration.model.ChatModel` adapters.

The graph depends on the protocol in ``tenantchat.orchestration.model``; this
package holds the providers that speak it, so a provider change lands here and
nowhere in the workflow code (`AI-001`).
"""

from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel

__all__ = ["OpenAICompatibleChatModel"]
