# Cloudflare notice grouping

Owner: universal-mail--automation, advancing the accepted Cloudflare recovery plan.
Work branch: work/heal/cloudflare-notice-incidents-20261027. Preserve original mail,
all existing findings and IDs outside this narrow source correction, and all sender,
body, credential and account privacy boundaries.

1. Extend existing historical intelligence with source-backed Cloudflare notice
   cohorts. Usage notices group by account binding, resource and received UTC date;
   this date is a mail-arrival proxy, never a verified provider reset window.
   Keep update, security, billing, renewal and other notices separate. Preserve all
   evidence IDs and source timestamps. Unknown account/window observations remain
   explicit and cannot silently merge unrelated notices.
2. Carry incident identity into the existing action planner so repeated usage
   warnings and exhaustion for one cohort share an owner action. Preserve existing
   grouping and action IDs for non-incident findings. Known usage alerts must not be
   classified as account-security notices merely because the subject says Alert.
3. Add a bounded, read-only Cloudflare metadata export adapter for the existing
   Apple Mail index and existing history normalizer. Use the fixed DomusAgentHost
   identity for protected access. No body/attachment export, mailbox moves, deletes
   or sends. Store private source and derived state outside the checkout, at the
   existing reports surface; publish only sanitized integrity/count receipts.
4. Test duplicate thresholds/exhaustion, resource/account/date separation, UTC
   boundaries, missing dates/accounts, spoofed/non-provider senders, distinct notice
   categories, privacy and unchanged non-incident action identity. Validate against
   the current bounded Cloudflare-only metadata inventory, preserving completeness
   and deduplication qualifications. Publish through the repository's branch/PR
   path; source review and live classification are separate evidence.

The full recovery goal remains open: grouping old mail does not resolve uncertain
product effects, authorize spending, prove current quotas or establish seven days.
