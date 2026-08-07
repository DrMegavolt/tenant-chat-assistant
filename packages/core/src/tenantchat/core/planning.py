"""Conversation-aware retrieval planning (`RAG-006`).

The retriever ranks one standalone query, and a conversation does not arrive as
one. This module turns the latest visitor message plus bounded prior context
into the standalone retrieval query for that turn: pronouns and ellipsis
("what about the other plan?", "and the estimate?") are resolved against what
the conversation already established, and context is dropped when the visitor
changes topic or corrects themselves.

**The trust boundary.** Prior visitor turns are untrusted text, and this module
is where the standalone-query rewrite the task warns about happens. Nothing
here may let prior text become an instruction. The planner is deterministic —
it never executes prior text and never asks a model to rewrite it — and the
only words that travel out of history into the resolved query are *known
terms*: a caller-supplied, server-approved vocabulary (service and product
names). A hostile prior turn may say anything it likes, but only a known term
is ever carried, so untrusted text cannot be laundered into the query. The
resolved query is consumed only by the retrieval scorer; it is never rendered
into a prompt region (`RAG-010`), trusted or untrusted.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from tenantchat.core.text import tokenize

PLANNER_VERSION = "query-planning@1"

# How much prior conversation the planner may consult. The referent of a
# pronoun is recent; history beyond this window is irrelevant to a follow-up.
MAX_HISTORY_TURNS = 8

# How many known terms carryover may add to a deictic query. Bounded so a long
# prior exchange cannot drown the follow-up's own words and push its lexical
# score below the abstention boundary.
MAX_CARRIED_TERMS = 2

# A visitor changing their mind. Strong markers only: a bare "actually" is
# ordinary speech ("Actually, what about the other plan?" is a continuation),
# so it must be paired with a replacement or a backtracking verb to count.
_CORRECTION_RE = re.compile(
    r"\b(?:i mean|i meant|scratch that|never ?mind|forget it|correction|"
    r"rather than|instead of|wait, actually|not the \w+)\b",
    re.IGNORECASE,
)

# A reference that needs the conversation to resolve: pronouns and deictics.
# Present in a message that carries no known term of its own is what triggers
# carryover. Plain question heads ("what does", "what is") are deliberately
# absent: "what does the warranty cover?" is self-anchored, not a referent.
_DEICTIC_RE = re.compile(
    r"\b(?:it|it's|they|them|that|those|this|these|this one|that one|the one|"
    r"the other|another|the same|same|both|such|one of|any of)\b"
    r"|\b(?:what about|how about|does it|is it|does that|is that|"
    r"can it|do they|are they|then|too|also)\b",
    re.IGNORECASE,
)

# A deictic head that explicitly continues the topic even when the message
# names a new thing of its own ("Do you clean screens too?"). Such a message
# is a continuation, never a topic switch.
_CONTINUATION_RE = re.compile(
    r"\b(?:too|also|what about|how about|and the|and then|then)\b", re.IGNORECASE
)


class PlanMode(StrEnum):
    """Why the query was resolved the way it was.

    ``TOPIC_SWITCH`` and ``CORRECTION`` drop the carried context, so the next
    turn starts from a clean topic; ``CARRYOVER`` continues it. ``DIRECT``
    means the message stood alone and the conversation changed nothing.
    """

    DIRECT = "direct"
    CARRYOVER = "carryover"
    CORRECTION = "correction"
    TOPIC_SWITCH = "topic_switch"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One authorized prior turn as the planner sees it.

    ``content`` is prior conversation text — untrusted when the role is
    ``user`` — and is treated as data to match known terms against, never as
    an instruction to follow.
    """

    role: str
    content: str

    def __post_init__(self) -> None:
        role = self.role.strip().casefold()
        if role not in ("user", "assistant"):
            raise ValueError(f"conversation turn role {self.role!r} is not user or assistant")
        object.__setattr__(self, "role", role)


@dataclass(frozen=True, slots=True)
class RetrievalPlan:
    """The resolved retrieval input for one turn, and why it looks that way.

    ``query`` is the standalone query handed to the retriever — the visitor's
    message plus any carried known terms. ``entities`` are the referenced
    known terms (services, products) this turn resolved, ``topic`` is the
    leading one, and ``history_used`` is how many prior turns were consulted,
    so "only bounded, relevant history was used" is checkable from the record.
    """

    tenant_id: str
    workflow: str
    query: str
    mode: PlanMode
    topic: str
    entities: tuple[str, ...]
    history_used: int
    reset: bool

    def to_dict(self) -> dict[str, object]:
        """The plan as JSON-safe data for the inference-plane trace."""
        return {
            "planner_version": PLANNER_VERSION,
            "tenant_id": self.tenant_id,
            "workflow": self.workflow,
            "query": self.query,
            "mode": self.mode.value,
            "topic": self.topic,
            "entities": list(self.entities),
            "history_used": self.history_used,
            "reset": self.reset,
        }


def plan_query(
    message: str,
    *,
    tenant_id: str,
    history: Sequence[ConversationTurn] = (),
    known_terms: Iterable[str] = (),
    workflow: str = "",
    max_history_turns: int = MAX_HISTORY_TURNS,
) -> RetrievalPlan:
    """Resolve one visitor message against bounded conversation state.

    The decision, in order:

    1. A correction drops the carried context and queries the message alone.
    2. A deictic message with no known term of its own carries the most recent
       known terms from history — this is the pronoun/ellipsis case.
    3. A message naming its own known terms is self-anchored and needs no
       carryover; if those terms are absent from the recent conversation it is
       a topic switch, which resets the context.
    4. Anything else is direct: the message as written.

    Deterministic: the same inputs produce the same plan, byte for byte.

    Raises:
        ValueError: a history turn carries a role other than ``user`` or
            ``assistant``.
    """
    bounded = history[-max_history_turns:]
    terms = _normalize(known_terms)
    context = _context(bounded, terms)
    message_terms = _matching(message, terms)
    message_deictic = _DEICTIC_RE.search(message) is not None

    if _CORRECTION_RE.search(message) is not None:
        return _build(
            message,
            tenant_id=tenant_id,
            workflow=workflow,
            mode=PlanMode.CORRECTION,
            topic=_topic(message_terms),
            entities=message_terms,
            reset=True,
            history_used=len(bounded),
        )

    if message_deictic and not message_terms:
        carried = context.recent[:MAX_CARRIED_TERMS]
        if carried:
            return _build(
                " ".join((message.strip(), *carried)),
                tenant_id=tenant_id,
                workflow=workflow,
                mode=PlanMode.CARRYOVER,
                topic=carried[0],
                entities=carried,
                reset=False,
                history_used=len(bounded),
            )
        # A deictic message with nowhere to resolve is direct: nothing is
        # carried, and it is not a topic switch either — it never had context.
        # ``message_deictic`` stays true so the switch rule below skips it.

    if message_terms:
        switch = (
            bool(context.terms)
            and context.terms.isdisjoint(message_terms)
            and not (_CONTINUATION_RE.search(message))
        )
        return _build(
            message,
            tenant_id=tenant_id,
            workflow=workflow,
            mode=PlanMode.TOPIC_SWITCH if switch else PlanMode.DIRECT,
            topic=_topic(message_terms),
            entities=message_terms,
            reset=switch,
            history_used=len(bounded),
        )

    # No known term and no deictic head: the message either continues the
    # current topic with ordinary words or moves to a fresh one. A message
    # whose content words share nothing with the last conversational text is a
    # switch, and the context does not carry forward.
    switch = (
        bool(bounded)
        and bool(context.last_text)
        and not message_deictic
        and bool(_words(message))
        and not bool(_words(message) & _words(context.last_text))
    )
    return _build(
        message,
        tenant_id=tenant_id,
        workflow=workflow,
        mode=PlanMode.TOPIC_SWITCH if switch else PlanMode.DIRECT,
        topic=_topic(message_terms),
        entities=message_terms,
        reset=switch,
        history_used=len(bounded),
    )


@dataclass(frozen=True, slots=True)
class _RecentContext:
    """The bounded conversation reduced to what carryover may use.

    ``terms`` is every known term found in the window, ``recent`` is those
    terms in recency order (newest turn first, deduplicated), and ``last_text``
    is the most recent conversational text.
    """

    terms: frozenset[str]
    recent: tuple[str, ...]
    last_text: str


def _context(turns: Sequence[ConversationTurn], terms: tuple[str, ...]) -> _RecentContext:
    """Scan the bounded turns newest-first for known terms and recent text.

    The recency order is what makes "the other plan" resolve to the Care Plan
    named in the last reply rather than the plan word from an earlier turn.
    """
    recent: list[str] = []
    seen: set[str] = set()
    last_text = ""
    for turn in reversed(turns):
        text = turn.content.strip()
        if text and not last_text:
            last_text = text
        for term in _matching(text, terms):
            if term not in seen:
                seen.add(term)
                recent.append(term)
    return _RecentContext(
        terms=frozenset(seen),
        recent=tuple(recent),
        last_text=last_text,
    )


def _normalize(raw: Iterable[str]) -> tuple[str, ...]:
    """Fold and validate the known terms.

    A term must survive tokenization with at least one content word, which
    rejects the pure-stopword strings that cannot anchor a query. Terms are
    sorted by length (descending) then lexically, so a caller's set or list
    order can never change the plan — the most specific multi-word term leads,
    and the result is identical across hash seeds.
    """
    folded = (str(term).strip().casefold() for term in raw)
    valid = tuple(term for term in folded if term and _words(term))
    return tuple(sorted(valid, key=lambda term: (-len(term), term)))


def _matching(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    """The terms that appear in ``text``, in the order given."""
    if not terms or not text.strip():
        return ()
    folded = text.casefold()
    patterns = _term_patterns(terms)
    return tuple(term for term in terms if patterns[term].search(folded))


@functools.lru_cache(maxsize=256)
def _term_patterns(terms: tuple[str, ...]) -> dict[str, re.Pattern[str]]:
    """One word-boundary pattern per known term, cached per vocabulary."""
    return {term: re.compile(rf"(?<!\w){re.escape(term)}(?!\w)") for term in terms}


def _words(text: str) -> frozenset[str]:
    """The text's content words, bounded to a length that can anchor a query."""
    return frozenset(word for word in tokenize(text) if len(word) >= 3)


def _topic(entities: Sequence[str]) -> str:
    return entities[0] if entities else "general"


def _build(
    query: str,
    *,
    tenant_id: str,
    workflow: str,
    mode: PlanMode,
    topic: str,
    entities: tuple[str, ...],
    reset: bool,
    history_used: int,
) -> RetrievalPlan:
    return RetrievalPlan(
        tenant_id=tenant_id,
        workflow=workflow,
        query=query,
        mode=mode,
        topic=topic,
        entities=entities,
        history_used=history_used,
        reset=reset,
    )
