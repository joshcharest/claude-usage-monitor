"""Tests for the conversation-index SQLite store."""

from claude_usage_monitor import index_db


def test_schema_created_on_first_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    conn = index_db.connect()
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert {"index_files", "conversations", "model_usage", "index_meta"} <= tables


def test_meta_roundtrip_and_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(tmp_path / "idx.db"))
    conn = index_db.connect()
    assert index_db.get_meta(conn, "missing") is None
    assert index_db.get_meta(conn, "missing", "fallback") == "fallback"

    index_db.set_meta(conn, "schema_version", "1")
    assert index_db.get_meta(conn, "schema_version") == "1"
    index_db.set_meta(conn, "schema_version", "2")  # upsert
    assert index_db.get_meta(conn, "schema_version") == "2"

    assert index_db.bump_generation(conn) == 1
    assert index_db.bump_generation(conn) == 2
    assert index_db.get_meta(conn, "generation") == "2"
    conn.close()


def test_path_honors_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("CLAUDE_USAGE_INDEX_DB", str(target))
    assert index_db.index_db_path() == target
