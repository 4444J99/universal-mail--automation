"""Bounded execution of approved mail actions (R3, issue #229).

Takes one approved action item from a ``uma.mail.action_plan.v1`` plan and
drives its concrete mutations through: approval check -> compare-and-set
pre-read -> canary (1..3) -> bounded rollout -> post-write verification ->
recoverable receipt.

Guarantees (one set of guarantees across all four action kinds):

- An explicit ``approved`` decision is required before any write. The plan
  layer never authorizes a mutation by itself; the execution request carries
  its own approval (decision ``approved``, a reason code, an actor, and the
  action id it approves).
- One account per execution request; every mutation's ``account_id`` must
  match it.
- Protected messages, human overrides, and uncertain (evidence-less)
  mutations are refused at write time and surfaced as ``needs-human``.
- Idempotency keys are deterministic per (plan hash, action id, mutation
  id). Replaying an already-verified mutation performs zero provider writes
  and returns the stored receipt.
- Canary-first: the first 1..3 mutations run first, are verified, and only
  then does the rest roll out. A canary failure blocks the batch and rolls
  back the canary writes.
- Post-write verification on every mutation; a mismatch rolls the mutation
  back and records ``conflicted`` with the evidence.
- Recoverable receipts: every execution appends a
  ``uma.mail.action_execution_receipt.v1`` receipt recording the plan hash,
  mutation ids, verification status per mutation, and the rollback pointer.

Back-ends (reused, never forked):

- ``flag`` mutations go through ``core.flag_transactions.TransactionEngine``
  (canary then full apply, verified with
  ``core.flag_verification.verify_transactions``, rolled back with
  ``ScopedRollbackEngine``).
- ``archive`` mutations go through ``core.archive_transactions.ArchiveEngine``
  (canary approval, verify, rollback). Archive batches are canary-sized by
  engine design (1..3 mutations per archive plan); larger archive work is
  split across execution requests with separate archive plans.
- ``label`` and ``draft`` mutations use a thin transaction wrapper in this
  module following the same lock/receipt shape (CAS pre-read via
  ``provider.resolve_scoped``, post-write re-read, idempotency ledger).
  Draft rollback is unavailable on the provider surface, so a draft receipt
  records that honestly instead of pretending a rollback exists.

Out of scope (never built here):

- Any programmatic send path. This module never sends mail and never imports
  a send path; ``config/mail-execution-policy.json`` pins
  ``send_authority: human_click_in_mailapp`` and
  ``automated_send_allowed: false``. Draft creation only ever saves to
  Drafts; the human presses send.
- The Cloudflare token / share-demo deploy failure (Anthony's provisioning).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.flag_transactions import (
    AdvisoryFileLock,
    ScopedRollbackEngine,
    TransactionEngine,
    TransactionLedger,
    TransactionRollbackReceipt,
)
from core.flag_verification import verify_transactions
from core.flag_workflow import (
    ApprovalReceipt,
    OverrideStore,
    compute_plan_hash,
    sha256_hex,
    validate_plan_schema,
)
from core.archive_transactions import ArchiveEngine
from core.mail_action_ledger import ACTION_REASON_CODE_VALUES
from core.mail_action_plan import MAIL_ACTION_PLAN_SCHEMA
from core.mail_draft_approval import DRAFT_APPROVAL_REASON_CODE_VALUES
from core.models import MessageReference
from providers.flag_codecs import mailapp_index_from_flag

MAIL_ACTION_EXECUTE_SCHEMA = "uma.mail.action_execute.v1"
MAIL_ACTION_EXECUTION_SCHEMA = "uma.mail.action_execution.v1"
MAIL_ACTION_EXECUTION_RECEIPT_SCHEMA = "uma.mail.action_execution_receipt.v1"

CANARY_MAX = 3
BATCH_MAX = 25
ARCHIVE_BATCH_MAX = 3
MUTATION_KINDS = ("flag", "archive", "label", "draft")

# Approval types whose automation boundary forbids executor writes: the
# official portal, legal review, or an external system owns those actions.
NON_EXECUTABLE_APPROVAL_TYPES = ("portal_verification", "human_legal_review", "external_reconcile")

# Stored receipt statuses that prove a mutation already happened (or may
# have happened). Replaying one performs zero provider writes.
REPLAY_BLOCKING_STATUSES = ("verified", "created", "uncertain", "ambiguous")
# Stored receipt statuses after which a fresh attempt is safe.
RETRYABLE_STATUSES = ("conflicted", "failed", "rolled_back", "blocked")


class MailActionExecuteError(ValueError):
    """Raised when an execution request cannot be built or executed safely."""

    def __init__(self, detail: str, *, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _hash(prefix: str, *parts: Any, length: int = 16) -> str:
    material = "|".join(str(part or "") for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _format_dt(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _plan_hash(action_plan: Dict[str, Any]) -> str:
    """Stable hash of the action plan dict (the plan carries no hash field)."""
    return sha256_hex(action_plan)


def idempotency_key_for(plan_hash: str, action_id: str, mutation_id: str) -> str:
    """Deterministic idempotency key per (plan hash, action id, mutation id)."""
    return "idem-" + sha256_hex({
        "schema": MAIL_ACTION_EXECUTE_SCHEMA,
        "plan_hash": plan_hash,
        "action_id": action_id,
        "mutation_id": mutation_id,
    })[:32]


def _coerce_action_plan(action_plan: Union[Dict[str, Any], Path, str]) -> Dict[str, Any]:
    if isinstance(action_plan, dict):
        data = action_plan
    else:
        path = Path(action_plan).expanduser()
        if not path.is_file():
            raise MailActionExecuteError("action plan input not found", status_code=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MailActionExecuteError("action plan input is not valid JSON") from e
        except OSError as e:
            raise MailActionExecuteError(f"action plan input could not be read: {e}") from e
    if not isinstance(data, dict) or data.get("schema") != MAIL_ACTION_PLAN_SCHEMA:
        raise MailActionExecuteError(f"action plan input must be {MAIL_ACTION_PLAN_SCHEMA}")
    return data


def _action_by_id(action_plan: Dict[str, Any], action_id: str) -> Dict[str, Any]:
    for item in action_plan.get("items") or []:
        if isinstance(item, dict) and item.get("id") == action_id:
            return item
    raise MailActionExecuteError("action_id is not present in the current action plan", status_code=404)


def _validate_approval(approval: Any, action_id: str) -> Dict[str, Any]:
    """Require an explicit approved decision naming this action.

    Why: the plan layer never authorizes a mutation by itself, so the
    execution request must carry its own approval. A missing, non-approved,
    or misdirected approval fails closed before any provider is touched.
    """
    if not isinstance(approval, dict):
        raise MailActionExecuteError("execution approval is required", status_code=403)
    decision = str(approval.get("decision") or "").strip().lower()
    if decision != "approved":
        raise MailActionExecuteError(
            "execution requires an explicit approved decision", status_code=403)
    reason = str(approval.get("reason_code") or "").strip().lower()
    allowed = set(DRAFT_APPROVAL_REASON_CODE_VALUES) | set(ACTION_REASON_CODE_VALUES)
    if reason not in allowed:
        raise MailActionExecuteError(
            f"approval reason_code must be one of {', '.join(sorted(allowed))}")
    actor = str(approval.get("actor") or "").strip()
    if not actor:
        raise MailActionExecuteError("approval actor is required", status_code=403)
    approved_action = approval.get("action_id")
    if approved_action is not None and approved_action != action_id:
        raise MailActionExecuteError("approval action_id does not match the action")
    return {
        "decision": decision,
        "reason_code": reason,
        "actor": actor,
        "receipt_id": approval.get("receipt_id"),
        "action_id": action_id,
    }


def _require_hex_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise MailActionExecuteError(f"{name} must be a 64-hex evidence digest")
    try:
        int(value, 16)
    except ValueError as e:
        raise MailActionExecuteError(f"{name} must be a 64-hex evidence digest") from e
    return value


def _validate_mutations(
    mutations: Any, account_id: str, *, plan_hash: str, action_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate mutation shape/scope and split out needs-human refusals.

    Returns (executable, needs_human). A mutation is refused as needs-human
    (never written) when it is protected, human-overridden, or uncertain
    (no evidence digest for a kind that mutates an existing message).
    """
    if not isinstance(mutations, list) or not 1 <= len(mutations) <= BATCH_MAX:
        raise MailActionExecuteError(
            f"execution requires 1..{BATCH_MAX} mutations")
    seen: set = set()
    executable: List[Dict[str, Any]] = []
    needs_human: List[Dict[str, Any]] = []
    for raw in mutations:
        if not isinstance(raw, dict):
            raise MailActionExecuteError("each mutation must be an object")
        mutation_id = raw.get("mutation_id")
        if not isinstance(mutation_id, str) or not mutation_id.strip():
            raise MailActionExecuteError("each mutation requires a mutation_id")
        if mutation_id in seen:
            raise MailActionExecuteError(f"duplicate mutation_id {mutation_id}")
        seen.add(mutation_id)
        kind = raw.get("kind")
        if kind not in MUTATION_KINDS:
            raise MailActionExecuteError(
                f"mutation kind must be one of {', '.join(MUTATION_KINDS)}")
        if raw.get("account_id") != account_id:
            raise MailActionExecuteError(
                f"mutation {mutation_id} account_id does not match the execution account")
        if not isinstance(raw.get("provider"), str) or not raw.get("provider"):
            raise MailActionExecuteError(f"mutation {mutation_id} requires a provider")
        entry = {
            "mutation_id": mutation_id,
            "kind": kind,
            "account_id": account_id,
            "provider": raw.get("provider"),
            "idempotency_key": idempotency_key_for(plan_hash, action_id, mutation_id),
        }
        # Protected / overridden / uncertain messages are never written.
        # Why: the issue's safety floor is preserving them and honoring
        # overrides; the executor surfaces them for a human instead.
        if raw.get("protected") is True:
            needs_human.append({**entry, "status": "needs-human",
                                "reason": "protected_message"})
            continue
        if raw.get("human_override") is True:
            needs_human.append({**entry, "status": "needs-human",
                                "reason": "human_override"})
            continue
        if kind in ("flag", "archive", "label"):
            digest = raw.get("evidence_digest")
            if not isinstance(digest, str) or not digest:
                needs_human.append({**entry, "status": "needs-human",
                                    "reason": "uncertain_no_evidence_digest"})
                continue
            entry["evidence_digest"] = _require_hex_digest(digest, "evidence_digest")
        if kind == "label":
            label = raw.get("label")
            if not isinstance(label, str) or not label.strip():
                raise MailActionExecuteError(f"mutation {mutation_id} requires a label")
            message = raw.get("message")
            if not isinstance(message, dict) or not message.get("provider_id") \
                    or not message.get("mailbox"):
                raise MailActionExecuteError(
                    f"mutation {mutation_id} requires a message provider_id and mailbox")
            entry["label"] = label
            entry["message"] = message
        elif kind == "draft":
            draft = raw.get("draft")
            if not isinstance(draft, dict):
                raise MailActionExecuteError(f"mutation {mutation_id} requires a draft")
            for field in ("to_addr", "subject", "body"):
                if not isinstance(draft.get(field), str) or not draft.get(field):
                    raise MailActionExecuteError(
                        f"mutation {mutation_id} draft requires {field}")
            entry["draft"] = draft
            source = raw.get("source_message")
            if source is not None:
                if not isinstance(source, dict) or not source.get("provider_id") \
                        or not source.get("mailbox"):
                    raise MailActionExecuteError(
                        f"mutation {mutation_id} source_message requires provider_id and mailbox")
                entry["source_message"] = source
                if raw.get("evidence_digest"):
                    entry["evidence_digest"] = _require_hex_digest(
                        raw.get("evidence_digest"), "evidence_digest")
        executable.append(entry)
    return executable, needs_human


def _validate_flag_back_end(
    flag_plan: Any, flag_approval: Any, flag_mutation_ids: List[str],
    flag_canary_approval: Any = None,
) -> Tuple[Dict[str, Any], ApprovalReceipt, Optional[ApprovalReceipt]]:
    """Bind the flag back-end: plan + approval must cover exactly the flag mutations.

    The engine's canary call requires the canary approval itself to approve
    1..3 mutations, so a batch of more than three flag mutations needs a
    second, canary-sized approval for exactly the first three (same plan,
    snapshot, policy, and operator as the full approval). With three or
    fewer flag mutations the full approval serves both steps.
    """
    if not isinstance(flag_plan, dict):
        raise MailActionExecuteError("flag mutations require a flag_plan")
    if not isinstance(flag_approval, dict):
        raise MailActionExecuteError("flag mutations require a flag_approval")
    mutations = validate_plan_schema(flag_plan)
    plan_ids = [m.mutation_id for m in mutations]
    if plan_ids != flag_mutation_ids:
        raise MailActionExecuteError(
            "flag_plan mutations must match the request flag mutations in order")
    approval = ApprovalReceipt.from_dict(flag_approval)
    if sorted(approval.approved_mutation_ids) != sorted(flag_mutation_ids):
        raise MailActionExecuteError(
            "flag_approval must approve exactly the request flag mutations")
    if compute_plan_hash(flag_plan) != flag_plan.get("plan_hash"):
        raise MailActionExecuteError("flag_plan plan_hash mismatch — plan was tampered")
    canary_ids = flag_mutation_ids[:CANARY_MAX]
    canary_approval: Optional[ApprovalReceipt] = None
    if len(flag_mutation_ids) > CANARY_MAX:
        if not isinstance(flag_canary_approval, dict):
            raise MailActionExecuteError(
                f"more than {CANARY_MAX} flag mutations require a flag_canary_approval "
                "approving exactly the first canary mutations")
        canary_approval = ApprovalReceipt.from_dict(flag_canary_approval)
        if sorted(canary_approval.approved_mutation_ids) != sorted(canary_ids):
            raise MailActionExecuteError(
                "flag_canary_approval must approve exactly the first canary mutations")
        for field in ("plan_hash", "snapshot_sha256", "policy_sha256",
                      "approving_operator"):
            if getattr(canary_approval, field) != getattr(approval, field):
                raise MailActionExecuteError(
                    f"flag_canary_approval {field} does not match flag_approval")
    elif flag_canary_approval is not None:
        raise MailActionExecuteError(
            "flag_canary_approval is only needed for more than "
            f"{CANARY_MAX} flag mutations")
    return flag_plan, approval, canary_approval


def _validate_archive_back_end(
    archive_plan: Any, archive_approval: Any, archive_mutation_ids: List[str],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Bind the archive back-end: canary approval must name exactly the archive mutations."""
    if not isinstance(archive_plan, dict):
        raise MailActionExecuteError("archive mutations require an archive_plan")
    if not isinstance(archive_approval, dict):
        raise MailActionExecuteError("archive mutations require an archive_approval")
    if len(archive_mutation_ids) > ARCHIVE_BATCH_MAX:
        raise MailActionExecuteError(
            f"archive batches are canary-sized (1..{ARCHIVE_BATCH_MAX}); "
            "split larger archive work across execution requests")
    plan_ids = [m.get("id") for m in archive_plan.get("mutations") or []]
    if plan_ids != archive_mutation_ids:
        raise MailActionExecuteError(
            "archive_plan mutations must match the request archive mutations in order")
    if archive_approval.get("schema") != "uma.archive_canary_approval.v1":
        raise MailActionExecuteError(
            "archive_approval must be a uma.archive_canary_approval.v1 canary approval")
    if archive_approval.get("plan_sha256") != archive_plan.get("content_hash"):
        raise MailActionExecuteError("archive_approval plan_sha256 does not match the archive plan")
    selected = archive_approval.get("selected_ids")
    if not isinstance(selected, list) or selected != archive_mutation_ids:
        raise MailActionExecuteError(
            "archive canary approval must select exactly the request archive mutations")
    return archive_plan, archive_approval


def build_execution_request(
    action_plan: Union[Dict[str, Any], Path, str],
    *,
    action_id: str,
    account_id: str,
    approval: Dict[str, Any],
    mutations: List[Dict[str, Any]],
    flag_plan: Optional[Dict[str, Any]] = None,
    flag_approval: Optional[Dict[str, Any]] = None,
    flag_canary_approval: Optional[Dict[str, Any]] = None,
    archive_plan: Optional[Dict[str, Any]] = None,
    archive_approval: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a validated bounded-execution request for one approved action.

    Building the request performs every check that can be done without
    touching a provider: plan/action/approval validity, mutation shape,
    single-account scope, protected/override/uncertain refusals, and
    back-end binding. Needs-human refusals are recorded on the request, not
    raised, so the operator sees exactly which mutations were refused and why.
    """
    plan = _coerce_action_plan(action_plan)
    item = _action_by_id(plan, action_id)
    if item.get("approval_type") in NON_EXECUTABLE_APPROVAL_TYPES:
        raise MailActionExecuteError(
            f"action approval_type {item.get('approval_type')} is not executable "
            "by the bounded executor; use the official portal, legal review, "
            "or external system the approval type names", status_code=403)
    checked = _validate_approval(approval, action_id)
    if not isinstance(account_id, str) or not account_id.strip():
        raise MailActionExecuteError("execution requires an account_id")
    hash_value = _plan_hash(plan)
    executable, needs_human = _validate_mutations(
        mutations, account_id, plan_hash=hash_value, action_id=action_id)
    flag_ids = [m["mutation_id"] for m in executable if m["kind"] == "flag"]
    archive_ids = [m["mutation_id"] for m in executable if m["kind"] == "archive"]
    bound_flag_plan = bound_flag_approval = bound_flag_canary_approval = None
    if flag_ids:
        bound_flag_plan, bound_flag_approval, bound_flag_canary_approval = \
            _validate_flag_back_end(
                flag_plan, flag_approval, flag_ids,
                flag_canary_approval=flag_canary_approval)
    elif flag_plan is not None or flag_approval is not None \
            or flag_canary_approval is not None:
        raise MailActionExecuteError(
            "flag_plan/flag_approval supplied without flag mutations")
    bound_archive_plan = bound_archive_approval = None
    if archive_ids:
        bound_archive_plan, bound_archive_approval = _validate_archive_back_end(
            archive_plan, archive_approval, archive_ids)
    elif archive_plan is not None or archive_approval is not None:
        raise MailActionExecuteError(
            "archive_plan/archive_approval supplied without archive mutations")
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return {
        "schema": MAIL_ACTION_EXECUTE_SCHEMA,
        "status": "ok",
        "action_plan_schema": plan.get("schema"),
        "plan_hash": hash_value,
        "action_id": action_id,
        "account_id": account_id,
        "approval": checked,
        "mutations": executable,
        "needs_human": needs_human,
        "flag_plan": bound_flag_plan,
        "flag_approval": bound_flag_approval.to_dict() if bound_flag_approval else None,
        "flag_canary_approval": (
            bound_flag_canary_approval.to_dict()
            if bound_flag_canary_approval else None),
        "archive_plan": bound_archive_plan,
        "archive_approval": bound_archive_approval,
        "checked_at": _format_dt(checked_at.astimezone(timezone.utc)),
    }


# --- Execution receipts -------------------------------------------------------

def load_execution_receipts(receipt_path: Union[Path, str, None]) -> List[Dict[str, Any]]:
    """Load execution receipts from a JSONL file.

    A missing file is a valid empty ledger; malformed existing files fail closed.
    """
    if receipt_path is None:
        return []
    path = Path(receipt_path).expanduser()
    if not path.exists():
        return []
    if not path.is_file():
        raise MailActionExecuteError("execution receipt ledger path is not a file")
    receipts: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise MailActionExecuteError(f"execution receipt ledger could not be read: {e}") from e
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise MailActionExecuteError(
                f"execution receipt ledger line {index} is not valid JSON") from e
        if not isinstance(data, dict) or data.get("schema") != MAIL_ACTION_EXECUTION_RECEIPT_SCHEMA:
            raise MailActionExecuteError(
                f"execution receipt ledger line {index} must be {MAIL_ACTION_EXECUTION_RECEIPT_SCHEMA}")
        receipts.append(data)
    return receipts


def _receipt_for_mutation(
    receipts: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Latest receipt per idempotency key (receipts are append-only)."""
    latest: Dict[str, Dict[str, Any]] = {}
    for receipt in receipts:
        for mutation in receipt.get("mutations") or []:
            key = mutation.get("idempotency_key")
            if isinstance(key, str):
                latest[key] = mutation
    return latest


def build_execution_receipt(
    request: Dict[str, Any],
    *,
    status: str,
    results: List[Dict[str, Any]],
    writes_performed: int,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build a recoverable execution receipt for one action execution.

    Why a separate builder instead of extending ``build_action_receipt``:
    the ledger receipt is record-only by design (``records_external_claim_only``);
    the execution receipt records what actually mutated, with the verification
    status per mutation and the rollback pointer needed to undo it.
    """
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    occurred = _format_dt(checked_at.astimezone(timezone.utc))
    receipt_id = _hash("exreceipt", occurred, request["action_id"],
                       request["plan_hash"], status,
                       ",".join(r.get("mutation_id", "") for r in results))
    rollback_pointer = {
        "flag_plan_hash": (request.get("flag_plan") or {}).get("plan_hash"),
        "archive_plan_hash": (request.get("archive_plan") or {}).get("content_hash"),
        "execution_receipt_id": receipt_id,
        "rollback_note": (
            "Flag mutations roll back via ScopedRollbackEngine with the "
            "transaction ids in this receipt; archive mutations via "
            "ArchiveEngine.rollback with the archive plan; label mutations "
            "by re-applying the original mailbox; draft creation has no "
            "provider rollback — delete the draft by hand."
        ),
    }
    return {
        "schema": MAIL_ACTION_EXECUTION_RECEIPT_SCHEMA,
        "status": "recorded",
        "receipt_id": receipt_id,
        "created_at": occurred,
        "actor": str(actor or request["approval"]["actor"])[:64],
        "action_id": request["action_id"],
        "plan_hash": request["plan_hash"],
        "account_id": request["account_id"],
        "approval": request["approval"],
        "execution_status": status,
        "mutations": results,
        "writes_performed": writes_performed,
        "rollback_pointer": rollback_pointer,
        "privacy": {
            "redacted": True,
            "raw_mail_printed": False,
            "freeform_private_notes": False,
        },
        "safety": {
            # The executor never sends; mailbox mutations are the executed
            # ones listed per mutation, nothing else.
            "send_allowed": False,
            "automated_send_allowed": False,
        },
    }


# --- Execution ----------------------------------------------------------------

def _message_ref(mutation: Dict[str, Any], message: Dict[str, Any]) -> MessageReference:
    """Build the scoped reference a mutation's CAS pre-read needs."""
    return MessageReference(
        provider=mutation["provider"],
        account=mutation["account_id"],
        mailbox=message["mailbox"],
        provider_id=message["provider_id"],
        received_iso=message.get("received_iso"),
        sender_digest=message.get("sender_digest"),
        subject_digest=message.get("subject_digest"),
        message_id_digest=message.get("message_id_digest"),
        observed_native_flag=message.get("observed_native_flag"),
        evidence_digest=mutation.get("evidence_digest"),
        snapshot_id=message.get("snapshot_id"),
    )


def _cas_pre_read(provider: Any, mutation: Dict[str, Any], message: Dict[str, Any]) -> Tuple[bool, str]:
    """Compare-and-set pre-read: re-read live state and compare the digest.

    Returns (ok, reason). A moved digest means the observation the mutation
    was planned against is stale, so the mutation aborts as conflicted
    before any write. A provider that cannot re-read fails closed too.
    """
    try:
        live = provider.resolve_scoped(_message_ref(mutation, message))
    except (RuntimeError, ValueError, KeyError) as exc:
        return False, f"pre_read_failed:{type(exc).__name__}"
    if live.evidence_digest != mutation.get("evidence_digest"):
        return False, "conflicted"
    return True, "verified"


def _execute_flag_group(
    request: Dict[str, Any], mutations: List[Dict[str, Any]], *,
    state_dir: Path, provider: Any, flag_overrides: Any, now: datetime,
) -> Tuple[List[Dict[str, Any]], int]:
    """Canary then rollout through the flag transaction engine.

    Why this order: the engine's canary applies the first 1..3 approved
    mutations and the full apply covers the rest from the same ledger, so a
    canary failure stops the batch before the remaining mutations are ever
    attempted.
    """
    flag_state = state_dir / "flags"
    ledger = TransactionLedger(flag_state / "ledger" / "transactions.jsonl")
    overrides = flag_overrides if flag_overrides is not None else OverrideStore(
        flag_state / "overrides.json")
    engine = TransactionEngine(flag_state, ledger, overrides)
    flag_plan = dict(request["flag_plan"])
    approval = ApprovalReceipt.from_dict(request["flag_approval"])
    canary_approval = (
        ApprovalReceipt.from_dict(request["flag_canary_approval"])
        if request.get("flag_canary_approval") else approval)
    canary_count = min(CANARY_MAX, len(mutations))
    writes = 0
    results: List[Dict[str, Any]] = []

    def _record(mutation: Dict[str, Any], status: str, verification: str, *,
                replayed: bool = False, mutation_writes: int = 0,
                rollback: Optional[str] = None) -> Dict[str, Any]:
        return {
            "mutation_id": mutation["mutation_id"],
            "kind": "flag",
            "status": status,
            "verification": verification,
            "idempotency_key": mutation["idempotency_key"],
            "replayed": replayed,
            "writes_performed": mutation_writes,
            "rollback": rollback,
        }

    canary = mutations[:canary_count]
    canary_result = engine.apply_canary_transaction(
        plan=flag_plan, approval=canary_approval, provider=provider,
        max_count=canary_count)
    writes += canary_result.writes_performed

    def _verify_map() -> Dict[str, str]:
        """Per-mutation verification status, straight from the verifier."""
        try:
            verification = verify_transactions(flag_plan, ledger, provider)
        except ValueError:
            return {}
        return {r["mutation_id"]: r["status"] for r in verification["results"]}

    def _status_for(vstatus: str) -> str:
        # The verifier's own vocabulary is the honest record: "unchanged"
        # means the write never landed (nothing to roll back), "conflicted"
        # means live state disagrees with both pre- and intended state.
        if vstatus == "verified":
            return "verified"
        if vstatus == "unchanged":
            return "unchanged"
        return "conflicted"

    # Post-write verification of the canary before any rollout.
    verified = _verify_map()
    canary_ok = all(verified.get(m["mutation_id"]) == "verified" for m in canary)
    if not canary_ok:
        # Canary failure blocks the batch and rolls back the canary writes
        # that landed: verified ones (the write took) and conflicted ones
        # (the write may have taken, then moved). Unchanged ones never wrote.
        rolled_back = _rollback_flag(
            engine, ledger, overrides, flag_plan, approval, provider,
            mutations=[m for m in canary
                       if verified.get(m["mutation_id"]) in ("verified", "conflicted")],
            now=now, canary_approval=canary_approval,
            canary_ids=tuple(m["mutation_id"] for m in canary))
        for mutation in canary:
            vstatus = verified.get(mutation["mutation_id"], "not_verified")
            if vstatus == "verified":
                results.append(_record(
                    mutation, "verified", "verified",
                    rollback="rolled_back" if mutation["mutation_id"] in rolled_back
                    else "rollback_failed"))
            elif canary_result.status == "blocked" and vstatus == "not_verified":
                results.append(_record(mutation, "blocked", "not_verified"))
            else:
                results.append(_record(
                    mutation, _status_for(vstatus), vstatus,
                    rollback="rolled_back" if mutation["mutation_id"] in rolled_back
                    else None))
        # The rest of the batch was never attempted; record it, don't drop it.
        for mutation in mutations[canary_count:]:
            results.append(_record(mutation, "blocked", "not_verified"))
        return results, writes
    for mutation in canary:
        results.append(_record(mutation, "verified", "verified"))
    # Bounded rollout of the rest from the same plan and ledger.
    rest = mutations[canary_count:]
    if rest:
        rollout = engine.apply_transaction(
            plan=flag_plan, approval=approval, provider=provider)
        writes += rollout.writes_performed
        if rollout.status in ("blocked", "already_claimed"):
            for mutation in rest:
                results.append(_record(mutation, "blocked", "not_verified"))
        else:
            verified = _verify_map()
            failed = [m for m in rest if verified.get(m["mutation_id"]) != "verified"]
            rollout_rolled_back: set = set()
            if failed:
                # A conflicted write may have landed and then moved; roll
                # it back. Unchanged writes never landed; skip them.
                rollout_rolled_back = _rollback_flag(
                    engine, ledger, overrides, flag_plan, approval,
                    provider,
                    mutations=[m for m in failed
                               if verified.get(m["mutation_id"]) == "conflicted"],
                    now=now, canary_approval=canary_approval,
                    canary_ids=tuple(m["mutation_id"] for m in canary))
            for mutation in rest:
                vstatus = verified.get(mutation["mutation_id"], "not_verified")
                if vstatus == "verified":
                    results.append(_record(mutation, "verified", "verified"))
                else:
                    results.append(_record(
                        mutation, _status_for(vstatus), vstatus,
                        rollback="rolled_back"
                        if mutation["mutation_id"] in rollout_rolled_back else None))
    return results, writes


def _rollback_flag(engine: TransactionEngine, ledger: TransactionLedger,
                   overrides: Any, flag_plan: Dict[str, Any],
                   approval: ApprovalReceipt, provider: Any, *,
                   mutations: List[Dict[str, Any]], now: datetime,
                   canary_approval: Optional[ApprovalReceipt] = None,
                   canary_ids: Tuple[str, ...] = ()) -> set:
    """Roll back applied flag mutations, best effort, in canary-sized chunks.

    Returns the mutation ids the engine reports as rolled back. Transaction
    ids bind the approval hash, so a mutation applied under the canary
    approval rolls back under that approval, not the full one.
    """
    plan_mutations = {m.mutation_id: m for m in validate_plan_schema(flag_plan)}
    rb_engine = ScopedRollbackEngine(engine.state_dir, ledger, overrides, flag_plan)
    receipts = []
    for mutation in mutations:
        planned = plan_mutations.get(mutation["mutation_id"])
        if planned is None:
            continue
        if planned.observed_native_flag is None:
            # No captured prior native state: nothing exact to restore.
            continue
        if not ledger.has_verified(flag_plan["plan_hash"], mutation["mutation_id"]):
            continue
        receipts.append(TransactionRollbackReceipt.create(
            transaction_id=engine._txid(
                flag_plan["plan_hash"], planned,
                canary_approval if mutation["mutation_id"] in canary_ids
                and canary_approval is not None else approval),
            plan_sha256=flag_plan["plan_hash"],
            approval_sha256=(
                canary_approval if mutation["mutation_id"] in canary_ids
                and canary_approval is not None else approval).content_hash,
            mutation_id=planned.mutation_id,
            ref_digest=planned.ref_digest,
            pre_native_flag=planned.observed_native_flag,
            pre_semantic_flag=planned.observed_flag.value,
            applied_native_flag=mailapp_index_from_flag(planned.proposed_flag),
            applied_semantic_flag=planned.proposed_flag.value,
        ))
    rolled_back: set = set()
    for start in range(0, len(receipts), CANARY_MAX):
        try:
            outcome = rb_engine.rollback_transactions(
                receipts=receipts[start:start + CANARY_MAX], provider=provider,
                max_count=CANARY_MAX)
        except (RuntimeError, ValueError):
            continue
        for row in outcome.results:
            if row.get("status") in ("rolled_back", "already_rolled_back"):
                rolled_back.add(row.get("mutation_id"))
    _ = now
    return rolled_back


def _execute_archive_group(
    request: Dict[str, Any], mutations: List[Dict[str, Any]], *,
    state_dir: Path, provider: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """Canary-sized archive execution through the archive engine."""
    engine = ArchiveEngine(state_dir / "archive")
    archive_plan = request["archive_plan"]
    approval = request["archive_approval"]
    receipt = engine.apply(archive_plan, approval, provider)
    writes = int(receipt.get("writes_performed") or 0)
    results: List[Dict[str, Any]] = []
    by_id = {r.get("id"): r for r in receipt.get("results") or []}
    if receipt.get("status") != "verified":
        # Mismatch or failure: roll back whatever verified, then stop.
        try:
            engine.rollback(archive_plan, provider)
            rolled_back = True
        except (RuntimeError, ValueError, KeyError, OSError):
            rolled_back = False
        for mutation in mutations:
            row = by_id.get(mutation["mutation_id"]) or {}
            status = row.get("status") or "blocked"
            results.append({
                "mutation_id": mutation["mutation_id"],
                "kind": "archive",
                "status": "verified" if status == "verified" else "conflicted",
                "verification": status,
                "idempotency_key": mutation["idempotency_key"],
                "replayed": False,
                "writes_performed": 1 if status == "verified" else 0,
                "rollback": "rolled_back" if rolled_back and status == "verified" else None,
            })
        return results, writes
    verification = engine.verify(archive_plan, provider)
    verified_ids = {r.get("id") for r in verification.get("results") or []
                    if r.get("status") == "verified"}
    for mutation in mutations:
        ok = mutation["mutation_id"] in verified_ids
        results.append({
            "mutation_id": mutation["mutation_id"],
            "kind": "archive",
            "status": "verified" if ok else "conflicted",
            "verification": "verified" if ok else "conflicted",
            "idempotency_key": mutation["idempotency_key"],
            "replayed": False,
            "writes_performed": 1 if ok else 0,
            "rollback": None,
        })
    return results, writes


def _execute_direct_group(
    request: Dict[str, Any], mutations: List[Dict[str, Any]], *,
    provider: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    """Canary-first label/draft execution with the thin wrapper in this module.

    Why a wrapper instead of the engines: labels and drafts have no
    transaction engine, so the executor itself provides the same shape —
    CAS pre-read, canary, post-write re-read, idempotency ledger — with the
    provider's scoped surface as the only write path.
    """
    writes = 0
    results: List[Dict[str, Any]] = []
    canary_count = min(CANARY_MAX, len(mutations))
    canary = mutations[:canary_count]
    rest = mutations[canary_count:]
    canary_results, canary_writes = _execute_direct_batch(
        canary, provider=provider)
    writes += canary_writes
    results.extend(canary_results)
    if any(r["status"] != "verified" and r["status"] != "created"
           for r in canary_results):
        # Canary failure blocks the batch; roll back the canary writes.
        for row in canary_results:
            if row["status"] in ("verified", "created"):
                _rollback_direct(row, mutations, provider=provider)
                row["rollback"] = "rolled_back" if row["kind"] == "label" \
                    else "unavailable_manual_delete_required"
                if row["kind"] == "label":
                    row["status"] = "rolled_back"
        for mutation in rest:
            results.append({
                "mutation_id": mutation["mutation_id"],
                "kind": mutation["kind"],
                "status": "blocked",
                "verification": "not_verified",
                "idempotency_key": mutation["idempotency_key"],
                "replayed": False,
                "writes_performed": 0,
                "rollback": None,
            })
        return results, writes
    rest_results, rest_writes = _execute_direct_batch(rest, provider=provider)
    writes += rest_writes
    results.extend(rest_results)
    return results, writes


def _execute_direct_batch(
    mutations: List[Dict[str, Any]], *, provider: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    writes = 0
    results: List[Dict[str, Any]] = []
    for mutation in mutations:
        if mutation["kind"] == "label":
            row, count = _execute_label(mutation, provider=provider)
        else:
            row, count = _execute_draft(mutation, provider=provider)
        writes += count
        results.append(row)
    return results, writes


def _execute_label(mutation: Dict[str, Any], *, provider: Any) -> Tuple[Dict[str, Any], int]:
    base = {
        "mutation_id": mutation["mutation_id"],
        "kind": "label",
        "idempotency_key": mutation["idempotency_key"],
        "replayed": False,
        "rollback": None,
    }
    message = mutation["message"]
    ok, reason = _cas_pre_read(provider, mutation, message)
    if not ok:
        return {**base, "status": "conflicted", "verification": reason,
                "writes_performed": 0}, 0
    try:
        applied = provider.apply_label(message["provider_id"], mutation["label"])
    except (RuntimeError, ValueError) as exc:
        return {**base, "status": "failed", "verification": f"write_failed:{type(exc).__name__}",
                "writes_performed": 0}, 0
    if applied is not True:
        return {**base, "status": "failed", "verification": "provider_refused",
                "writes_performed": 0}, 0
    # Post-write verification: re-read in the target mailbox and confirm.
    try:
        live = provider.resolve_scoped(_message_ref(
            mutation, {**message, "mailbox": mutation["label"]}))
        details = provider.get_message_details_ref(live)
        labels = details.labels if details is not None else set()
        if mutation["label"] not in labels:
            _rollback_label(mutation, provider=provider)
            return {**base, "status": "conflicted", "verification": "mismatch",
                    "writes_performed": 1, "rollback": "rolled_back"}, 1
    except (RuntimeError, ValueError, KeyError):
        return {**base, "status": "uncertain", "verification": "readback_failed",
                "writes_performed": 1, "rollback": "unverified"}, 1
    return {**base, "status": "verified", "verification": "verified",
            "writes_performed": 1}, 1


def _rollback_label(mutation: Dict[str, Any], *, provider: Any) -> None:
    """Move a label write back to the original mailbox, best effort."""
    try:
        provider.apply_label(mutation["message"]["provider_id"],
                             mutation["message"]["mailbox"])
    except (RuntimeError, ValueError):
        pass


def _rollback_direct(row: Dict[str, Any], mutations: List[Dict[str, Any]], *,
                     provider: Any) -> None:
    if row["kind"] != "label":
        return
    mutation = next((m for m in mutations if m["mutation_id"] == row["mutation_id"]), None)
    if mutation is not None:
        _rollback_label(mutation, provider=provider)


def _execute_draft(mutation: Dict[str, Any], *, provider: Any) -> Tuple[Dict[str, Any], int]:
    base = {
        "mutation_id": mutation["mutation_id"],
        "kind": "draft",
        "idempotency_key": mutation["idempotency_key"],
        "replayed": False,
        "rollback": "unavailable_manual_delete_required",
    }
    source = mutation.get("source_message")
    if source is not None and mutation.get("evidence_digest"):
        ok, reason = _cas_pre_read(provider, mutation, source)
        if not ok:
            return {**base, "status": "conflicted", "verification": reason,
                    "writes_performed": 0}, 0
    draft = mutation["draft"]
    try:
        # create_draft only ever saves to Drafts; it never sends. The human
        # presses send in Mail.app (send_authority: human_click_in_mailapp).
        saved = provider.create_draft(
            draft["to_addr"], draft["subject"], draft["body"],
            account=mutation["account_id"])
    except (RuntimeError, ValueError) as exc:
        return {**base, "status": "failed", "verification": f"write_failed:{type(exc).__name__}",
                "writes_performed": 0}, 0
    if saved is not True:
        return {**base, "status": "failed", "verification": "provider_refused",
                "writes_performed": 0}, 0
    # Drafts cannot be re-read through the provider surface, so verification
    # is the provider's save confirmation, recorded as such — not dressed
    # up as a re-read.
    return {**base, "status": "created", "verification": "provider_save_confirmed",
            "writes_performed": 1}, 1


# --- Top-level execution ------------------------------------------------------

def _result_status(results: List[Dict[str, Any]], needs_human: List[Dict[str, Any]]) -> str:
    """Aggregate one status for the execution from per-mutation results."""
    statuses = {r["status"] for r in results}
    if statuses and statuses <= {"verified", "created"} and not needs_human:
        return "verified"
    if statuses <= {"verified", "created", "needs-human", "blocked"} and (
            "verified" in statuses or "created" in statuses):
        return "partially_verified"
    if statuses == {"blocked"}:
        return "blocked"
    if "conflicted" in statuses or "failed" in statuses:
        return "conflicted"
    if "rolled_back" in statuses:
        return "rolled_back"
    return "needs_human" if needs_human else "blocked"


def execute_action(
    request: Union[Dict[str, Any], Path, str],
    *,
    state_dir: Union[Path, str],
    provider: Any,
    flag_overrides: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Execute one approved action's mutations with the bounded guarantees.

    The request may be a dict built by :func:`build_execution_request` or a
    path to its JSON. Execution is serialized per (plan hash, action) under a
    private lock, replays verified mutations with zero provider writes, runs
    each kind-group canary-first, verifies every write, rolls back on
    mismatch, and appends a recoverable receipt to
    ``<state_dir>/executions.jsonl``.

    The provider is the mail surface (``MailAppProvider`` in production, a
    fake in tests). No send path is reachable from here: label/draft writes
    go through ``apply_label`` / ``create_draft`` only, and draft creation
    only ever saves to Drafts.
    """
    if isinstance(request, dict):
        req = request
    else:
        path = Path(request).expanduser()
        if not path.is_file():
            raise MailActionExecuteError("execution request input not found", status_code=404)
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MailActionExecuteError("execution request input is not valid JSON") from e
        except OSError as e:
            raise MailActionExecuteError(f"execution request input could not be read: {e}") from e
    if not isinstance(req, dict) or req.get("schema") != MAIL_ACTION_EXECUTE_SCHEMA:
        raise MailActionExecuteError(f"execution request must be {MAIL_ACTION_EXECUTE_SCHEMA}")
    if provider is None:
        raise MailActionExecuteError("execution requires a provider")
    state = Path(state_dir).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)

    receipt_path = state / "executions.jsonl"
    lock_path = state / "locks" / f"execute-{req['plan_hash'][:16]}-{req['action_id']}.lock"
    with AdvisoryFileLock(lock_path):
        # Re-load inside the lock so a concurrent execution cannot replay
        # the same mutations twice.
        stored = _receipt_for_mutation(load_execution_receipts(receipt_path))
        results: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        for mutation in req.get("mutations") or []:
            prior = stored.get(mutation["idempotency_key"])
            if prior is not None and prior.get("status") in REPLAY_BLOCKING_STATUSES:
                # Idempotent replay: zero provider writes, stored receipt.
                results.append({
                    "mutation_id": mutation["mutation_id"],
                    "kind": mutation["kind"],
                    "status": prior.get("status"),
                    "verification": prior.get("verification"),
                    "idempotency_key": mutation["idempotency_key"],
                    "replayed": True,
                    "writes_performed": 0,
                    "rollback": prior.get("rollback"),
                })
            elif prior is not None and prior.get("status") in RETRYABLE_STATUSES:
                pending.append(mutation)
            elif prior is not None:
                # An unknown stored status fails closed: do not guess whether
                # the mutation happened.
                results.append({
                    "mutation_id": mutation["mutation_id"],
                    "kind": mutation["kind"],
                    "status": "blocked",
                    "verification": "unknown_prior_receipt_status",
                    "idempotency_key": mutation["idempotency_key"],
                    "replayed": True,
                    "writes_performed": 0,
                    "rollback": prior.get("rollback"),
                })
            else:
                pending.append(mutation)
        writes = 0
        if pending:
            flag_group = [m for m in pending if m["kind"] == "flag"]
            if flag_group:
                group_results, group_writes = _execute_flag_group(
                    req, flag_group, state_dir=state, provider=provider,
                    flag_overrides=flag_overrides, now=checked_at)
                results.extend(group_results)
                writes += group_writes
            archive_group = [m for m in pending if m["kind"] == "archive"]
            if archive_group:
                group_results, group_writes = _execute_archive_group(
                    req, archive_group, state_dir=state, provider=provider)
                results.extend(group_results)
                writes += group_writes
            direct_group = [m for m in pending if m["kind"] in ("label", "draft")]
            if direct_group:
                group_results, group_writes = _execute_direct_group(
                    req, direct_group, provider=provider)
                results.extend(group_results)
                writes += group_writes
        # Needs-human refusals from request validation ride along, unwritten.
        for refused in req.get("needs_human") or []:
            results.append({
                "mutation_id": refused["mutation_id"],
                "kind": refused["kind"],
                "status": "needs-human",
                "verification": refused.get("reason"),
                "idempotency_key": refused["idempotency_key"],
                "replayed": False,
                "writes_performed": 0,
                "rollback": None,
            })
        # Keep result order stable: request order first, refusals last.
        order = {m["mutation_id"]: i for i, m in enumerate(req.get("mutations") or [])}
        results.sort(key=lambda r: order.get(r["mutation_id"], len(order)))
        status = _result_status(results, req.get("needs_human") or [])
        receipt = build_execution_receipt(
            req, status=status, results=results, writes_performed=writes,
            now=checked_at)
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            with receipt_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError as e:
            raise MailActionExecuteError(f"execution receipt could not be written: {e}") from e
    return {
        "schema": MAIL_ACTION_EXECUTION_SCHEMA,
        "status": status,
        "action_id": req["action_id"],
        "plan_hash": req["plan_hash"],
        "account_id": req["account_id"],
        "writes_performed": writes,
        "results": results,
        "receipt_id": receipt["receipt_id"],
        "receipt_path": str(receipt_path),
        "needs_human": req.get("needs_human") or [],
    }
