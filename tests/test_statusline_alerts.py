"""Statusline <-> alert-engine wiring tests (scoped; does not touch the dashboard)."""

from __future__ import annotations

from claude_usage_monitor import db, statusline


def _config():
    return {
        "forecast": {"window_5h_seconds": 18000, "window_7d_seconds": 604800},
        "alerts": {
            "enabled": True,
            "notify": True,
            "warn_projected_pct": 90.0,
            "alert_over_pct": 125.0,
            "spike_pp_per_min": 5.0,
            "spike_window_seconds": 600.0,
            "spike_bucket_seconds": 30.0,
            "spike_min_points": 4,
            "cooldown_seconds": 1800.0,
            "clear_factor": 0.9,
        },
    }


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "budget.db"))
    monkeypatch.setenv("CLAUDE_ALERT_STATE", str(tmp_path / "alert-state.json"))
    monkeypatch.setenv("CLAUDE_ALERT_NO_NOTIFY", "1")  # no real notify-send


def test_statusline_prepends_over_marker(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    now = 1_000_000.0
    # 50% through the 5h window, 70% used -> projects 140% -> "way over" (>=125).
    payload = {
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 70.0, "resets_at": now + 9000},
        },
    }
    line = statusline.render(payload, now=now, config=_config())
    assert "🚨 5h proj 140%" in line
    # The normal segment is still present after the marker.
    assert "5h 70%" in line
    assert line.index("🚨") < line.index("5h 70%")


def test_statusline_no_marker_when_calm(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    now = 1_000_000.0
    payload = {
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 30.0, "resets_at": now + 9000},
        },
    }
    line = statusline.render(payload, now=now, config=_config())
    assert "🚨" not in line


def test_statusline_spike_marker_from_db_history(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    now = 1_000_000.0
    reset = now + 18000
    # Seed a steep FRESH climb in the DB (8 pp/min over 6 minutes).
    for i in range(6):
        ts = now - (5 - i) * 60
        used = 20.0 + 8.0 * i  # +8 per minute
        db.ingest(
            db.Sample(
                ts=ts, session_id="s", used_pct_5h=used, resets_at_5h=reset
            )
        )
    # Final tick at `now` continues the climb.
    payload = {
        "model": {"display_name": "Opus"},
        "rate_limits": {"five_hour": {"used_percentage": 60.0, "resets_at": reset}},
    }
    line = statusline.render(payload, now=now, config=_config())
    assert "🚨 5h spiking" in line


def test_statusline_alerts_disabled(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    cfg = _config()
    cfg["alerts"]["enabled"] = False
    now = 1_000_000.0
    payload = {
        "model": {"display_name": "Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 70.0, "resets_at": now + 9000},
        },
    }
    line = statusline.render(payload, now=now, config=cfg)
    assert "🚨" not in line
