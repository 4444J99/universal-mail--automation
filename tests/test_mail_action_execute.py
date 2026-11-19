"""Bounded execution of approved mail actions (R3, issue #229) tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.historical_intelligence import build_historical_intelligence
from core.mail_action_execute import (
    MAIL_ACTION_EXECUTE_SCHEMA,
    MailActionExecuteError,
    build_execution_request,
    execute_action,
    load_execution_receipts,
)
from core.mail_action_plan import build_action_plan
from core.models import FlagColor, MessageReference
from providers.flag_codecs import mailapp_index_from_flag
from tests.test_archive_transactions import Provider as ArchiveProvider
from tests.test_archive_transactions import setup as archive_setup
from tests.test_commit6_transactions import Harness

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "historical"
OPS_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ops"


def _write_intelligence(tmp_path: Path) -> Path:
    history = tmp_path / "history.json"
    ops_report = tmp_path / "latest.json"
    shutil.copyfile(HISTORICAL_FIXTURE_DIR / "latest.json", history)
    shutil.copyfile(OPS_FIXTURE_DIR / "latest.json", ops_report)
    shutil.copyfile(OPS_FIXTURE_DIR / "latest-actions.md", tmp_path / "latest-actions.md")
    intelligence = build_historical_intelligence(history, ops_report_path=ops_report)
    target = tmp_path / "latest-intelligence.json"
    target.write_text(json.dumps(intelligence), encoding="utf-8")
    return target


def _plan(tmp_path: Path):
    return build_action_plan(_write_intelligence(tmp_path))


def _item(plan, approval_type):
    for item in plan["items"]:
        if item["approval_type"] == approval_type:
            return item
    raise AssertionError(f"no {approval_type} item in fixture plan")


def _approval(action_id, reason_code="ready_to_send"):
    return {"decision": "approved", "reason_code": reason_code,
            "actor": "operator", "action_id": action_id}


def _flag_request(plan, item, harness, mutations):
    from core import flag_workflow as fw

    kwargs = {}
    if len(mutations) > 3:
        # The engine's canary call requires a canary-sized approval.
        canary = fw.ApprovalReceipt.create(
            plan_hash=harness.plan["plan_hash"],
            snapshot_sha256=harness.plan["snapshot_sha256"],
            policy_sha256=harness.plan["policy_sha256"],
            approved_mutation_ids=[m.mutation_id for m in harness.mutations[:3]],
            approving_operator="operator",
            canary_limit=3)
        kwargs["flag_canary_approval"] = canary.to_dict()
    return build_execution_request(
        plan, action_id=item["id"], account_id="acct",
        approval=_approval(item["id"]), mutations=mutations,
        flag_plan=harness.plan, flag_approval=harness.approval.to_dict(), **kwargs)


def _flag_mutations(harness):
    return [{
        "mutation_id": m.mutation_id,
        "kind": "flag",
        "account_id": "acct",
        "provider": "mailapp",
        "evidence_digest": m.evidence_digest,
    } for m in harness.mutations]


def _starred_details(provider):
    def details(ref):
        message = provider.messages[ref.provider_id]
        return SimpleNamespace(is_starred=message.native != -1)
    return details


class FakeDirectProvider:
    """Scoped surface + label/draft writes over in-memory messages."""

    name = "mailapp"

    def __init__(self, messages):
        # messages: pid -> {"mailbox", "received", "sender", "subject"}
        self.messages = dict(messages)
        self.calls = []
        self.drafts = []

    def _digest(self, message):
        return MessageReference.compute_evidence_digest(
            message["received"], message["sender"], message["subject"])

    def resolve_scoped(self, ref):
        self.calls.append(("resolve_scoped", ref.provider_id))
        message = self.messages[ref.provider_id]
        return MessageReference(
            provider=ref.provider, account=ref.account, mailbox=ref.mailbox,
            provider_id=ref.provider_id, received_iso=message["received"],
            sender_digest=None, subject_digest=None,
            observed_native_flag=-1,
            evidence_digest=self._digest(message),
            snapshot_id=ref.snapshot_id)

    def get_message_details_ref(self, ref):
        self.calls.append(("get_message_details_ref", ref.provider_id))
        message = self.messages[ref.provider_id]
        return SimpleNamespace(
            is_starred=False, labels={message["mailbox"]})

    def apply_label(self, message_id, label):
        self.calls.append(("apply_label", message_id, label))
        self.messages[message_id]["mailbox"] = label
        return True

    def create_draft(self, to_addr, subject, body, account=None):
        self.calls.append(("create_draft", to_addr, subject))
        self.drafts.append({"to_addr": to_addr, "subject": subject,
                            "body": body, "account": account})
        return True


def _direct_messages(pids=("1", "2", "3", "4")):
    return {pid: {"mailbox": "INBOX", "received": "2026-07-01T00:00:00",
                  "sender": "billing@corp.example",
                  "subject": f"Subject {pid}"} for pid in pids}


def _direct_mutations(provider, pids, kinds=("label",)):
    out = []
    for pid in pids:
        message = provider.messages[pid]
        digest = provider._digest(message)
        for kind in kinds:
            if kind == "label":
                out.append({
                    "mutation_id": f"label-{pid}",
                    "kind": "label",
                    "account_id": "acct",
                    "provider": "mailapp",
                    "evidence_digest": digest,
                    "message": {"provider_id": pid, "mailbox": "INBOX",
                                "received_iso": message["received"]},
                    "label": "Dev/GitHub",
                })
            else:
                out.append({
                    "mutation_id": f"draft-{pid}",
                    "kind": "draft",
                    "account_id": "acct",
                    "provider": "mailapp",
                    "draft": {"to_addr": "a@example.test",
                              "subject": f"Re: Subject {pid}",
                              "body": "Short reply."},
                    "source_message": {"provider_id": pid, "mailbox": "INBOX"},
                    "evidence_digest": digest,
                })
    return out


# --- Request validation -------------------------------------------------------

def test_request_requires_explicit_approval(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    with pytest.raises(MailActionExecuteError, match="explicit approved"):
        build_execution_request(
            plan, action_id=item["id"], account_id="acct",
            approval={"decision": "rejected", "reason_code": "needs_edit",
                      "actor": "operator", "action_id": item["id"]},
            mutations=[{"mutation_id": "m1", "kind": "label", "account_id": "acct",
                        "provider": "mailapp", "evidence_digest": "a" * 64,
                        "message": {"provider_id": "1", "mailbox": "INBOX"},
                        "label": "Dev/GitHub"}])
    with pytest.raises(MailActionExecuteError, match="approval actor"):
        build_execution_request(
            plan, action_id=item["id"], account_id="acct",
            approval={"decision": "approved", "reason_code": "ready_to_send",
                      "actor": "", "action_id": item["id"]},
            mutations=[])


def test_request_refuses_portal_verification_items(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "portal_verification")
    with pytest.raises(MailActionExecuteError, match="not executable"):
        build_execution_request(
            plan, action_id=item["id"], account_id="acct",
            approval=_approval(item["id"]), mutations=[])


def test_request_refuses_protected_and_uncertain_as_needs_human(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    provider = FakeDirectProvider(_direct_messages(("1", "2")))
    digest = provider._digest(provider.messages["1"])
    request = build_execution_request(
        plan, action_id=item["id"], account_id="acct",
        approval=_approval(item["id"]),
        mutations=[
            {"mutation_id": "label-1", "kind": "label", "account_id": "acct",
             "provider": "mailapp", "evidence_digest": digest,
             "message": {"provider_id": "1", "mailbox": "INBOX"},
             "label": "Dev/GitHub", "protected": True},
            {"mutation_id": "label-2", "kind": "label", "account_id": "acct",
             "provider": "mailapp",
             "message": {"provider_id": "2", "mailbox": "INBOX"},
             "label": "Dev/GitHub"},
        ])
    assert request["schema"] == MAIL_ACTION_EXECUTE_SCHEMA
    assert request["mutations"] == []
    assert {r["mutation_id"] for r in request["needs_human"]} == {"label-1", "label-2"}
    assert {r["reason"] for r in request["needs_human"]} == {
        "protected_message", "uncertain_no_evidence_digest"}
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["status"] == "needs_human"
    assert result["writes_performed"] == 0
    assert provider.calls == []
    assert provider.messages["1"]["mailbox"] == "INBOX"


def test_archive_batch_over_three_is_refused(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    archive_plan, _ = archive_setup(count=4)
    mutations = [{"mutation_id": m["id"], "kind": "archive", "account_id": "a@example.invalid",
                  "provider": "gmail", "evidence_digest": "a" * 64}
                 for m in archive_plan["mutations"]]
    with pytest.raises(MailActionExecuteError, match="canary-sized"):
        build_execution_request(
            plan, action_id=item["id"], account_id="a@example.invalid",
            approval=_approval(item["id"]), mutations=mutations,
            archive_plan=archive_plan,
            archive_approval={"schema": "uma.archive_canary_approval.v1",
                              "plan_sha256": archive_plan["content_hash"],
                              "policy_sha256": archive_plan["policy_sha256"],
                              "authority": "explicit_operator_selection",
                              "selection_receipt": "operator-test-selection",
                              "selected_ids": [m["id"] for m in archive_plan["mutations"]]})


# --- Flag back-end ------------------------------------------------------------

def test_flag_canary_then_rollout_verifies_and_replays_zero_write(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    harness = Harness(tmp_path, pids=("1", "2", "3", "4"))
    harness.provider.get_message_details_ref = _starred_details(harness.provider)
    request = _flag_request(plan, item, harness, _flag_mutations(harness))
    result = execute_action(request, state_dir=tmp_path / "state",
                            provider=harness.provider)
    assert result["status"] == "verified"
    assert result["writes_performed"] == 4
    assert all(r["status"] == "verified" for r in result["results"])
    assert all(not r["replayed"] for r in result["results"])
    assert harness.provider.messages["1"].native == mailapp_index_from_flag(FlagColor.RED)
    writes_before = len([c for c in harness.provider.calls if c[0] == "set_flag_color_ref"])
    assert writes_before == 4
    # Idempotent replay: zero provider writes, stored receipt returned.
    replay = execute_action(request, state_dir=tmp_path / "state",
                            provider=harness.provider)
    assert replay["status"] == "verified"
    assert replay["writes_performed"] == 0
    assert all(r["replayed"] for r in replay["results"])
    assert len([c for c in harness.provider.calls if c[0] == "set_flag_color_ref"]) == 4
    receipts = load_execution_receipts(tmp_path / "state" / "executions.jsonl")
    assert len(receipts) == 2
    assert receipts[0]["plan_hash"] == request["plan_hash"]
    assert receipts[0]["rollback_pointer"]["flag_plan_hash"] == harness.plan["plan_hash"]
    assert {m["mutation_id"] for m in receipts[0]["mutations"]} == {
        m.mutation_id for m in harness.mutations}


def test_flag_canary_failure_blocks_rollout_and_rolls_back(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    harness = Harness(tmp_path, pids=("1", "2", "3", "4"))
    harness.provider.get_message_details_ref = _starred_details(harness.provider)
    original_native = harness.provider.messages["1"].native

    real_set = harness.provider.set_flag_color_ref

    def corrupt_second(ref, color):
        result = real_set(ref, color)
        if ref.provider_id == "2":
            # Wrong readback: the live flag no longer matches the intent,
            # and equals the pre-state, so verification reads "unchanged".
            harness.provider.messages["2"].native = mailapp_index_from_flag(FlagColor.ORANGE)
        return result

    harness.provider.set_flag_color_ref = corrupt_second
    request = _flag_request(plan, item, harness, _flag_mutations(harness))
    result = execute_action(request, state_dir=tmp_path / "state",
                            provider=harness.provider)
    by_id = {r["mutation_id"]: r for r in result["results"]}
    first, second, third, fourth = (m.mutation_id for m in harness.mutations)
    # The engine stops the canary at the ambiguous write: pid 2 reads
    # "unchanged" (live state still equals the pre-state) and pid 3 was
    # never attempted, so it also reads "unchanged".
    assert by_id[second]["status"] == "unchanged"
    assert by_id[second]["verification"] == "unchanged"
    assert by_id[third]["status"] == "unchanged"
    assert by_id[third]["verification"] == "unchanged"
    # The canary write that did land was rolled back when the canary failed.
    assert by_id[first]["status"] == "verified"
    assert by_id[first]["rollback"] == "rolled_back"
    # The rest of the batch was never attempted.
    assert by_id[fourth]["status"] == "blocked"
    assert all(harness.provider.messages[pid].native == original_native
               for pid in ("1", "2", "3", "4"))


# --- Archive back-end ---------------------------------------------------------

def test_archive_canary_executes_and_replays(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    archive_plan, approval = archive_setup(count=2)
    provider = ArchiveProvider(archive_plan)
    request = build_execution_request(
        plan, action_id=item["id"], account_id="a@example.invalid",
        approval=_approval(item["id"]),
        mutations=[{"mutation_id": m["id"], "kind": "archive",
                    "account_id": "a@example.invalid", "provider": "gmail",
                    "evidence_digest": "a" * 64}
                   for m in archive_plan["mutations"]],
        archive_plan=archive_plan, archive_approval=approval)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["status"] == "verified"
    assert result["writes_performed"] == 2
    assert provider.writes == 2
    replay = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert replay["writes_performed"] == 0
    assert provider.writes == 2
    assert all(r["replayed"] for r in replay["results"])


def test_archive_canary_mismatch_writes_nothing(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    archive_plan, approval = archive_setup(count=2)
    provider = ArchiveProvider(archive_plan)
    provider.states[archive_plan["mutations"][1]["id"]]["revision"] = "human-edit"
    request = build_execution_request(
        plan, action_id=item["id"], account_id="a@example.invalid",
        approval=_approval(item["id"]),
        mutations=[{"mutation_id": m["id"], "kind": "archive",
                    "account_id": "a@example.invalid", "provider": "gmail",
                    "evidence_digest": "a" * 64}
                   for m in archive_plan["mutations"]],
        archive_plan=archive_plan, archive_approval=approval)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["writes_performed"] == 0
    assert provider.writes == 0
    assert all(r["status"] in ("conflicted", "blocked") for r in result["results"])


# --- Label / draft wrapper ----------------------------------------------------

def test_label_cas_conflict_aborts_without_write(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    provider = FakeDirectProvider(_direct_messages(("1",)))
    mutations = _direct_mutations(provider, ("1",))
    # Evidence drifted after the plan was built: stale observation.
    provider.messages["1"]["subject"] = "Changed after planning"
    request = build_execution_request(
        plan, action_id=item["id"], account_id="acct",
        approval=_approval(item["id"]), mutations=mutations)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["status"] == "conflicted"
    assert result["writes_performed"] == 0
    assert result["results"][0]["verification"] == "conflicted"
    assert provider.messages["1"]["mailbox"] == "INBOX"
    assert ("apply_label", "1", "Dev/GitHub") not in provider.calls


def test_label_canary_mismatch_blocks_rollout_and_rolls_back(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    provider = FakeDirectProvider(_direct_messages(("1", "2", "3", "4")))
    mutations = _direct_mutations(provider, ("1", "2", "3", "4"))
    real_details = provider.get_message_details_ref

    def wrong_readback(ref):
        if ref.provider_id == "2":
            return SimpleNamespace(is_starred=False, labels={"INBOX"})
        return real_details(ref)

    provider.get_message_details_ref = wrong_readback
    request = build_execution_request(
        plan, action_id=item["id"], account_id="acct",
        approval=_approval(item["id"]), mutations=mutations)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    by_id = {r["mutation_id"]: r for r in result["results"]}
    assert by_id["label-2"]["status"] == "conflicted"
    assert by_id["label-2"]["rollback"] == "rolled_back"
    # The canary write was undone; the rest was never attempted.
    assert provider.messages["1"]["mailbox"] == "INBOX"
    assert provider.messages["2"]["mailbox"] == "INBOX"
    assert provider.messages["3"]["mailbox"] == "INBOX"
    assert provider.messages["4"]["mailbox"] == "INBOX"


def test_label_and_draft_execute_and_replay(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    provider = FakeDirectProvider(_direct_messages(("1", "2")))
    mutations = _direct_mutations(provider, ("1",), kinds=("label", "draft"))
    mutations += _direct_mutations(provider, ("2",), kinds=("label",))
    request = build_execution_request(
        plan, action_id=item["id"], account_id="acct",
        approval=_approval(item["id"]), mutations=mutations)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["status"] == "verified"
    assert result["writes_performed"] == 3
    by_id = {r["mutation_id"]: r for r in result["results"]}
    assert by_id["label-1"]["status"] == "verified"
    assert by_id["draft-1"]["status"] == "created"
    assert by_id["draft-1"]["verification"] == "provider_save_confirmed"
    assert provider.messages["1"]["mailbox"] == "Dev/GitHub"
    assert len(provider.drafts) == 1
    # Replay performs zero writes and creates no duplicate draft.
    replay = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert replay["writes_performed"] == 0
    assert len(provider.drafts) == 1
    assert all(r["replayed"] for r in replay["results"])


def test_mixed_kinds_execute_together(tmp_path):
    plan = _plan(tmp_path)
    item = _item(plan, "draft_approval")
    harness = Harness(tmp_path, pids=("1",))
    harness.provider.get_message_details_ref = _starred_details(harness.provider)
    direct = FakeDirectProvider(_direct_messages(("9",)))
    mutations = _flag_mutations(harness) + _direct_mutations(direct, ("9",), kinds=("label",))

    class CombinedProvider:
        def __init__(self, flag_provider, direct_provider):
            self.flag = flag_provider
            self.direct = direct_provider
            self.messages = direct_provider.messages

        def __getattr__(self, name):
            return getattr(self.flag, name)

        def _route(self, ref):
            return self.direct if ref.provider_id in self.direct.messages else self.flag

        def resolve_scoped(self, ref):
            return self._route(ref).resolve_scoped(ref)

        def get_message_details_ref(self, ref):
            return self._route(ref).get_message_details_ref(ref)

        def apply_label(self, message_id, label):
            return self.direct.apply_label(message_id, label)

        def create_draft(self, *args, **kwargs):
            return self.direct.create_draft(*args, **kwargs)

    provider = CombinedProvider(harness.provider, direct)
    request = _flag_request(plan, item, harness, mutations)
    result = execute_action(request, state_dir=tmp_path / "state", provider=provider)
    assert result["status"] == "verified"
    assert result["writes_performed"] == 2
    assert direct.messages["9"]["mailbox"] == "Dev/GitHub"


# --- Send boundary (regression) -----------------------------------------------

def test_send_boundary_refuses_automated_send(tmp_path, monkeypatch):
    from core.send_policy import automated_send_allowed
    from email.message import EmailMessage
    import mail_send
    import send_drafts

    policy = tmp_path / "mail-execution-policy.json"
    policy.write_text(json.dumps({
        "schema": "uma.mail_execution_policy.v1",
        "send_authority": "human_click_in_mailapp",
        "automated_send_allowed": False,
    }), encoding="utf-8")
    monkeypatch.setattr("core.send_policy.POLICY_PATH", policy)
    assert automated_send_allowed() is False

    msg = EmailMessage()
    msg["From"] = "a@example.test"
    msg["To"] = "b@example.test"
    msg["Subject"] = "s"
    msg.set_content("b")
    assert mail_send._smtp_send(msg, ("a@example.test", "pw")) is False
    assert mail_send.send_and_verify(
        msg, ("a@example.test", "pw"), None, 30, None, "send", "attempt-1") == mail_send.EXIT_FAIL_CLOSED
    assert send_drafts.send_reply(
        "b@example.test", "s", "b", ("a@example.test", "pw")) is False

    # No new code path sends: the executor never imports a send path.
    source = Path(__file__).resolve().parents[1].joinpath(
        "core", "mail_action_execute.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "mail_send" not in stripped
            assert "send_drafts" not in stripped
