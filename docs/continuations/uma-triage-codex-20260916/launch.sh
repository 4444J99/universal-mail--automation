#!/usr/bin/env bash
set -euo pipefail
packet_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
worktree="$(git -C "$packet_dir" rev-parse --show-toplevel)"
case "${1:---check}" in
  --check)
    exec python3 "$packet_dir/verify.py"
    ;;
  --launch)
    python3 "$packet_dir/verify.py"
    exec bash "$worktree/.limen-workstream/kickstart.sh"
    ;;
  *)
    printf 'Usage: bash launch.sh [--check|--launch]\n' >&2
    exit 2
    ;;
esac
