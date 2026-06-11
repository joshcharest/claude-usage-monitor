"""Model/effort recommendation policy (Phase 2 seam).

Phase 1 only *computes* a recommendation so it can be displayed. It does NOT
change the model or effort — that is impossible mid-session in interactive
Claude Code and is deferred to a Phase 2 preflight launcher or Agent SDK harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Recommendation:
    model: str
    effort: str


_FALLBACK = Recommendation(model="haiku", effort="low")


def recommend(used_pct: float | None, config: dict[str, Any]) -> Recommendation | None:
    """Pick the tier whose ``max_used_pct`` first covers ``used_pct``.

    Returns ``None`` when usage is unknown. Tiers come from
    ``[[policy.tiers]]`` in config; they are evaluated in ascending
    ``max_used_pct`` order.
    """
    if used_pct is None:
        return None

    tiers = (config.get("policy", {}) or {}).get("tiers", []) or []
    for tier in sorted(tiers, key=lambda t: t.get("max_used_pct", 100.0)):
        if used_pct <= tier.get("max_used_pct", 100.0):
            return Recommendation(
                model=tier.get("model", _FALLBACK.model),
                effort=tier.get("effort", _FALLBACK.effort),
            )
    return _FALLBACK
