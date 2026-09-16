"""Drafts-graveyard surfacing.

The dominant real-inbox failure mode for reply automation is a polished draft
that was written and never sent. Those drafts sit in the Drafts folder while
the thread is still flagged for follow-up, so triage keeps presenting the same
stale thread as if nothing had been done.

This module detects that state: it takes triaged items plus the set of
"draft" artifacts known to exist on the thread (from ``in:draft`` queries or
provider draft searches) and marks the items whose thread has an unanswered
draft — the drafts-graveyard. The CLI then ranks them as the top class.

It also owns the hard auto-send boundary for draft artifacts: legal and
government threads must never be auto-sent, mirroring the send-policy guard
(``core.send_policy``) at the draft-surfacing layer.

Public API:
    thread_key(item) -> str
    mark_pending_drafts(items, draft_keys) -> list[TriageItem]
    autosend_forbidden(label) -> bool
"""

from __future__ import annotations

import re
from typing import Iterable, List, Set

from core.models import EmailMessage
from core.triage import TriageItem

_REPLY_PREFIX = re.compile(r"^(re|fw|fwd|回复|答复)\s*:\s*", re.IGNORECASE)

# Categories that must never be auto-sent, even when the global send policy
# permits automated sending. Mirrors the operator-owned boundary in
# core/send_policy.py and the envelope doctrine's human-review lanes.
_FORBIDDEN_AUTOSEND_LABELS = frozenset({
    "Professional/Legal",
    "Personal/Government",
})
_FORBIDDEN_AUTOSEND_PREFIXES = ("Legal", "Government")


def _normalize_subject(subject: str) -> str:
    stem = _REPLY_PREFIX.sub("", subject.strip())
    return re.sub(r"\s+", " ", stem).lower()


def thread_key(message: EmailMessage) -> str:
    """Canonical identity of a thread for draft matching.

    Keyed on the normalized subject stem (reply prefixes stripped) plus the
    sender's domain, so a draft created from the same thread links back to it
    even when the draft artifact has its own message id and timestamp.
    """
    sender = (message.sender or "").lower()
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else sender
    return f"{domain}::{_normalize_subject(message.subject)}"


def pending_draft_keys(messages: Iterable[EmailMessage]) -> Set[str]:
    """Project a set of thread keys from draft artifacts.

    ``messages`` are the raw artifacts returned by an ``in:draft`` scan (or
    provider draft search). Thread identity is inferred from subject stem and
    sender domain rather than native ids, so this works across Gmail, IMAP and
    Exchange without protocol-specific draft parsing.
    """
    return {thread_key(m) for m in messages if m.subject}


def mark_pending_drafts(items: List[TriageItem],
                        draft_keys: Iterable[str]) -> List[TriageItem]:
    """Tag items whose thread has an unanswered draft (drafts-graveyard).

    The input list is sorted by priority already; the returned list promotes
    graveyard items to the front (stable within each class), so drafted-but-
    unsent threads surface as the top work class. Items are marked in place
    and the new ordering is returned.
    """
    keys = set(draft_keys)
    graveyard: List[TriageItem] = []
    rest: List[TriageItem] = []
    for item in items:
        item.pending_draft = thread_key(item.message) in keys
        if item.pending_draft and autosend_forbidden(item.label):
            item.suggested_draft = None  # surface, never auto-send legal/gov
        (graveyard if item.pending_draft else rest).append(item)
    return graveyard + rest


def autosend_forbidden(label: str) -> bool:
    """True when a thread's label forbids automated sending of a draft."""
    if label in _FORBIDDEN_AUTOSEND_LABELS:
        return True
    return any(label.startswith(p) for p in _FORBIDDEN_AUTOSEND_PREFIXES)