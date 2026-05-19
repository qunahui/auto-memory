"""Zed storage provider backed by a local SQLite FTS index."""

from __future__ import annotations

import time
from pathlib import Path

from ..base import StorageProvider
from .index import build_index, query_files, query_search, query_sessions, query_show

_STALE_SECONDS = 60
_TRUST_LEVEL = "zed_threads_db"


class ZedProvider(StorageProvider):
    provider_id = "zed"
    provider_name = "Zed Editor"

    def __init__(self, db_path: str | None = None) -> None:
        default_path = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Zed"
            / "threads"
            / "threads.db"
        )
        self.db_path = str(Path(db_path).expanduser()) if db_path else str(default_path)

    def _has_db(self) -> bool:
        return Path(self.db_path).is_file()

    def is_available(self) -> bool:
        return self._has_db()

    def schema_problems(self) -> list[str]:
        if not self._has_db():
            return []
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            tables = {str(r[0]).lower() for r in rows}
            if "threads" not in tables:
                return ["zed threads.db missing expected table: threads"]
            return []
        finally:
            conn.close()

    def _ensure_index(self) -> None:
        from .index import _index_path

        idx = _index_path()
        if not idx.exists():
            build_index(self.db_path, rebuild=True)
            return
        try:
            age = time.time() - idx.stat().st_mtime
        except OSError:
            age = _STALE_SECONDS + 1
        if age > _STALE_SECONDS:
            build_index(self.db_path, rebuild=False)

    def list_sessions(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict]:
        if not self._has_db():
            return []
        self._ensure_index()
        rows = query_sessions(repo=repo, limit=limit, days=days or 30)
        return [
            {
                "provider": self.provider_id,
                "id_short": str(r["id"])[:8],
                "id_full": r["id"],
                "repository": r.get("repository") or "local:zed",
                "branch": r.get("branch") or "",
                "summary": r.get("summary") or "",
                "date": str(r.get("created_at") or r.get("updated_at") or "")[:10],
                "created_at": r.get("created_at") or r.get("updated_at") or "",
                "updated_at": r.get("updated_at") or r.get("created_at") or "",
                "turns_count": int(r.get("turns_count") or 0),
                "files_count": int(r.get("files_count") or 0),
                "_trust_level": _TRUST_LEVEL,
            }
            for r in rows
        ]

    def recent_files(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict]:
        if not self._has_db():
            return []
        self._ensure_index()
        rows = query_files(repo=repo, limit=limit, days=days or 30)
        return [
            {
                "provider": self.provider_id,
                "file_path": r.get("file_path") or "",
                "tool_name": r.get("tool_name") or "",
                "date": str(r.get("updated_at") or "")[:10],
                "session_id": str(r.get("session_id") or "")[:8],
                "session_summary": r.get("summary") or "",
                "_trust_level": _TRUST_LEVEL,
            }
            for r in rows
        ]

    def list_checkpoints(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict]:
        return []

    def search(
        self, query: str, repo: str | None, limit: int, days: int | None
    ) -> list[dict]:
        if not self._has_db():
            return []
        self._ensure_index()
        rows = query_search(query, repo=repo, limit=limit, days=days or 30)
        out = []
        for r in rows:
            content = (
                f"{r.get('user_msg') or ''}\n{r.get('assistant_msg') or ''}".strip()
            )
            out.append(
                {
                    "provider": self.provider_id,
                    "session_id": str(r.get("session_id") or "")[:8],
                    "session_id_full": r.get("session_id") or "",
                    "source_type": "turn",
                    "summary": r.get("summary") or "",
                    "repository": r.get("repository") or "local:zed",
                    "date": str(r.get("updated_at") or "")[:10],
                    "excerpt": content[:250] + ("..." if len(content) > 250 else ""),
                    "_trust_level": _TRUST_LEVEL,
                }
            )
        return out

    def get_session(
        self, session_id: str, turns: int | None, full: bool
    ) -> dict | None:
        if not self._has_db():
            return None
        self._ensure_index()
        row = query_show(session_id, turns=turns)
        if row is None:
            return None

        mx = 99999 if full else 500
        turn_payload = [
            {
                "idx": t.get("turn_index", 0),
                "user": str(t.get("user_msg") or "")[:mx],
                "assistant": str(t.get("assistant_msg") or "")[:mx],
                "timestamp": t.get("timestamp") or "",
            }
            for t in row.get("turns", [])
        ]

        return {
            "provider": self.provider_id,
            "id": row.get("id") or "",
            "repository": row.get("repository") or "local:zed",
            "branch": row.get("branch") or "",
            "summary": row.get("summary") or "",
            "created_at": row.get("created_at") or row.get("updated_at") or "",
            "turns_count": len(turn_payload),
            "turns": turn_payload,
            "files": row.get("files", []),
            "refs": [],
            "checkpoints": [],
            "_trust_level": _TRUST_LEVEL,
        }
