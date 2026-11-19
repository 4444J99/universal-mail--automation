# UMA Mail Action Execute v1

`uma.mail.action_execute.v1` is the bounded execution contract for one
approved action item from `uma.mail.action_plan.v1`. It is the only layer
that writes to a mailbox, and it never sends mail.

The plan layer never authorizes a mutation by itself. Execution requires a
separate, explicit approval carried on the execution request itself:
decision `approved`, a reason code, an actor, and the action id it approves.
A missing, non-approved, or misdirected approval fails closed before any
provider is touched.

## Entry Points

- Core: `core.mail_action_execute.build_execution_request(...)`
- Core: `core.mail_action_execute.execute_action(request, state_dir=..., provider=...)`
- Receipts: `core.mail_action_execute.load_execution_receipts(...)`
- Follow-on plan layer: [mail-action-plan-v1.md](mail-action-plan-v1.md)
- Follow-on ledger layer: [mail-action-ledger-v1.md](mail-action-ledger-v1.md)

## Execution Request Shape

```json
{
  "schema": "uma.mail.action_execute.v1",
  "status": "ok",
  "plan_hash": "<sha256 of the action plan>",
  "action_id": "action_...",
  "account_id": "acct",
  "approval": {
    "decision": "approved",
    "reason_code": "ready_to_send",
    "actor": "operator",
    "receipt_id": "draftapproval_...",
    "action_id": "action_..."
  },
  "mutations": [],
  "needs_human": [],
  "flag_plan": {},
  "flag_approval": {},
  "flag_canary_approval": {},
  "archive_plan": {},
  "archive_approval": {},
  "checked_at": "2026-11-19T23:00:00Z"
}
```

One account per request; every mutation's `account_id` must match it.
At most 25 mutations per request.

## Mutation Kinds

| Kind | Back-end | Canary | Verification | Rollback |
| --- | --- | --- | --- | --- |
| `flag` | `core.flag_transactions.TransactionEngine` (canary, then full apply) | First 1..3 via `apply_canary_transaction` | `core.flag_verification.verify_transactions` | `ScopedRollbackEngine` with transaction-bound receipts |
| `archive` | `core.archive_transactions.ArchiveEngine` | Canary approval (1..3, archive batches are canary-sized by engine design) | `ArchiveEngine.verify` | `ArchiveEngine.rollback` |
| `label` | Thin wrapper in this module over `provider.apply_label` | First 1..3, then the rest | Re-read in the target mailbox | Re-apply the original mailbox |
| `draft` | Thin wrapper in this module over `provider.create_draft` | First 1..3, then the rest | Provider save confirmation (drafts cannot be re-read through the provider surface) | None — delete the draft by hand; the receipt says so |

A batch of more than three flag mutations needs a second, canary-sized
`flag_canary_approval` approving exactly the first three mutations (same
plan, snapshot, policy, and operator as the full approval). Transaction ids
bind the approval hash, so a mutation applied under the canary approval
rolls back under that approval.

## Guarantees

- **Approval check first.** No provider is touched without an explicit
  `approved` decision. Action items whose `approval_type` is
  `portal_verification`, `human_legal_review`, or `external_reconcile`
  are not executable here at all; the official portal, legal review, or
  external system owns those actions.
- **Compare-and-set pre-read.** Every mutation of an existing message
  re-reads live state before writing and aborts as `conflicted` when the
  evidence digest moved. A mutation with no evidence digest is
  `uncertain` and is refused as needs-human.
- **Protected / overridden messages are never written.** They are
  surfaced in `needs_human` with the reason (`protected_message`,
  `human_override`, `uncertain_no_evidence_digest`).
- **Canary-first.** The first 1..3 mutations run first and are verified
  before the rest rolls out. A canary failure blocks the batch, rolls
  back the canary writes that landed, and records the rest as `blocked`.
- **Idempotency.** The key is deterministic per (plan hash, action id,
  mutation id). Replaying an already-verified (or created) mutation
  performs zero provider writes and returns the stored receipt. A stored
  `uncertain` / `ambiguous` receipt also blocks replay (fail closed: the
  mutation may have happened). `conflicted`, `failed`, `rolled_back`,
  and `blocked` receipts allow a fresh attempt.
- **Post-write verification + mismatch/rollback.** After each write the
  executor re-reads and compares against the intended state; on mismatch
  it rolls the mutation back and records `conflicted` with the evidence.
- **Recoverable receipts.** Every execution appends a
  `uma.mail.action_execution_receipt.v1` receipt to
  `<state_dir>/executions.jsonl` recording the plan hash, mutation ids,
  verification status per mutation, and the rollback pointer (flag plan
  hash, archive plan hash, execution receipt id).

## Execution Receipt Shape

```json
{
  "schema": "uma.mail.action_execution_receipt.v1",
  "status": "recorded",
  "receipt_id": "exreceipt_...",
  "created_at": "2026-11-19T23:00:00Z",
  "action_id": "action_...",
  "plan_hash": "<sha256>",
  "account_id": "acct",
  "approval": {"decision": "approved", "reason_code": "ready_to_send", "actor": "operator"},
  "execution_status": "verified",
  "mutations": [
    {
      "mutation_id": "mut_...",
      "kind": "flag",
      "status": "verified",
      "verification": "verified",
      "idempotency_key": "idem_...",
      "replayed": false,
      "writes_performed": 1,
      "rollback": null
    }
  ],
  "writes_performed": 1,
  "rollback_pointer": {
    "flag_plan_hash": "<sha256>",
    "archive_plan_hash": null,
    "execution_receipt_id": "exreceipt_..."
  },
  "privacy": {"redacted": true, "raw_mail_printed": false},
  "safety": {"send_allowed": false, "automated_send_allowed": false}
}
```

The receipt is redacted: it carries ids, digests, and statuses, never raw
sender names, addresses, subjects, bodies, snippets, or full local paths.

## What Is Out of Scope

- **Any programmatic send.** This module never sends mail and never
  imports a send path. Draft creation only ever saves to Drafts; the
  human presses send in Mail.app (`send_authority:
  human_click_in_mailapp`, `automated_send_allowed: false` in
  `config/mail-execution-policy.json`). The real-account canary from the
  parent release (#226) is an operator-run step on Anthony's account
  after merge, using the canary mechanism this layer builds — it is not
  part of a PR and must never be substituted with a faked run.
- **The Cloudflare token / share-demo deploy failure** (Anthony's
  provisioning).
- **R4/R5 (#231/#230)** and the #205 billing-lockout intention.

## Safety Boundary

The executor writes only the mutations named in one approved execution
request, on one account, through the four back-ends above. It never sends,
never widens scope to another account, and never acts on a protected,
overridden, or uncertain message.
