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
| Export writer is non-atomic and follows caller-chosen destinations | Existing writer semantics retained. This observation used a new private run directory and new filenames, preserving every source and prior derivative. No atomic-custody or hostile-local-writer guarantee is claimed by this grouping change. |
| POSIX private permissions apply to all raw exports | Accepted: every existing export can contain raw private mail. Supported platform permission semantics are documented. |
| Notice identity is computed twice per risk | Accepted minor computation cost at the measured 474-record scale; no performance claim or passing gate depends on removing it. |

The full Cloudflare recovery goal remains open. This receipt does not accept
uncertain product effects, live persistence migration or seven complete days.
