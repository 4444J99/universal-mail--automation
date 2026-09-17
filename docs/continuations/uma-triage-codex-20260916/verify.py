#!/usr/bin/env python3
"""Read-only review-closeout predicate; not an inbox or runtime acceptance test."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def require(condition, message):
    if not condition:
        raise SystemExit(f"FAIL: {message}")


def command(root, *args):
    return subprocess.check_output(args, cwd=root, text=True, timeout=30).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-published", action="store_true")
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    root = Path(command(packet, "git", "rev-parse", "--show-toplevel"))
    slug = "uma-triage-codex-20260916"
    branch = f"docs/{slug}"
    capsule = root / ".limen-workstream"
    require(command(root, "git", "branch", "--show-current") == branch, "wrong branch")
    for name in ("README.md", "plan.md", "launch.sh", "workstream.json"):
        require((packet / name).is_file(), f"missing continuation module: {name}")
    for name in (
        "2026-09-08-evidence-driven-mail.md",
        "2026-09-08-four-account-rollout-v3.md",
        "2026-09-10-full-gap-coverage.md",
    ):
        require((root / ".codex/plans" / name).is_file(), f"missing owner plan: {name}")
    require(shutil.which("codex") is not None, "Codex CLI unavailable")
    contract = json.loads((capsule / "workstream.json").read_text())
    receipt = json.loads((packet / "workstream.json").read_text())
    identity = json.loads((capsule / "capsule.identity").read_text())
    require(receipt["schema"] == "limen.workstream.receipt.v1", "receipt schema")
    require(receipt["slug"] == slug and receipt["branch"] == branch, "receipt identity")
    require(receipt["workstream"] == "uma-inbox-operational-completion", "workstream owner")
    require(receipt["contract"] == contract, "private/public contract mismatch")
    require(contract["runway"]["duration_seconds"] == 1800, "finite runway differs")
    require("public_send" in contract["authorization"]["retained_gates"], "send gate missing")
    require(identity["schema"] == "limen.workstream.capsule-identity.v2", "identity schema")
    expected_modules = {
        "README.md", "manifest.md", "workstream.json", "workstream-contract.py",
        "intent.md", "runtime.md", "closeout.md", "kickstart.sh",
    }
    require(set(identity["modules"]) == expected_modules, "identity module set differs")
    for name, digest in identity["modules"].items():
        require(hashlib.sha256((capsule / name).read_bytes()).hexdigest() == digest, f"capsule changed: {name}")
    command(root, "bash", "-n", str(packet / "launch.sh"))
    command(root, "bash", "-n", str(capsule / "kickstart.sh"))
    if args.require_published:
        require(contract["runway"]["started_epoch"] is None, "capsule already admitted; review predicate is stale")
        require(contract["runway"]["deadline_epoch"] is None, "unstarted deadline differs")
        require(not command(root, "git", "status", "--porcelain", "--untracked-files=all"), "dirty worktree")
        head = command(root, "git", "rev-parse", "HEAD")
        remote = command(root, "git", "ls-remote", "origin", f"refs/heads/{branch}")
        require(bool(remote) and remote.split()[0] == head, "remote does not preserve HEAD")
        for name in ("README.md", "plan.md", "verify.py", "launch.sh", "workstream.json"):
            relative = (packet / name).relative_to(root)
            command(root, "git", "cat-file", "-e", f"HEAD:{relative}")
    print("PASS: review continuation intact; no mail access, launch, admission, or writes performed")


if __name__ == "__main__":
    main()
