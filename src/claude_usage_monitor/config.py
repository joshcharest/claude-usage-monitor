"""Load configuration from the shipped template, overlaid with a local override.

Resolution order (later wins):
1. ``config/budget.toml`` bundled in the repo (placeholder defaults).
2. ``~/.claude/budget.local.toml`` (gitignored personal override), if present.
3. ``$CLAUDE_BUDGET_CONFIG`` (explicit path), if set — used mainly by tests.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

_REPO_TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "budget.toml"
_LOCAL_OVERRIDE = Path.home() / ".claude" / "budget.local.toml"


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``over`` into a copy of ``base``."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config() -> dict[str, Any]:
    """Return the merged configuration dict.

    Missing files are skipped silently so the tool keeps working with defaults.
    """
    config: dict[str, Any] = {}
    if _REPO_TEMPLATE.exists():
        config = _read_toml(_REPO_TEMPLATE)
    if _LOCAL_OVERRIDE.exists():
        config = _deep_merge(config, _read_toml(_LOCAL_OVERRIDE))
    explicit = os.environ.get("CLAUDE_BUDGET_CONFIG")
    if explicit and Path(explicit).exists():
        config = _deep_merge(config, _read_toml(Path(explicit)))
    return config
