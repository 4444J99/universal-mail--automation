# Cloudflare notice incidents

The existing `mail-intel` output includes `provider_notice_groups`. Recognized
usage warnings and exhaustion for one account, resource, reported budget period
and received UTC date share an incident identity. This is a mail-arrival cohort:
delivery time is a proxy, and `provider_usage_window` stays null. Daily and monthly
notices never share an identity. Unknown account bindings or dates do not silently
merge unrelated evidence. Sender-domain matching is not email authentication.

Update, security, billing, renewal and other notices retain separate categories.
Every source evidence ID remains available. Recognized usage alerts route through
the existing provider-action lane instead of becoming security work because the
subject contains the word Alert. The existing action planner carries the incident
identity into its grouping key. Non-incident findings keep their earlier grouping
and action identity; informational other notices do not create new action groups.

The existing history export command accepts `--source-type
apple_mail_cloudflare_index`. It reads sender, subject, time, read state and opaque
local identities from a read-only SQLite snapshot. It reads no bodies or
attachments and changes no source state. The default bound is 2000 matching rows;
an oversized query requires an explicit `--limit`. Date filtering happens before
the bound. Explicitly limited results carry incomplete coverage through the export
and intelligence output. Queries have a ten-second progress deadline and two-second
SQLite lock timeout. Unsupported schemas and failed observations remain errors.
The snapshot is a SQLite read transaction, not a copied database. WAL sidecars
must be accessible; inaccessible state fails instead of returning an empty result.
The adapter does not use immutable mode or copy only the primary file, either of
which could omit recent WAL data. See SQLite's
[snapshot isolation](https://sqlite.org/isolation.html) and
[read-only WAL requirements](https://sqlite.org/wal.html#read_only_databases).

On the managed macOS host, run protected access through the fixed responsible
identity from the UMA checkout and its configured Python environment:

```sh
domus-agent-host run -- python3 cli.py mail-history-export --source "$HOME/Library/Mail/V10/MailData/Envelope Index" --source-type apple_mail_cloudflare_index --body-char-limit 0 --output "$HOME/System/Reports/mail-history/cloudflare/history.json"
```

Use the existing `mail-intel` and `mail-action-plan` commands on that private export.
Raw source and derived operator state belong outside the checkout. The exporter
creates private files with mode 0600 on POSIX hosts; Windows permissions retain
their platform semantics. Do not infer mailbox moves, deletions, provider quota
health, resolved effects or sustained recovery from these read-only groups.

The October 27 local observation classified all 474 matching metadata records,
including 215 usage notices in 96 cohorts. Raw mail remains private. The sanitized
counts and file-integrity receipt are in
[`audit/2026-10-27-cloudflare-notice-groups.json`](../audit/2026-10-27-cloudflare-notice-groups.json).
