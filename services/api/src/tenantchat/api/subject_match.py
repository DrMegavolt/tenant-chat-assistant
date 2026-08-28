"""What counts as one data subject's contact data during privacy discovery.

Export and erasure both select their records through
``PrivacyStore.sessions_for_contact``, so the policy here decides which
sessions a rights request touches. It is deliberately conservative and shared
by every backend:

- A *contact-typed* value — the ``customer_phone_or_email`` argument the
  booking and lead tools carry through prompt and tool records — is parsed
  with the domain's :class:`Contact` and compared to the subject's canonical
  value exactly. A value that cannot parse as a contact is not a match.
- Conversation free text — the visitor's message as the transcript, the
  retrieval section, and the rendered prompt segments carry it; the answer;
  tool results; and verdict excerpts — is searched with the same recognition
  patterns the eraser and the promotion privacy check read, and every
  candidate is canonicalized through :class:`Contact` before the exact
  comparison. Discovery therefore finds exactly what :func:`anonymize_text`
  would erase, never more.

Nothing else is read. Turn-record JSON is parsed and walked section by
section, so timestamps, epoch values, manifest hashes, scores, and identifiers
can never contribute a digit or a substring to a match — the whole-document
scans this module replaced pulled unrelated sessions into export and, worse,
into erasure that way. A record or section whose shape is unrecognized matches
nothing: an inability to parse narrows what discovery selects and must never
widen it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final

from tenantchat.core.contact import EMAIL_IN_TEXT, PHONE_IN_TEXT, Contact, ContactKind

CONTACT_ARGUMENT_KEY: Final = "customer_phone_or_email"

_RETRIEVAL_TEXT_FIELDS: Final = ("query", "original_message", "resolved_query")
_OUTPUT_TEXT_FIELDS: Final = ("answer", "raw")
_VERDICT_TEXT_FIELDS: Final = ("claims_invalid", "output_invalid")


def text_holds_contact(text: str, contact: Contact) -> bool:
    """Whether conversation free text carries this exact subject contact.

    Candidates come from the domain's recognition patterns — the same ones
    :func:`anonymize_text` erases and the promotion privacy check refuses on —
    and each must parse as a contact equal to the subject's canonical value. A
    digit run embedded in a longer number therefore matches only when what it
    yields genuinely parses to the subject's number; a lookalike that fails to
    parse matches nothing.
    """
    pattern = PHONE_IN_TEXT if contact.kind is ContactKind.PHONE else EMAIL_IN_TEXT
    return any(
        (candidate := Contact.try_parse(candidate_text)) is not None and candidate == contact
        for candidate_text in pattern.findall(text)
    )


def contact_value_matches(raw: object, contact: Contact) -> bool:
    """Whether a contact-typed field's value is this subject's contact.

    The field's schema promises a contact, so the value is canonicalized the
    way :meth:`Contact.parse` canonicalizes at write time and compared for
    equality — never substring-matched, and never digit-normalized.
    """
    if not isinstance(raw, str):
        return False
    candidate = Contact.try_parse(raw)
    return candidate is not None and candidate == contact


def trace_holds_contact(content: object, contact: Contact) -> bool:
    """Whether one stored turn record carries the subject's contact data.

    ``content`` is the ``turn_records.content`` value as the driver decoded it.
    Only the sections the trace schema defines as conversation content are
    read: the retrieval section's copies of the visitor's message, the rendered
    prompt segments and the tool-call arguments they carry, the tool section's
    arguments and results, the published output, and the verdict excerpts.
    Metadata — versions, hashes, timestamps, scores, identifiers — and
    retrieved tenant documents are invisible to discovery. A record that is
    not the expected mapping, or a section whose shape does not conform, is
    not a match.
    """
    if not isinstance(content, Mapping):
        return False
    for section, fields in (
        (content.get("retrieval"), _RETRIEVAL_TEXT_FIELDS),
        (content.get("output"), _OUTPUT_TEXT_FIELDS),
    ):
        for text in _field_texts(section, fields):
            if text_holds_contact(text, contact):
                return True
    return (
        _prompt_holds_contact(content.get("prompt"), contact)
        or _tools_hold_contact(content.get("tools"), contact)
        or _verdicts_hold_contact(content.get("verdicts"), contact)
    )


def _field_texts(section: object, fields: tuple[str, ...]) -> Iterator[str]:
    if not isinstance(section, Mapping):
        return
    for field in fields:
        value = section.get(field)
        if isinstance(value, str):
            yield value


def _prompt_holds_contact(prompt: object, contact: Contact) -> bool:
    if not isinstance(prompt, Mapping):
        return False
    messages = prompt.get("messages")
    for message in messages if isinstance(messages, list) else ():
        if not isinstance(message, Mapping):
            continue
        segments = message.get("segments")
        for segment in segments if isinstance(segments, list) else ():
            # A rendered segment is [segment_id, region, text]; only the text
            # is content. Anything else in the list is not compared.
            if (
                isinstance(segment, list)
                and len(segment) > 2
                and isinstance(segment[2], str)
                and text_holds_contact(segment[2], contact)
            ):
                return True
        for call in _tool_calls(message):
            if _call_holds_contact(call, contact):
                return True
    return False


def _tools_hold_contact(tools: object, contact: Contact) -> bool:
    if not isinstance(tools, Mapping):
        return False
    for call in _tool_calls(tools):
        if _call_holds_contact(call, contact):
            return True
    results = tools.get("tool_results")
    for result in results if isinstance(results, list) else ():
        if isinstance(result, Mapping):
            value = result.get("result")
            if isinstance(value, str) and text_holds_contact(value, contact):
                return True
    return False


def _verdicts_hold_contact(verdicts: object, contact: Contact) -> bool:
    if not isinstance(verdicts, Mapping):
        return False
    for field in _VERDICT_TEXT_FIELDS:
        items = verdicts.get(field)
        for item in items if isinstance(items, list) else ():
            if isinstance(item, Mapping):
                value = item.get("value")
                if isinstance(value, str) and text_holds_contact(value, contact):
                    return True
    return False


def _tool_calls(section: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
    calls = section.get("tool_calls")
    for call in calls if isinstance(calls, list) else ():
        if isinstance(call, Mapping):
            yield call


def _call_holds_contact(call: Mapping[str, object], contact: Contact) -> bool:
    arguments = call.get("arguments")
    if not isinstance(arguments, Mapping):
        return False
    return contact_value_matches(arguments.get(CONTACT_ARGUMENT_KEY), contact)
