"""Tests for parsing per-session cost from statusline-capture.jsonl."""

import json

from claude_usage_monitor.statusline_cost import load_session_costs


def _write(path, *records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_last_record_wins(tmp_path):
    cap = tmp_path / "cap.jsonl"
    _write(
        cap,
        {"session_id": "s1", "cost": {"total_cost_usd": 1.0}},
        {"session_id": "s1", "cost": {"total_cost_usd": 5.5},
         "workspace": {"repo": {"owner": "katomed", "name": "kato"}}},
        {"session_id": "s2", "cost": {"total_cost_usd": 2.0}},
    )
    costs = load_session_costs(cap)
    assert costs["s1"].cost_usd == 5.5  # last wins
    assert costs["s1"].repo == "katomed/kato"
    assert costs["s2"].cost_usd == 2.0
    assert costs["s2"].repo is None


def test_missing_file_returns_empty(tmp_path):
    assert load_session_costs(tmp_path / "nope.jsonl") == {}


def test_malformed_lines_skipped(tmp_path):
    cap = tmp_path / "cap.jsonl"
    cap.write_text('{"session_id": "s1", "cost": {"total_cost_usd": 3.0}}\n'
                   "garbage\n\n"
                   '{"no_session": true}\n')
    costs = load_session_costs(cap)
    assert set(costs) == {"s1"}
    assert costs["s1"].cost_usd == 3.0
