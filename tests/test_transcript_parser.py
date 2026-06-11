"""Tests for the pure streaming transcript parser."""

import io
import json

from claude_usage_monitor import transcript_parser as tp


def _jsonl(*records) -> io.BytesIO:
    body = "\n".join(json.dumps(r) for r in records) + "\n"
    return io.BytesIO(body.encode("utf-8"))


SID = "11111111-1111-1111-1111-111111111111"


def _assistant(ts, model, sidechain=False):
    return {
        "type": "assistant",
        "sessionId": SID,
        "isSidechain": sidechain,
        "timestamp": ts,
        "cwd": "/home/josh/proj",
        "gitBranch": "main",
        "version": "2.1.170",
        "message": {"model": model},
    }


def test_basic_session_fields():
    stream = _jsonl(
        {"type": "user", "sessionId": SID, "timestamp": "2026-06-01T10:00:00.000Z",
         "cwd": "/home/josh/proj", "gitBranch": "main"},
        _assistant("2026-06-01T10:00:05.000Z", "claude-opus-4-8"),
        {"type": "ai-title", "sessionId": SID, "aiTitle": "First title"},
        {"type": "ai-title", "sessionId": SID, "aiTitle": "Final title"},
        {"type": "system", "subtype": "turn_duration", "sessionId": SID,
         "timestamp": "2026-06-01T10:00:10.000Z", "durationMs": 5000, "messageCount": 2},
    )
    s = tp.parse_stream(stream)
    assert s.session_id == SID
    assert s.title == "Final title"  # last-wins
    assert s.cwd == "/home/josh/proj"
    assert s.git_branch == "main"
    assert s.version == "2.1.170"
    assert s.message_count == 2  # 1 user + 1 assistant
    assert s.turn_count == 1
    assert s.active_secs == 5.0
    assert tp.models_csv(s) == "claude-opus-4-8"
    assert s.started_at is not None and s.ended_at is not None
    assert s.ended_at > s.started_at
    assert tp.duration_secs(s) == 10.0  # 10:00:00 -> 10:00:10


def test_sidechain_does_not_clobber_main_context():
    stream = _jsonl(
        _assistant("2026-06-01T10:00:00.000Z", "claude-opus-4-8"),
        {**_assistant("2026-06-01T10:00:01.000Z", "claude-haiku-4-5", sidechain=True),
         "cwd": "/tmp/agent", "gitBranch": "sidebranch"},
    )
    s = tp.parse_stream(stream)
    assert s.cwd == "/home/josh/proj"  # NOT /tmp/agent
    assert s.git_branch == "main"  # NOT sidebranch
    assert s.message_count == 1  # only the main assistant
    assert s.sidechain_count == 1
    assert s.models[("claude-haiku-4-5", True)].message_count == 1
    assert s.models[("claude-opus-4-8", False)].message_count == 1
    assert tp.models_csv(s) == "claude-haiku-4-5,claude-opus-4-8"


def test_malformed_and_blank_lines_skipped():
    good = json.dumps(_assistant("2026-06-01T10:00:00.000Z", "claude-opus-4-8"))
    body = good + "\n" + "\n" + "{not json\n" + "[1,2,3]\n" + good + "\n"
    s = tp.parse_stream(io.BytesIO(body.encode("utf-8")))
    assert s.message_count == 2  # two valid assistant records, junk ignored


def test_unknown_system_subtype_tolerated():
    stream = _jsonl(
        {"type": "system", "subtype": "away_summary", "sessionId": SID,
         "timestamp": "2026-06-01T10:00:00.000Z"},
        _assistant("2026-06-01T10:00:05.000Z", "claude-opus-4-8"),
    )
    s = tp.parse_stream(stream)
    assert s.turn_count == 0  # away_summary is not turn_duration
    assert s.message_count == 1


def test_incremental_tail_parse_merges():
    first = _jsonl(_assistant("2026-06-01T10:00:00.000Z", "claude-opus-4-8"))
    s = tp.parse_stream(first)
    assert s.message_count == 1
    offset = s.bytes_consumed

    # Simulate the file being appended to: full content = first + new line.
    new_record = _assistant("2026-06-01T10:05:00.000Z", "claude-opus-4-8")
    full = first.getvalue() + (json.dumps(new_record) + "\n").encode("utf-8")
    merged = tp.parse_stream(io.BytesIO(full), start_offset=offset, into=s)
    assert merged.message_count == 2  # merged, not doubled
    assert merged.models[("claude-opus-4-8", False)].message_count == 2


def test_partial_trailing_line_not_consumed():
    good = json.dumps(_assistant("2026-06-01T10:00:00.000Z", "claude-opus-4-8"))
    # No trailing newline on the second record -> it is incomplete.
    body = (good + "\n" + good).encode("utf-8")
    s = tp.parse_stream(io.BytesIO(body))
    assert s.message_count == 1  # only the complete line
    assert s.bytes_consumed == len((good + "\n").encode("utf-8"))


def test_empty_stream():
    s = tp.parse_stream(io.BytesIO(b""))
    assert s.session_id is None
    assert s.message_count == 0
    assert tp.duration_secs(s) is None
