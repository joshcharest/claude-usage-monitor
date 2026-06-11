"""Supplemental per-session cost/repo from ``statusline-capture.jsonl``.

The statusline payload carries ``cost.total_cost_usd`` (the session's running
total) and ``workspace.repo``. We read the capture log (a small append-only file)
and keep the LAST record per session. This is the only local source of per-session
USD cost — transcripts have none — and only covers sessions the monitor observed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_CAPTURE = Path.home() / ".claude" / "statusline-capture.jsonl"


@dataclass
class SessionCost:
    cost_usd: float | None = None
    repo: str | None = None


def _repo_str(workspace: dict | None) -> str | None:
    repo = (workspace or {}).get("repo")
    if isinstance(repo, dict):
        owner, name = repo.get("owner"), repo.get("name")
        if owner and name:
            return f"{owner}/{name}"
        if name:
            return str(name)
    return None


def load_session_costs(path: Path | None = None) -> dict[str, SessionCost]:
    """Return ``{session_id: SessionCost}`` (last record wins).

    A missing or unreadable file degrades to an empty dict.
    """
    path = path or _DEFAULT_CAPTURE
    out: dict[str, SessionCost] = {}
    if not path.exists():
        return out
    try:
        with path.open("rb") as fh:
            for raw in fh:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                sid = rec.get("session_id")
                if not isinstance(sid, str):
                    continue
                cost = (rec.get("cost") or {}).get("total_cost_usd")
                out[sid] = SessionCost(
                    cost_usd=cost if isinstance(cost, (int, float)) else None,
                    repo=_repo_str(rec.get("workspace")),
                )
    except OSError:
        return out
    return out
