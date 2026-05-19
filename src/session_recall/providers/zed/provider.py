"""Zed Editor SQLite provider.

This provider reads Zed's local `threads.db` and maps it to the common
session-recall provider contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...db.connect import connect_ro
from ...util.detect_repo import detect_repo_for_cwd
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
        return self._has_db() and "threads" in tables

    def schema_problems(self) -> list[str]:
        if not self._has_db():
            return []
        tables = self._tables()
        if "threads" not in tables:
            return ["zed threads.db missing expected table: threads"]
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
    def _extract_text_from_content_blocks(content: Any) -> str:
        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                s = node.strip()
                if s:
                    parts.append(s)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return

            # Common Zed blocks: {"Text": "..."}
            txt = node.get("Text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt.strip())

            for key in (
                "content",
                "text",
                "body",
                "message",
                "input",
                "raw_input",
                "tool_results",
            ):
                if key in node:
                    walk(node[key])

            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(content)
        return "\n".join(parts).strip()

    @staticmethod
    def _extract_paths_from_obj(obj: Any) -> list[str]:
        paths: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return

            for k in (
                "path",
                "file_path",
                "source_path",
                "destination_path",
                "root",
                "cwd",
            ):
                v = node.get(k)
                if isinstance(v, str) and v.strip() and "/" in v:
                    paths.append(v.strip())

            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)

        walk(obj)
        return paths

    @staticmethod
    def _decode_payload(data_type: str, data_blob: Any) -> dict[str, Any] | None:
        if not isinstance(data_blob, (bytes, bytearray)):
            return None

        if data_type == "zstd":
            try:
                import zstandard as zstd  # type: ignore

                raw = zstd.ZstdDecompressor().decompress(
                    data_blob, max_output_size=50_000_000
                )
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
            return None

        # Optional plain JSON fallback
        try:
            obj = json.loads(data_blob)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_messages_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("messages")
        if not isinstance(raw, list):
            return []

        out: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue

            if "User" in item and isinstance(item["User"], dict):
                u = item["User"]
                out.append(
                    {
                        "idx": idx,
                        "role": "user",
                        "text": ZedProvider._extract_text_from_content_blocks(
                            u.get("content")
                        ),
                        "timestamp": str(u.get("timestamp") or ""),
                        "raw": item,
                    }
                )

            elif "Agent" in item and isinstance(item["Agent"], dict):
                a = item["Agent"]
                out.append(
                    {
                        "idx": idx,
                        "role": "assistant",
                        "text": ZedProvider._extract_text_from_content_blocks(
                            a.get("content")
                        ),
                        "timestamp": str(a.get("timestamp") or ""),
                        "raw": item,
                    }
                )

        return out

    @staticmethod
    def _repo_from_row(row: dict[str, Any]) -> str:
        for key in ("repository", "repo"):
            v = row.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

        # Zed-native hints
        folder_paths = row.get("folder_paths")
        if isinstance(folder_paths, str) and folder_paths.strip():
            first = folder_paths.split(",")[0].strip()
            detected = detect_repo_for_cwd(first)
            if detected:
                return detected
            return f"local:{Path(first).expanduser()}"

        for key in ("project_path", "workspace", "cwd", "path"):
            v = row.get(key)
            if not isinstance(v, str) or not v.strip():
                continue
            path_val = v.strip()
            detected = detect_repo_for_cwd(path_val)
            if detected:
                return detected
            return f"local:{Path(path_val).expanduser()}"

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
        data_type_col = self._pick(cols, "data_type")
        data_col = self._pick(cols, "data")

        selected = [id_col]
        for c in (title_col, created_col, updated_col, data_type_col, data_col):
            if c and c not in selected:
                selected.append(c)

        # Optional context columns used for repository inference.
        for c in (
            "repository",
            "repo",
            "project_path",
            "workspace",
            "cwd",
            "path",
            "folder_paths",
        ):
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
            summary = self._val(r, title_col, "").strip()
            payload = self._decode_payload(
                self._val(r, data_type_col), r[data_col] if data_col else None
            )
            messages = self._parse_messages_from_payload(payload or {})
            first_user = next(
                (
                    m["text"]
                    for m in messages
                    if m.get("role") == "user" and str(m.get("text") or "").strip()
                ),
                "",
            )

            if not summary:
                summary = (
                    (first_user[:120] + "...") if len(first_user) > 120 else first_user
                )
                if not summary:
                    summary = "(untitled)"

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
                    "turns_count": len(messages),
                    "files_count": 0,
                    "_trust_level": "trusted_first_party",
                    "_zed_messages": messages,
                }
            )
        return out

    def list_sessions(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        if not self._has_db():
            return []
        conn = connect_ro(self.db_path)
        try:
            sessions = self._list_threads(conn, limit=max(limit * 3, limit), days=days)
            if repo and repo != "all":
                sessions = [s for s in sessions if s.get("repository") == repo]
            sessions.sort(
                key=lambda s: s.get("updated_at") or s.get("created_at") or "",
                reverse=True,
            )
            for s in sessions:
                s.pop("_zed_messages", None)
            return sessions[:limit]
        finally:
            conn.close()

    def recent_files(
        self, repo: str | None, limit: int, days: int | None
    ) -> list[dict[str, Any]]:
        if not self._has_db():
            return []
        conn = connect_ro(self.db_path)
        try:
            sessions = self._list_threads(conn, limit=300, days=days)
            if repo and repo != "all":
                sessions = [s for s in sessions if s.get("repository") == repo]

            out: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for s in sessions:
                for m in s.get("_zed_messages", []):
                    raw = m.get("raw")
                    if not isinstance(raw, dict):
                        continue
                    for p in self._extract_paths_from_obj(raw):
                        key = (s["id_full"], p)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(
                            {
                                "provider": self.provider_id,
                                "file_path": p,
                                "tool_name": "zed_tool",
                                "date": (s.get("updated_at") or "")[:10],
                                "session_id": s.get("id_short"),
                                "session_summary": s.get("summary"),
                                "_trust_level": "trusted_first_party",
                            }
                        )
                        if len(out) >= limit:
                            for sess in sessions:
                                sess.pop("_zed_messages", None)
                            return out

            for sess in sessions:
                sess.pop("_zed_messages", None)
            return out
        finally:
            conn.close()

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
            sessions = self._list_threads(conn, limit=300, days=days)
            if repo and repo != "all":
                sessions = [s for s in sessions if s.get("repository") == repo]
            hits: list[dict[str, Any]] = []
            for s in sessions:
                for m in s.get("_zed_messages", []):
                    text = str(m.get("text") or "")
                    if q not in text.lower():
                        continue
                    excerpt = text[:250] + ("..." if len(text) > 250 else "")
                    hits.append(
                        {
                            "provider": self.provider_id,
                            "session_id": s["id_short"],
                            "session_id_full": s["id_full"],
                            "source_type": f"message:{m.get('role') or 'unknown'}",
                            "summary": s.get("summary", ""),
                            "repository": s.get("repository", "local:zed"),
                            "date": s.get("date"),
                            "excerpt": excerpt,
                            "_trust_level": "trusted_first_party",
                        }
                    )
                    if len(hits) >= limit:
                        for sess in sessions:
                            sess.pop("_zed_messages", None)
                        return hits
            for sess in sessions:
                sess.pop("_zed_messages", None)
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

            rows = list(target.get("_zed_messages", []))
            if turns is not None:
                rows = rows[:turns]
            mx = 99999 if full else 500
            payload = []
            for i, r in enumerate(rows):
                role = str(r.get("role") or "assistant")
                text = str(r.get("text") or "")[:mx]
                ts = str(r.get("timestamp") or "")
                if role.startswith("user"):
                    payload.append(
                        {"idx": i, "user": text, "assistant": "", "timestamp": ts}
                    )
                else:
                    payload.append(
                        {"idx": i, "user": "", "assistant": text, "timestamp": ts}
                    )

            # Best-effort extracted files from tool payloads
            files_payload: list[dict[str, Any]] = []
            seen: set[str] = set()
            for r in rows:
                raw = r.get("raw")
                if not isinstance(raw, dict):
                    continue
                for p in self._extract_paths_from_obj(raw):
                    if p in seen:
                        continue
                    seen.add(p)
                    files_payload.append(
                        {
                            "file_path": p,
                            "tool_name": "zed_tool",
                            "turn_index": int(r.get("idx") or 0),
                        }
                    )

            result = {
                "provider": self.provider_id,
                "id": target["id_full"],
                "repository": target.get("repository", "local:zed"),
                "branch": "",
                "summary": target.get("summary", "(untitled)"),
                "created_at": target.get("created_at"),
                "turns_count": len(payload),
                "turns": payload,
                "files": files_payload,
                "refs": [],
                "checkpoints": [],
                "_trust_level": "trusted_first_party",
            }
            target.pop("_zed_messages", None)
            return result
        finally:
            conn.close()
