"""Tests for the incremental transcript indexer."""

import json

import pytest

from claude_usage_monitor import index_db, indexer

SID = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    return projects


def _assistant(ts, model="claude-opus-4-8", sid=SID):
    return {"type": "assistant", "sessionId": sid, "isSidechain": False,
            "timestamp": ts, "cwd": "/home/josh/proj", "gitBranch": "main",
            "version": "2.1.170", "message": {"model": model}}


def _write_session(projects, slug, sid, records):
    proj = projects / slug
    proj.mkdir(exist_ok=True)
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _conv(sid):
    conn = index_db.connect()
    row = conn.execute(
        "SELECT * FROM conversations WHERE session_id = ?", (sid,)
    ).fetchone()
    conn.close()
    return row


def test_index_new_session(env):
    _write_session(env, "-home-josh-proj", SID, [
        {"type": "ai-title", "sessionId": SID, "aiTitle": "My session"},
        _assistant("2026-06-01T10:00:00.000Z"),
        {"type": "system", "subtype": "turn_duration", "sessionId": SID,
         "timestamp": "2026-06-01T10:00:08.000Z", "durationMs": 8000},
    ])
    result = indexer.reindex()
    assert result.files_indexed == 1
    assert result.rows_written == 1
    row = _conv(SID)
    assert row["title"] == "My session"
    assert row["cwd"] == "/home/josh/proj"
    assert row["message_count"] == 1
    assert row["turn_count"] == 1
    assert row["active_secs"] == 8.0
    assert row["models_csv"] == "claude-opus-4-8"
    assert row["slug"] == "-home-josh-proj"


def test_unchanged_files_skipped(env):
    _write_session(env, "-p", SID, [_assistant("2026-06-01T10:00:00.000Z")])
    indexer.reindex()
    second = indexer.reindex()
    assert second.files_seen == 1
    assert second.files_skipped == 1
    assert second.files_indexed == 0


def test_append_merges_not_doubles(env):
    path = _write_session(env, "-p", SID, [_assistant("2026-06-01T10:00:00.000Z")])
    indexer.reindex()
    assert _conv(SID)["message_count"] == 1

    # Append a new record to the same file.
    with path.open("a") as fh:
        fh.write(json.dumps(_assistant("2026-06-01T10:30:00.000Z")) + "\n")
    result = indexer.reindex()
    assert result.files_indexed == 1
    row = _conv(SID)
    assert row["message_count"] == 2  # merged, not doubled
    # ended_at advanced to the later record.
    assert row["duration_secs"] == pytest.approx(1800.0)  # 30 min

    conn = index_db.connect()
    mu = conn.execute(
        "SELECT message_count FROM model_usage WHERE session_id=?", (SID,)
    ).fetchone()
    conn.close()
    assert mu["message_count"] == 2


def test_shrink_triggers_full_reparse(env):
    path = _write_session(env, "-p", SID, [
        _assistant("2026-06-01T10:00:00.000Z"),
        _assistant("2026-06-01T10:01:00.000Z"),
        _assistant("2026-06-01T10:02:00.000Z"),
    ])
    indexer.reindex()
    assert _conv(SID)["message_count"] == 3

    # Rewrite with fewer records (file shrinks) -> full reparse.
    path.write_text(json.dumps(_assistant("2026-06-02T09:00:00.000Z")) + "\n")
    indexer.reindex()
    assert _conv(SID)["message_count"] == 1  # reflects new content only


def test_max_files_caps(env):
    for i in range(3):
        sid = f"bbbbbbbb-0000-0000-0000-00000000000{i}"
        _write_session(env, "-p", sid, [_assistant("2026-06-01T10:00:00.000Z", sid=sid)])
    result = indexer.reindex(max_files=1)
    assert result.files_indexed == 1
    assert result.capped is True

    # A follow-up pass picks up the rest.
    rest = indexer.reindex(max_files=10)
    assert rest.files_indexed == 2
    assert rest.capped is False


def test_enrich_costs_from_capture(env, tmp_path):
    _write_session(env, "-p", SID, [_assistant("2026-06-01T10:00:00.000Z")])
    capture = tmp_path / "cap.jsonl"
    capture.write_text(json.dumps({
        "session_id": SID, "cost": {"total_cost_usd": 12.5},
        "workspace": {"repo": {"owner": "katomed", "name": "kato"}},
    }) + "\n")
    indexer.reindex(capture_path=capture)
    row = _conv(SID)
    assert row["cost_usd"] == 12.5
    assert row["repo"] == "katomed/kato"


def test_generation_bumps_on_write(env):
    _write_session(env, "-p", SID, [_assistant("2026-06-01T10:00:00.000Z")])
    indexer.reindex()
    conn = index_db.connect()
    gen = index_db.get_meta(conn, "generation")
    conn.close()
    assert gen == "1"
