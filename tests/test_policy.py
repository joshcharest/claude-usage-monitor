"""Tests for the dormant Phase 2 model/effort recommendation policy.

The policy is not shown in the status line yet; these keep the seam covered.
"""

from claude_usage_monitor.config import load_config
from claude_usage_monitor.policy import recommend


def test_tiers_from_default_config():
    config = load_config()
    assert recommend(10.0, config) == _rec("opus", "high")  # <= 50%
    assert recommend(70.0, config) == _rec("sonnet", "medium")  # <= 80%
    assert recommend(95.0, config) == _rec("haiku", "low")  # > 80%


def test_boundaries_are_inclusive():
    config = load_config()
    assert recommend(50.0, config).model == "opus"
    assert recommend(80.0, config).model == "sonnet"


def test_unknown_usage_returns_none():
    assert recommend(None, load_config()) is None


def _rec(model, effort):
    from claude_usage_monitor.policy import Recommendation

    return Recommendation(model=model, effort=effort)
