"""Tests for core.draft_graveyard — drafts-graveyard surfacing (IRF-III-063).

Verifies thread keying (subject stem + sender domain, protocol-agnostic),
pending-draft marking with graveyard-first ordering, the red-badge JSON
field, and the hard autosend boundary for legal/government labels.
"""

from datetime import datetime, timezone, timedelta

import pytest

from core.models import EmailMessage
from core.research import ResearchDossier
from core.triage import TriageItem, render_triage, triage_messages
from core.voice import default_voice_profile
from core.draft_graveyard import (
    pending_draft_keys,
    thread_key,
    mark_pending_drafts,
    autosend_forbidden,
)

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _item(id, sender, subject, *, label="Awaiting Reply", requires_reply=True,
          pending_draft=False):
    msg = EmailMessage(
        id=id, sender=sender, subject=subject, body=body_text(requires_reply),
        date=NOW - timedelta(hours=2),
    )
    return TriageItem(
        message=msg,
        label=label,
        tier=2,
        base_tier=2,
        is_vip=False,
        age_hours=2,
        priority_score=50.0,
        dossier=ResearchDossier(requires_reply=requires_reply),
        pending_draft=pending_draft,
    )


def body_text(requires_reply: bool) -> str:
    if not requires_reply:
        return "Here is your quarterly statement."
    return "Could you please confirm by Thursday? I need the update before the deadline."


def _draft_msg(id, sender, subject, reply_prefix=""):
    return EmailMessage(
        id=id, sender=sender, subject=f"{reply_prefix}{subject}",
        body="draft body", date=NOW - timedelta(minutes=30),
    )


class TestThreadKey:
    def test_subject_stem_and_domain(self):
        m = _draft_msg("d1", "counsel@lawfirm.example", "Re: settlement offer")
        assert thread_key(m) == "lawfirm.example::settlement offer"

    def test_reply_prefixes_stripped(self):
        for prefix in ("Re:", "FW:", "FWD:", "re:", "回复:"):
            m = _draft_msg("d2", "x@acme.example", f"{prefix} budgets")
            assert thread_key(m) == "acme.example::budgets"

    def test_reply_prefix_inside_word_kept(self):
        m = _draft_msg("d3", "x@acme.example", "Please review the re:org plan")
        assert thread_key(m) == "acme.example::please review the re:org plan"

    def test_whitespace_and_case_normalized(self):
        m = _draft_msg("d4", "X@Acme.Example", "  Great   Plan  ")
        assert thread_key(m) == "acme.example::great plan"

    def test_sender_without_at_domain_uses_whole_sender(self):
        m = _draft_msg("d5", "internal-alias", "Vendors report")
        assert thread_key(m) == "internal-alias::vendors report"


class TestPendingDraftKeys:
    def test_keys_dedupe_across_draft_artifacts(self):
        drafts = [
            _draft_msg("a", "x@acme.example", "Review needed"),
            _draft_msg("b", "x@acme.example", "Re: Review needed"),
            _draft_msg("c", "y@other.example", "Review needed"),
        ]
        keys = pending_draft_keys(drafts)
        assert keys == {"acme.example::review needed", "other.example::review needed"}

    def test_blank_subject_ignored(self):
        m = EmailMessage(id="n", sender="x@acme.example", subject="",
                         body="", date=NOW)
        assert pending_draft_keys([m]) == set()


class TestMarkPendingDrafts:
    def setup_method(self):
        self.review = _item("t1", "x@acme.example", "Review needed")
        self.reply = _item("t2", "deals@store.example", "Warm thanks")

    def test_matches_thread_and_promotes_graveyard_first(self):
        items = mark_pending_drafts(
            [self.reply, self.review],  # reply first (higher score)
            {"acme.example::review needed"},
        )
        assert [i.message.id for i in items] == ["t1", "t2"]
        assert items[0].pending_draft is True
        assert items[1].pending_draft is False

    def test_no_match_keeps_order(self):
        items = mark_pending_drafts([self.reply, self.review], set())
        assert [i.message.id for i in items] == ["t2", "t1"]
        assert all(not i.pending_draft for i in items)

    def test_draft_generated_still_marked(self):
        msgs = [
            EmailMessage(id="a", sender="x@acme.example", subject="Review needed",
                         body=body_text(True), date=NOW - timedelta(hours=1)),
        ]
        items = triage_messages(msgs, voice=default_voice_profile(),
                                draft=True, now=NOW)
        assert items[0].suggested_draft  # non-legal thread may draft
        items = mark_pending_drafts(items, {"acme.example::review needed"})
        assert items[0].pending_draft is True


class TestRenderField:
    def test_json_renders_pending_draft(self):
        import json as _json
        items = [self._item_with_flag()]
        payload = _json.loads(render_triage(items, fmt="json"))
        assert payload[0]["pending_draft"] is True

    @staticmethod
    def _item_with_flag():
        return _item("t1", "x@acme.example", "Review needed",
                     pending_draft=True)


class TestAutosendForbidden:
    @pytest.mark.parametrize("label", [
        "Professional/Legal",
        "Legal/Contracts",
        "Legal",
        "Personal/Government",
        "Government/Tax",
        "Government",
    ])
    def test_forbidden_labels(self, label):
        assert autosend_forbidden(label) is True

    @pytest.mark.parametrize("label", [
        "Finance/Banking",
        "Finance/Payments",
        "Dev/GitHub",
        "Personal/Health",
        "Misc/Other",
        "Awaiting Reply",
    ])
    def test_allowed_labels(self, label):
        assert autosend_forbidden(label) is False

    def test_graveyard_clears_suggested_draft_for_forbidden_label(self):
        legal = _item("t1", "counsel@lawfirm.example", "Settlement",
                      label="Professional/Legal")
        legal.suggested_draft = "You should sign this."
        items = mark_pending_drafts(
            [legal], {"lawfirm.example::settlement"})
        assert items[0].pending_draft is True
        assert items[0].suggested_draft is None

    def test_graveyard_keeps_draft_for_allowed_label(self):
        allowed = _item("t2", "x@acme.example", "Review needed")
        allowed.suggested_draft = "Happy to help."
        items = mark_pending_drafts(
            [allowed], {"acme.example::review needed"})
        assert items[0].pending_draft is True
        assert items[0].suggested_draft == "Happy to help."