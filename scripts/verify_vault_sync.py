#!/usr/bin/env python3
"""
Diagnostic script to verify connectivity and bidirectional sync
between universal-mail--automation and 4444J99/estate-vault.

READ-ONLY BY DEFAULT. The historical write-probe pushed a checkpoint straight
into the canonical vault state path (universal-mail/labeler_state.json). That
is a state-mutation vector, not a connectivity diagnostic, so it is no longer
the default. This script now defaults to a read-only connectivity check.

To run a write/read-back probe, pass --probe-write. Writes are restricted to
paths under `universal-mail/diagnostics/` and are refused when running inside
CI. The production vault state path is never written by this tool.
"""

import os
import sys
import argparse
import logging
import subprocess
from datetime import datetime
from pathlib import Path

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vault_sync import VaultSync

READ_DEFAULT_PATH = "universal-mail/labeler_state.json"
WRITE_DEFAULT_PATH = "universal-mail/diagnostics/connectivity-check.json"
WRITE_PREFIX = "universal-mail/diagnostics/"


def get_token():
    token = os.environ.get("VAULT_PAT")  # allow-secret
    if token:
        return token
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()  # allow-secret
        if token:
            return token
    except Exception:
        pass
    return None


def probe_read(vault):
    """Pull remote state, collecting vault logger errors for honest exit codes."""
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                captured.append(record.getMessage())

    logger = logging.getLogger("core.vault_sync")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        return vault.pull(), captured
    finally:
        logger.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="Estate-vault connectivity diagnostic (read-only by default)")
    parser.add_argument(
        "--probe-write",
        action="store_true",
        help="Run a write/read-back probe against a universal-mail/diagnostics/ path (never CI, never production state)",
    )
    args = parser.parse_args()

    repo = os.environ.get("VAULT_REPO", "4444J99/estate-vault")
    path = os.environ.get("VAULT_PATH") or (WRITE_DEFAULT_PATH if args.probe_write else READ_DEFAULT_PATH)

    token = get_token()  # allow-secret
    if not token:
        print("[ERROR] No GitHub token found. Set VAULT_PAT or log in with `gh auth login`.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Connecting to Vault: {repo} (Path: {path})")
    vault = VaultSync(repo=repo, pat=token, path=path)

    print("[INFO] Probing remote state (read-only)...")
    existing, errors = probe_read(vault)
    if existing:
        print(f"[SUCCESS] Retrieved existing state: total_processed={existing.get('total_processed', 0)}, last_run={existing.get('last_run')}")
    elif errors:
        print(f"[ERROR] Failed to read vault state: {errors}", file=sys.stderr)
        sys.exit(1)
    else:
        print("[INFO] No state file found in vault at this path (or 404); read path is reachable.")

    if not args.probe_write:
        print("[SUCCESS] Read-only connectivity verification passed.")
        return 0

    # Write-probe guards: never in CI, never outside the diagnostics prefix.
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        print("[ERROR] --probe-write is not permitted in CI; this tool is read-only in CI.", file=sys.stderr)
        return 1
    if not path.startswith(WRITE_PREFIX):
        print(f"[ERROR] --probe-write may only target {WRITE_PREFIX}* (refusing: {path}).", file=sys.stderr)
        return 1

    checkpoint_data = {
        "next_page_token": None,
        "total_processed": 0,
        "history": {},
        "provider": "vault_verifier",
        "last_verified": datetime.now().isoformat(),
        "verifier_status": "OK",
    }

    print(f"[INFO] Testing push/commit to Vault diagnostics path: {path}")
    success = vault.push(checkpoint_data, commit_message=f"verify: connectivity probe {datetime.now().isoformat()}")
    if not success:
        print("[ERROR] Failed to push state checkpoint to vault.", file=sys.stderr)
        return 1

    print("[SUCCESS] Successfully committed state checkpoint to estate-vault diagnostics!")

    verify_pull, verify_errors = probe_read(vault)
    if verify_errors or not (verify_pull and verify_pull.get("verifier_status") == "OK"):
        print("[ERROR] Verification read failed or mismatched content.", file=sys.stderr)
        return 1
    print("[SUCCESS] Read-after-write verification passed perfectly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())