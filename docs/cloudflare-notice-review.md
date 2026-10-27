# Cloudflare grouping review

Native Agy independently read the PR #237 diff at
`3844a92b3d8713e63f6a5cc168008557d5665120`. Keeper graph readback confirms
review run `run-d5583cfbffed1a11b3fb1074c7abc0d3` succeeded, lease generation 1083
was released, and receipt `receipt-d5583cfbffed1a11-86540926` was accepted at
2026-10-27T00:37:58.676Z. This was source review; the reviewer did not rerun tests
or read the repository handoff. The implementer separately read the repository
instructions and handoff and validated the affected history, CLI, API,
intelligence and action paths.

| Finding | Disposition |
| --- | --- |
| Negative or zero limit could make SQL unbounded | Rejected as a public-path defect: build_mail_history_export already rejects int(limit) below 1 before source dispatch. A dedicated index-mode regression now verifies both values. The private helper is called only through that validated path. |
| Live WAL access may fail or omit recent data | Accepted the access limitation, rejected copying the primary file as a remedy. One explicit read transaction includes committed WAL data; inaccessible sidecars fail. A WAL regression verifies fresh committed rows are visible and primary/WAL bytes unchanged. Documentation links SQLite's isolation and read-only requirements. |
| Unknown-account nonusage notices can produce separate actions | Intended and now measured: the observed nonusage categories contain 12 billing, 3 security, 4 renewal and 3 update notices. Unknown bindings cannot justify merging distinct invoices or account actions. Other informational notices retain prior action grouping. |
| Subject categories and sender domains do not prove authentication | Accepted limitation, explicit in source and output. Classification is evidence routing, not verified provider identity or resolved effects. |
| Account hashes are unsalted | Accepted pseudonymization boundary: raw account data and group identities remain in private operator output. The public audit contains only counts, file hashes and implementation hashes. No anonymity claim is made. |
| Monthly notices still use received-day cohorts | Intended conservative proxy. Reported daily/monthly periods cannot merge, and the provider usage window stays unknown; mail arrival does not establish a billing/reset interval. |
| Export writer is non-atomic and follows caller-chosen destinations | The non-atomic writer remains outside a hostile-local-writer guarantee. The follow-up review below found that supporting a live SQLite source makes source/output alias rejection necessary; the writer now checks its open descriptor before mutation. The original observation used distinct private filenames. |
| POSIX private permissions apply to all raw exports | Accepted: every existing export can contain raw private mail. Supported platform permission semantics are documented. |
| Notice identity is computed twice per risk | Accepted minor computation cost at the measured 474-record scale; no performance claim or passing gate depends on removing it. |

The full Cloudflare recovery goal remains open. This receipt does not accept
uncertain product effects, live persistence migration or seven complete days.

## PR review follow-up (2026-10-27)

The two published review findings were reproduced against PR head
`41e40e4fe1f49b2647d962d2d0171d5caad4995c` using synthetic SQLite fixtures.
Both are accepted and addressed:

- Undated index notices disappeared when either date bound was supplied. The
  bounded SQL now preserves NULL dates in both the count and returned records,
  matching the normalizer's unknown-date behavior. Regressions cover the lower
  bound, upper bound, and both together, including truncated coverage.
- An export destination could identify the source index by the same path,
  symlink, or hardlink. The CLI captures the source identity before building the
  export and rejects existing aliases. The writer opens without truncation,
  compares the open descriptor against the original source identity and current
  source path before chmod, truncation, or writing, and rejects a destination
  that changes identity before or during writing. Index exports require the
  pre-read source path and identity at the public writer boundary. The canonical
  SQLite main, WAL, shared-memory and rollback-journal paths and existing inode
  identities are protected too. Existing non-index writer calls remain valid.

The initial six regression subcases failed before the date/early-alias fixes.
Independent review then requested a descriptor-level guard for aliases introduced
during export construction and destination replacement. After the local execution
environment returned, all 22 affected history and Cloudflare tests plus 27
subcases passed, including late symlink/hardlink aliases, replaced source paths,
destination replacement after opening or truncating the output, missing API
bindings, valid bound exports, and SQLite companion aliases. Scoped fatal Ruff
checks also passed. This local evidence covers the final source and tests;
required PR checks must still be evaluated on the published exact head.

No live mail, credentials, mailbox operations, or provider effects were used in
this follow-up. The earlier 474-message observation is historical evidence,
not a fresh observation under the follow-up revision.
