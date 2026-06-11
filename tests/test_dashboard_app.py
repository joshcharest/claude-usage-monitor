"""Smoke tests for the Streamlit dashboard.

Skipped unless the `dashboard` extra (streamlit) is installed. Uses Streamlit's
official headless AppTest harness — no browser, runs the real app.py top to
bottom against temp DBs.
"""

import json

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from claude_usage_monitor import db, indexer  # noqa: E402
from claude_usage_monitor.db import Sample  # noqa: E402

APP = "src/claude_usage_monitor/dashboard/app.py"


def test_modules_import():
    # Catches missing symbols / signature drift without running the app.
    import importlib

    for mod in ("prep", "data", "kpi", "panels", "app", "launch"):
        importlib.import_module(f"claude_usage_monitor.dashboard.{mod}")


def _seed(tmp_path, monkeypatch, with_samples=True):
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "budget.db"))
    projects = tmp_path / "projects" / "-home-josh-proj"
    projects.mkdir(parents=True)
    sid = "cccccccc-0000-0000-0000-000000000001"
    rec = lambda: {"type": "assistant", "sessionId": sid, "isSidechain": False,
                   "timestamp": "2026-06-01T10:00:00.000Z", "cwd": "/home/josh/proj",
                   "gitBranch": "main", "message": {"model": "claude-opus-4-8"}}
    (projects / f"{sid}.jsonl").write_text(
        json.dumps({"type": "ai-title", "sessionId": sid, "aiTitle": "Smoke session"})
        + "\n" + json.dumps(rec()) + "\n"
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    indexer.reindex()
    if with_samples:
        now = 1_000_000.0
        db.ingest(Sample(ts=now, session_id=sid, model="claude-opus-4-8", effort="high",
                         used_pct_5h=22.0, resets_at_5h=int(now + 9000),
                         used_pct_7d=56.0, resets_at_7d=int(now + 200000), cost_usd=3.2))


def test_app_runs_with_data(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, with_samples=True)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    # The six tabs render.
    labels = {t.label for t in at.tabs}
    assert {"Time", "Pace", "Usage", "Models", "Effort", "Conversations"} <= labels
    # KPI metrics present.
    assert len(at.metric) >= 3


def test_app_runs_empty(tmp_path, monkeypatch):
    # No transcripts, no samples — must not crash.
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    monkeypatch.setenv("CLAUDE_BUDGET_DB", str(tmp_path / "budget.db"))
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "empty"))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception


def test_app_runs_without_rate_limits(tmp_path, monkeypatch):
    # Samples present but no rate_limits (Free-tier shape) -> still no crash.
    _seed(tmp_path, monkeypatch, with_samples=False)
    db.ingest(Sample(ts=1_000_000.0, session_id="x", model="claude-opus-4-8"))
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
