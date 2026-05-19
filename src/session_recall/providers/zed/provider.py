"""Zed Editor SQLite provider.

This provider reads Zed's local `threads.db` and maps it to the common
session-recall provider contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...db.connect import connect_ro
from ..base import StorageProvider


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
        self.db_path: str = (
            str(Path(db_path).expanduser()) if db_path else str(default_path)
        )

    def _has_db(self) -> bool:
        return Path(self.db_path).is_file()

    def _tables(self) -> set[str]:
        if not self._has_db():
            return set()
        conn = connect_ro(self.db_path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            return {str(r["name"]).lower() for r in rows}
        finally:
            conn.close()

    def is_available(self) -> bool:
        tables = self._tables()
        return self._has_db() and ("threads" in tables or "messages" in tables)

    def schema_problems(self) -> list[str]:
        if not self._has_db():
            return []
        tables = self._tables()
        if "threads" not in tables and "messages" not in tables:
            return ["zed threads.db missing expected tables (threads/messages)"]
        return []

    @staticmethod
    def _colset(conn, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]).lower() for r in rows}

    @staticmethod
    def _pick(cols: set[str], *names: str) -> str | None:
        for n in names:
            if n in cols:
                return n
        return None

    @staticmethod
    def _val(row, col: str | None, default: str = "") -> str:
        if not col:
            return default
        v = row[col]
        if v is None:
            return default
        return str(v)

    @staticmethod
    def _repo_from_row(row: dict[str, Any]) -> str:
        for key in ("repository", "repo", "project_path", "workspace", "cwd", "path"):
            v = row.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return "local:zed"

    def _list_threads(
        self, conn: Any, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        cols = self._colset(conn, "threads")
        id_col = self._pick(cols, "id", "thread_id", "uuid")
        if not id_col:
            return []
        title_col = self._pick(cols, "title", "summary", "name")
        created_col = self._pick(cols, "created_at", "createdat", "created")
        updated_col = self._pick(
            cols, "updated_at", "updatedat", "updated", "last_updated_at"
        )

        selected = [id_col]
        if title_col:
            selected.append(title_col)
        if created_col:
            selected.append(created_col)
        if updated_col and updated_col not in selected:
            selected.append(updated_col)

        # Optional context columns used for repository inference.
        for c in ("repository", "repo", "project_path", "workspace", "cwd", "path"):
            if c in cols and c not in selected:
                selected.append(c)

        where = ""
        params: list[Any] = []
        ts_col = updated_col or created_col
        if days is not None and ts_col:
            where = f" WHERE {ts_col} >= datetime('now', ?)"
            params.append(f"-{days} days")

        order_col = updated_col or created_col or id_col
        sql = f"SELECT {', '.join(selected)} FROM threads{where} ORDER BY {order_col} DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()

        out = []
        for r in rows:
            as_dict = {k.lower(): r[k] for k in r.keys()}
            sid = str(r[id_col])
            created_at = self._val(r, created_col)
            updated_at = self._val(r, updated_col, created_at)
            summary = self._val(r, title_col, "(untitled)")
            out.append(
                {
                    "provider": self.provider_id,
                    "id_short": sid[:8],
                    "id_full": sid,
                    "repository": self._repo_from_row(as_dict),
                    "branch": "",
                    "summary": summary,
                    "date": (created_at or updated_at)[:10]
                    if (created_at or updated_at)
                    else None,
                    "created_at": created_at or updated_at,
                    "updated_at": updated_at or created_at,
                    "turns_count": 0,
                    "files_count": 0,
                    "_trust_level": "trusted_first_party",
                }
            )
        return out

    def _message_rows_for_thread(self, conn: Any, thread_id: str) -> list[Any]:
        try:
            cols = self._colset(conn, "messages")
        except Exception:
            return []
        tid_col = self._pick(
            cols, "thread_id", "threadid", "conversation_id", "session_id"
        )
        if not tid_col:
            return []
        order_col = (
            self._pick(cols, "created_at", "createdat", "timestamp", "id") or "id"
        )
        return conn.execute(
            f"SELECT * FROM messages WHERE {tid_col} = ? ORDER BY {order_col} ASC",
            (thread_id,),
        ).fetchall()

    @staticmethod
    def _extract_text(msg_row: Any) -> tuple[str, str]:
        d = {k.lower(): msg_row[k] for k in msg_row.keys()}

        role = "assistant"
        for rk in ("role", "sender_role", "author_role", "kind", "type"):
            rv = d.get(rk)
            if isinstance(rv, str) and rv.strip():
                role = rv.strip().lower()
                break

        for ck in ("content", "text", "body", "message"):
            cv = d.get(ck)
            if isinstance(cv, str) and cv:
                s = cv.strip()
                if s.startswith("{") or s.startswith("["):
                    try:
                        obj = json.loads(s)
                        if isinstance(obj, dict):
                            for k in ("text", "content", "message"):
                                v = obj.get(k)
                                if isinstance(v, str):
                                    return role, v
                        elif isinstance(obj, list):
                            parts = [p for p in obj if isinstance(p, str)]
                            if parts:
                                return role, "\n".join(parts)
                    except Exception:
                        pass
                return role, s
        return role, ""

    def list_sessions(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        if not self._has_db():
            return []
        conn = connect_ro(self.db_path)
        try:
            sessions = self._list_threads(conn, limit=max(limit * 3, limit), days=days)
            if not sessions:
                return []
            for s in sessions:
                msgs = self._message_rows_for_thread(conn, s["id_full"])
                s["turns_count"] = len(msgs)
            if repo and repo != "all":
                sessions = [s for s in sessions if s.get("repository") == repo]
            sessions.sort(
                key=lambda s: s.get("updated_at") or s.get("created_at") or "",
                reverse=True,
            )
            return sessions[:limit]
        finally:
            conn.close()

    def recent_files(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        return []

    def list_checkpoints(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        return []

    def search(
        self, query: str, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q or not self._has_db():
            return []

        conn = connect_ro(self.db_path)
        try:
            sessions = self._list_threads(conn, limit=200, days=days)
            if repo and repo != "all":
                sessions = [s for s in sessions if s.get("repository") == repo]
            hits: list[dict[str, Any]] = []
            for s in sessions:
                for r in self._message_rows_for_thread(conn, s["id_full"]):
                    role, text = self._extract_text(r)
                    if q not in text.lower():
                        continue
                    excerpt = text[:250] + ("..." if len(text) > 250 else "")
                    hits.append(
                        {
                            "provider": self.provider_id,
                            "session_id": s["id_short"],
                            "session_id_full": s["id_full"],
                            "source_type": f"message:{role}",
                            "summary": s.get("summary", ""),
                            "repository": s.get("repository", "local:zed"),
                            "date": s.get("date"),
                            "excerpt": excerpt,
                            "_trust_level": "trusted_first_party",
                        }
                    )
                    if len(hits) >= limit:
                        return hits
            return hits
        finally:
            conn.close()

    def get_session(
        self, session_id: str, turns: int | None, full: bool
    ) -> dict[str, Any] | None:
        sid = (session_id or "").strip().lower()
        if not sid or not self._has_db():
            return None

        conn = connect_ro(self.db_path)
        try:
            sessions = self._list_threads(conn, limit=10000, days=None)
            target = None
            for s in sessions:
                full_id = str(s["id_full"]).lower()
                short_id = str(s["id_short"]).lower()
                if sid == full_id or full_id.startswith(sid) or sid == short_id:
                    target = s
                    break
            if not target:
                return None

            rows = self._message_rows_for_thread(conn, target["id_full"])
            if turns is not None:
                rows = rows[:turns]
            mx = 99999 if full else 500
            payload = []
            for i, r in enumerate(rows):
                role, text = self._extract_text(r)
                ts = ""
                row_lc = {k.lower(): r[k] for k in r.keys()}
                for tk in ("created_at", "createdat", "timestamp", "time"):
                    if tk in row_lc and row_lc[tk] is not None:
                        ts = str(row_lc[tk])
                        break
                if role.startswith("user"):
                    payload.append(
                        {"idx": i, "user": text[:mx], "assistant": "", "timestamp": ts}
                    )
                else:
                    payload.append(
                        {"idx": i, "user": "", "assistant": text[:mx], "timestamp": ts}
                    )

            return {
                "provider": self.provider_id,
                "id": target["id_full"],
                "repository": target.get("repository", "local:zed"),
                "branch": "",
                "summary": target.get("summary", "(untitled)"),
                "created_at": target.get("created_at"),
                "turns_count": len(payload),
                "turns": payload,
                "files": [],
                "refs": [],
                "checkpoints": [],
                "_trust_level": "trusted_first_party",
            }
        finally:
            conn.close()
