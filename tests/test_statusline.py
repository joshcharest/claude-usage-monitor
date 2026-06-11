"""Integration-ish tests for the statusline render path."""

from claude_usage_monitor import statusline
from claude_usage_monitor.config import load_config

SAMPLE = {
    "model": {"id": "claude-opus-4-8", "display_name": "Opus"},
    "cost": {"total_cost_usd": 0.42},
    "context_window": {"used_percentage": 8},
    "session_id": "test-session",
    "rate_limits": {
        "five_hour": {"used_percentage": 23.0, "resets_at": 1_000_000},
        "seven_day": {"used_percentage": 41.0, "resets_at": 2_000_000},
    },
}


def test_render_produces_line_and_records_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "s.db"))
    config = load_config()
    line = statusline.render(SAMPLE, now=999_000.0, config=config)
    assert "Opus" in line
    assert "ctx 8%" in line
    assert "5h 23%" in line
    assert "7d 41%" in line

    from claude_usage_monitor import db

    rows = db.recent(window_seconds=10_000, now=999_000.0)
    assert len(rows) == 1
    assert rows[0]["used_pct_5h"] == 23.0


def test_render_survives_empty_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "s.db"))
    config = load_config()
    line = statusline.render({}, now=1.0, config=config)
    assert isinstance(line, str)  # must not raise; degrades to "unknown" segments
    assert "5h —" in line


def test_7d_projection_from_period_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "p.db"))
    config = load_config()
    now = 1_000_000.0
    # Halfway through the 7d window, 30% used -> projects 60%.
    resets = now + 604800 / 2
    payload = {
        "model": {"display_name": "Opus"},
        "rate_limits": {"seven_day": {"used_percentage": 30.0, "resets_at": resets}},
    }
    line = statusline.render(payload, now=now, config=config)
    assert "7d 30%→proj 60%" in line


def test_missing_rate_limits_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "s.db"))
    config = load_config()
    payload = {"model": {"display_name": "Sonnet"}, "context_window": {"used_percentage": 5}}
    line = statusline.render(payload, now=1.0, config=config)
    assert "Sonnet" in line
    assert "5h —" in line
    assert "7d —" in line
