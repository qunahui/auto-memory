"""Parse Zed `threads` rows into normalized session payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...util.detect_repo import detect_repo_for_cwd


def _extract_text_blocks(content: Any) -> str:
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


def _extract_paths(node: Any) -> list[str]:
    out: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for i in x:
                walk(i)
            return
        if not isinstance(x, dict):
            return
        for k in (
            "path",
            "file_path",
            "source_path",
            "destination_path",
            "root",
            "cwd",
        ):
            v = x.get(k)
            if isinstance(v, str) and v.strip() and "/" in v:
                out.append(v.strip())
        for v in x.values():
            if isinstance(v, (dict, list)):
                walk(v)

    walk(node)
    return out


def decode_payload(data_type: str, data_blob: Any) -> dict[str, Any] | None:
    if not isinstance(data_blob, (bytes, bytearray)):
        return None
    if data_type == "zstd":
        import zstandard as zstd  # type: ignore

        raw = zstd.ZstdDecompressor().decompress(data_blob, max_output_size=50_000_000)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    obj = json.loads(data_blob)
    return obj if isinstance(obj, dict) else None


def parse_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
                    "text": _extract_text_blocks(u.get("content")),
                    "timestamp": str(u.get("timestamp") or ""),
                    "paths": _extract_paths(item),
                }
            )
        elif "Agent" in item and isinstance(item["Agent"], dict):
            a = item["Agent"]
            out.append(
                {
                    "idx": idx,
                    "role": "assistant",
                    "text": _extract_text_blocks(a.get("content")),
                    "timestamp": str(a.get("timestamp") or ""),
                    "paths": _extract_paths(item),
                }
            )
    return out


def resolve_repository(row: dict[str, Any]) -> str:
    folder_paths = row.get("folder_paths")
    if isinstance(folder_paths, str) and folder_paths.strip():
        first = folder_paths.split(",")[0].strip()
        detected = detect_repo_for_cwd(first)
        if detected:
            return detected
        return f"local:{Path(first).expanduser()}"
    return "local:zed"


def parse_thread_row(row: dict[str, Any]) -> dict[str, Any] | None:
    sid = str(row.get("id") or "").strip()
    if not sid:
        return None
    try:
        payload = decode_payload(str(row.get("data_type") or ""), row.get("data")) or {}
    except Exception:
        payload = {}
    messages = parse_messages(payload)
    first_user = next(
        (m["text"] for m in messages if m["role"] == "user" and m["text"].strip()), ""
    )
    summary = str(row.get("summary") or "").strip() or (
        first_user[:120] if first_user else "(untitled)"
    )
    created_at = str(row.get("created_at") or row.get("updated_at") or "")
    updated_at = str(row.get("updated_at") or created_at)
    repository = resolve_repository(row)

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in messages:
        for p in m.get("paths", []):
            if p in seen:
                continue
            seen.add(p)
            files.append(
                {
                    "file_path": p,
                    "tool_name": "zed_tool",
                    "turn_index": int(m.get("idx") or 0),
                }
            )

    turns = [
        {
            "turn_index": int(m.get("idx") or 0),
            "user_message": m["text"] if m["role"] == "user" else "",
            "assistant_response": m["text"] if m["role"] != "user" else "",
            "timestamp": m.get("timestamp") or "",
        }
        for m in messages
    ]

    return {
        "id": sid,
        "repository": repository,
        "branch": "",
        "summary": summary,
        "created_at": created_at,
        "updated_at": updated_at,
        "turns": turns,
        "files": files,
    }
