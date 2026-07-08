"""Recover Loom messages from a Claude/Umans JSONL transcript.

Dry-run by default. Use --apply only after inspecting the preview.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path


def _timestamp(ts: str | None) -> float:
    if not ts:
        return dt.datetime.now().timestamp()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts).timestamp()


def _content_text(content) -> tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts: list[str] = []
    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append(block.get("text", ""))
        elif block_type == "tool_result":
            saw_tool_result = True
    return "\n".join(p for p in parts if p).strip(), saw_tool_result


def parse_turns(path: Path) -> list[dict]:
    turns: list[dict] = []
    session_id = path.stem
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") not in {"user", "assistant"}:
            continue
        message = obj.get("message") or {}
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text, saw_tool_result = _content_text(message.get("content"))
        if not text or saw_tool_result:
            continue
        turns.append(
            {
                "role": role,
                "content": text,
                "created_at": _timestamp(obj.get("timestamp")),
                "cc_session_id": obj.get("sessionId") or session_id,
            }
        )
    return turns


def apply_turns(db_path: Path, conv_id: int, turns: list[dict]) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("PRAGMA foreign_keys=ON")
        existing = con.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)
        ).fetchone()[0]
        if existing:
            raise SystemExit(
                f"conversation {conv_id} already has {existing} messages; refusing to append"
            )
        parent_id = None
        for turn in turns:
            cur = con.execute(
                """INSERT INTO messages
                   (conversation_id, parent_id, role, content, token_estimate,
                    is_active, cc_session_id, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    conv_id,
                    parent_id,
                    turn["role"],
                    turn["content"],
                    len(turn["content"]) // 3,
                    turn["cc_session_id"],
                    turn["created_at"],
                ),
            )
            parent_id = cur.lastrowid
        if turns:
            con.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (max(t["created_at"] for t in turns), conv_id),
            )
        con.commit()
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--conv-id", required=True, type=int)
    parser.add_argument("--session-jsonl", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    turns = parse_turns(args.session_jsonl)
    print(f"recoverable text turns: {len(turns)}")
    for turn in turns[:12]:
        when = dt.datetime.fromtimestamp(turn["created_at"]).isoformat()
        preview = turn["content"].replace("\n", " ")[:120]
        print(f"{when} {turn['role']}: {preview!r}")
    if len(turns) > 12:
        print(f"... {len(turns) - 12} more")

    if args.apply:
        apply_turns(args.db, args.conv_id, turns)
        print(f"inserted {len(turns)} messages into conversation {args.conv_id}")
    else:
        print("dry run only; rerun with --apply to insert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
