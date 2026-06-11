#!/usr/bin/env python3
"""Schema-capture helper (Phase 1, Step 0).

Temporarily wire this as the Claude Code `statusLine.command` to record the
REAL statusline JSON payloads, so we can confirm the exact field names before
relying on them. It appends each payload (one JSON object per line) to
~/.claude/statusline-capture.jsonl and prints a trivial status line.

Usage in ~/.claude/settings.json:
    "statusLine": {
      "type": "command",
      "command": "python3 /home/josh/claude-usage-monitor/scripts/capture-statusline.py"
    }

This file writes only to a gitignored capture log; it never crashes the session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CAPTURE = Path.home() / ".claude" / "statusline-capture.jsonl"


def main() -> int:
    raw = sys.stdin.read()
    try:
        CAPTURE.parent.mkdir(parents=True, exist_ok=True)
        with CAPTURE.open("a") as fh:
            # Store the raw line as-is; re-dump compactly if it parses.
            try:
                fh.write(json.dumps(json.loads(raw)) + "\n")
            except Exception:
                fh.write(raw.rstrip("\n") + "\n")
    except Exception:
        pass

    # Minimal visible status line.
    try:
        payload = json.loads(raw)
        model = (payload.get("model") or {}).get("display_name", "?")
    except Exception:
        model = "?"
    print(f"[capture] {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
