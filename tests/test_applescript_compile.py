"""Compile-validation for the macOS Mail AppleScript assets.

The root ``*.applescript`` helpers (archive_old_inbox, export_mail_snapshot,
flag_important_senders, route_bulk_senders) are invoked by name from the run
scripts and from ``providers/mailapp.py``. A syntax error or a stale reference
in one of them silently breaks the automation at runtime — the parser runs per
Tool call, never at import. ``osacompile`` parses the full script without
executing it, which is the cheapest offline gate for those assets.

These tests are macOS-only on purpose: AppleScript and osacompile do not exist
on Linux/Windows, and running them there would be a false failure, so the
whole module skips unless /usr/bin/osacompile is present.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

osacompile = shutil.which("osacompile")

pytestmark = pytest.mark.skipif(
    osacompile is None,
    reason="osacompile unavailable (not macOS or no AppleScript runtime)",
)


def test_every_root_applescript_compiles():
    scripts = sorted(ROOT.glob("*.applescript"))
    assert scripts, "expected at least one *.applescript at the repo root"
    failed = []
    with tempfile.TemporaryDirectory() as tmp:
        for script in scripts:
            out = Path(tmp) / f"{script.stem}.scpt"
            result = subprocess.run(
                [osacompile, "-o", str(out), str(script)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0 or not out.exists():
                failed.append((script.name, result.stderr.strip()))
    assert not failed, [f"{name}: {err}" for name, err in failed]