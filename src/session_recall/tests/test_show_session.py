"""Tests for show_session command — C1 fix validation."""
import sqlite3
import argparse
import pytest
from unittest.mock import patch
from session_recall.commands.show_session import run


def _seed_db(conn):
    """Create schema and seed test data."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER)")
    conn.execute("INSERT OR REPLACE INTO schema_version VALUES (2)")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, repository TEXT, branch TEXT, summary TEXT,
        created_at TEXT, updated_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS turns (
        session_id TEXT, turn_index INTEGER, user_message TEXT,
        assistant_response TEXT, timestamp TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS session_files (
        session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER,
        first_seen_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS session_refs (
        session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER,
        created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
        session_id TEXT, checkpoint_number INTEGER, title TEXT,
        overview TEXT, created_at TEXT)""")
    conn.execute("INSERT INTO sessions VALUES ('abcd1234-0000-0000-0000-000000000000','repo','main','test session','2026-04-17','2026-04-17')")
    for i in range(10):
        conn.execute("INSERT INTO turns VALUES (?,?,?,?,?)",
                     ('abcd1234-0000-0000-0000-000000000000', i, f'user msg {i}', f'assistant msg {i}', '2026-04-17'))
    conn.commit()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_db(conn)
    conn.close()
    return db_path


def _make_args(session_id, turns=None, json_mode=False, full=False):
    return argparse.Namespace(
        session_id=session_id, turns=turns, json=json_mode, full=full
    )


def test_turns_positive_limits_correctly(seeded_db, capsys):
    """--turns 3 should return exactly 3 turns on a session with 10."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234', turns=3, json_mode=True))
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert len(out["turns"]) == 3


def test_turns_zero_returns_no_turns(seeded_db, capsys):
    """--turns 0 should return zero turns."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234', turns=0, json_mode=True))
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert len(out["turns"]) == 0


def test_turns_none_returns_all(seeded_db, capsys):
    """Omitting --turns should return all 10 turns."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234', turns=None, json_mode=True))
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert len(out["turns"]) == 10


def test_turns_exceeding_count_returns_all(seeded_db, capsys):
    """--turns 9999 should return all 10 (not crash)."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234', turns=9999, json_mode=True))
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert len(out["turns"]) == 10


def test_argparse_rejects_negative_turns():
    """Argparse should reject --turns -1 before run() is called."""
    from session_recall.__main__ import _non_negative_int
    with pytest.raises((argparse.ArgumentTypeError, SystemExit, ValueError)):
        _non_negative_int("-1")


# --- H3 fix: Session ID validation tests ---

def test_show_rejects_percent(seeded_db, capsys):
    """'%' should be rejected as invalid session ID."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('%'))
    assert rc == 2
    assert 'invalid session id' in capsys.readouterr().err.lower()


def test_show_rejects_underscore(seeded_db, capsys):
    """'_' should be rejected as invalid session ID."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('_'))
    assert rc == 2


def test_show_rejects_empty(seeded_db, capsys):
    """Empty string should be rejected."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args(''))
    assert rc == 2


def test_show_rejects_non_hex(seeded_db, capsys):
    """Non-hex characters should be rejected."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('not-a-uuid'))
    assert rc == 2


def test_show_rejects_short(seeded_db, capsys):
    """Less than 4 chars should be rejected."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abc'))
    assert rc == 2


def test_show_accepts_hex_prefix(seeded_db, capsys):
    """8-char hex prefix should find the session."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234', json_mode=True))
    assert rc == 0


def test_show_accepts_full_uuid(seeded_db, capsys):
    """Full UUID with dashes should find the session."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('abcd1234-0000-0000-0000-000000000000', json_mode=True))
    assert rc == 0


def test_show_case_insensitive(seeded_db, capsys):
    """Uppercase hex should work (lowered internally)."""
    with patch('session_recall.commands.show_session.DB_PATH', seeded_db):
        rc = run(_make_args('ABCD1234', json_mode=True))
    assert rc == 0
