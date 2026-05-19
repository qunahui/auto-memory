"""Build/query a SQLite index over Zed threads.db payloads."""

from __future__ import annotations

import os
import pathlib
import re
import sqlite3
import time
from typing import Optional

from .reader import parse_thread_row

_TRUST_LEVEL = "zed_threads_db"
_FTS5_SPECIAL = re.compile(r'[.\-(){}\[\]^~*:"+/\\@#$%&!?<>=|]')


def _sanitize_fts5_query(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped:
        return None
    tokens = stripped.split()
    safe_tokens = []
    for tok in tokens:
        escaped = tok.replace('"', '""')
        if _FTS5_SPECIAL.search(tok):
            safe_tokens.append(f'"{escaped}"')
        else:
            safe_tokens.append(f"{escaped}*")
    return " ".join(safe_tokens)


_DDL = [
    """CREATE TABLE IF NOT EXISTS zed_sessions (
    id TEXT PRIMARY KEY, repository TEXT, branch TEXT, summary TEXT,
    created_at TEXT, updated_at TEXT, turns_count INTEGER, files_count INTEGER)""",
    """CREATE TABLE IF NOT EXISTS zed_turns (
    session_id TEXT, turn_index INTEGER, user_msg TEXT, assistant_msg TEXT, timestamp TEXT,
    PRIMARY KEY(session_id, turn_index))""",
    """CREATE TABLE IF NOT EXISTS zed_files (
    session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER,
    PRIMARY KEY(session_id, file_path, turn_index))""",
    "CREATE TABLE IF NOT EXISTS zed_meta (key TEXT PRIMARY KEY, value TEXT)",
]

_FTS = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS zed_search USING fts5("
    "session_id UNINDEXED, user_msg, assistant_msg, summary,"
    ' tokenize="porter unicode61 remove_diacritics 2",'
    " prefix='2 3 4'{extra})"
)


def _index_path() -> pathlib.Path:
    custom = os.environ.get("SESSION_RECALL_ZED_INDEX_PATH")
    return (
        pathlib.Path(custom)
        if custom
        else pathlib.Path.home() / ".zed" / ".sr-zed-index.db"
    )


def _open(path: pathlib.Path | None = None) -> sqlite3.Connection:
    if path is None:
        path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in _DDL:
        conn.execute(stmt)
    try:
        conn.execute(_FTS.format(extra=", contentless_delete=1"))
    except sqlite3.OperationalError:
        conn.execute(_FTS.format(extra=""))
    conn.commit()
    return conn


def build_index(db_path: str, *, rebuild: bool = False) -> dict:
    src = sqlite3.connect(db_path)
    src.row_factory = sqlite3.Row
    conn = _open()
    indexed = 0
    try:
        conn.execute("BEGIN")
        if rebuild:
            conn.execute("DELETE FROM zed_sessions")
            conn.execute("DELETE FROM zed_turns")
            conn.execute("DELETE FROM zed_files")
            conn.execute("DELETE FROM zed_search")
        rows = src.execute(
            "SELECT id, summary, updated_at, created_at, data_type, data, folder_paths FROM threads"
        ).fetchall()
        for r in rows:
            parsed = parse_thread_row(dict(r))
            if not parsed:
                continue
            _upsert(conn, parsed)
            indexed += 1
        conn.execute(
            "INSERT OR REPLACE INTO zed_meta(key,value) VALUES('last_run_epoch',?)",
            (str(time.time()),),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        src.close()
        conn.close()
    return {"indexed": indexed}


def _upsert(conn: sqlite3.Connection, s: dict) -> None:
    sid = s["id"]
    turns = s.get("turns", [])
    files = s.get("files", [])
    conn.execute(
        "INSERT OR REPLACE INTO zed_sessions VALUES(?,?,?,?,?,?,?,?)",
        (
            sid,
            s.get("repository", ""),
            s.get("branch", ""),
            s.get("summary", ""),
            s.get("created_at", ""),
            s.get("updated_at", ""),
            len(turns),
            len(files),
        ),
    )
    conn.execute("DELETE FROM zed_turns WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM zed_files WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM zed_search WHERE session_id=?", (sid,))
    for t in turns:
        conn.execute(
            "INSERT OR REPLACE INTO zed_turns VALUES(?,?,?,?,?)",
            (
                sid,
                t.get("turn_index", 0),
                t.get("user_message", ""),
                t.get("assistant_response", ""),
                t.get("timestamp", ""),
            ),
        )
        conn.execute(
            "INSERT INTO zed_search(session_id,user_msg,assistant_msg,summary) VALUES(?,?,?,?)",
            (
                sid,
                t.get("user_message", ""),
                t.get("assistant_response", ""),
                s.get("summary", ""),
            ),
        )
    for f in files:
        conn.execute(
            "INSERT OR IGNORE INTO zed_files VALUES(?,?,?,?)",
            (
                sid,
                f.get("file_path", ""),
                f.get("tool_name", ""),
                f.get("turn_index", 0),
            ),
        )


def query_sessions(*, repo: Optional[str], limit: int, days: int) -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    conn = _open(path)
    try:
        df = f"-{days} days"
        if repo and repo != "all":
            rows = conn.execute(
                "SELECT * FROM zed_sessions WHERE repository=? AND turns_count >= 2 AND updated_at >= datetime('now',?) ORDER BY updated_at DESC LIMIT ?",
                (repo, df, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM zed_sessions WHERE turns_count >= 2 AND updated_at >= datetime('now',?) ORDER BY updated_at DESC LIMIT ?",
                (df, limit),
            ).fetchall()
        return [{**dict(r), "_trust_level": _TRUST_LEVEL} for r in rows]
    finally:
        conn.close()


def query_files(*, repo: Optional[str], limit: int, days: int) -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    conn = _open(path)
    try:
        df = f"-{days} days"
        base = "SELECT f.file_path,f.tool_name,f.turn_index,s.updated_at,s.id AS session_id,s.summary,s.repository FROM zed_files f JOIN zed_sessions s ON s.id=f.session_id"
        if repo and repo != "all":
            rows = conn.execute(
                base
                + " WHERE s.repository=? AND s.updated_at >= datetime('now',?) ORDER BY s.updated_at DESC LIMIT ?",
                (repo, df, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                base
                + " WHERE s.updated_at >= datetime('now',?) ORDER BY s.updated_at DESC LIMIT ?",
                (df, limit),
            ).fetchall()
        return [{**dict(r), "_trust_level": _TRUST_LEVEL} for r in rows]
    finally:
        conn.close()


def query_search(
    query: str, *, repo: Optional[str], limit: int, days: int
) -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    safe = _sanitize_fts5_query(query)
    if safe is None:
        return []
    conn = _open(path)
    try:
        df = f"-{days} days"
        base = (
            "SELECT z.session_id,z.user_msg,z.assistant_msg,z.summary,s.repository,s.updated_at "
            "FROM zed_search z JOIN zed_sessions s ON s.id=z.session_id"
        )
        if repo and repo != "all":
            rows = conn.execute(
                base
                + " WHERE zed_search MATCH ? AND s.repository=? AND s.updated_at >= datetime('now',?) ORDER BY bm25(zed_search) LIMIT ?",
                (safe, repo, df, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                base
                + " WHERE zed_search MATCH ? AND s.updated_at >= datetime('now',?) ORDER BY bm25(zed_search) LIMIT ?",
                (safe, df, limit),
            ).fetchall()
        return [{**dict(r), "_trust_level": _TRUST_LEVEL} for r in rows]
    finally:
        conn.close()


def query_show(session_id: str, *, turns: Optional[int]) -> Optional[dict]:
    path = _index_path()
    if not path.exists():
        return None
    conn = _open(path)
    try:
        row = conn.execute(
            "SELECT * FROM zed_sessions WHERE id LIKE ? LIMIT 1", (session_id + "%",)
        ).fetchone()
        if not row:
            return None
        sid = row["id"]
        if turns is None:
            turn_rows = conn.execute(
                "SELECT * FROM zed_turns WHERE session_id=? ORDER BY turn_index", (sid,)
            ).fetchall()
        else:
            turn_rows = conn.execute(
                "SELECT * FROM zed_turns WHERE session_id=? ORDER BY turn_index LIMIT ?",
                (sid, turns),
            ).fetchall()
        file_rows = conn.execute(
            "SELECT file_path, tool_name, turn_index FROM zed_files WHERE session_id=?",
            (sid,),
        ).fetchall()
        return {
            **dict(row),
            "turns": [dict(t) for t in turn_rows],
            "files": [dict(f) for f in file_rows],
            "_trust_level": _TRUST_LEVEL,
        }
    finally:
        conn.close()
