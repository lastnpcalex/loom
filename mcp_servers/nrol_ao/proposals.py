"""Persistent article + proposal store for the NROL-AO MCP boundary.

Implements the spec's proposal lifecycle: articles are submitted as
candidate observations (no posterior movement), proposals record a typed
match (no posterior movement), and only commit_match routes a proposal
through the engine's gated transition machinery.

SQLite is used (stdlib, concurrent-safe) because the MCP server runs as a
short-lived stdio process per Claude Code session — state must survive
process turnover and tolerate two sessions writing at once.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY,
    submitted_at TEXT NOT NULL,
    headline TEXT,
    url TEXT,
    source TEXT,
    date TEXT,
    body TEXT,
    submitted_by TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    action TEXT NOT NULL,
    indicator_id TEXT,
    observed_value REAL,
    rationale TEXT,
    missing_direction TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_slug_status ON proposals(slug, status);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def article_id_for(article: dict) -> str:
    """Stable content-keyed id: same URL (or headline) resubmitted = same id."""
    key = (article.get("url") or "").strip().lower()
    if not key:
        key = (article.get("headline") or article.get("title") or "").strip().lower()
    if not key:
        key = uuid.uuid4().hex
    return "art-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def new_proposal_id() -> str:
    return f"prop-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:4]}"


class ProposalStore:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "proposals.db"

    def _conn(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # -- articles --

    def submit_article(self, article: dict, submitted_by: str = "") -> dict:
        art_id = article_id_for(article)
        with contextlib.closing(self._conn()) as conn, conn:
            existing = conn.execute(
                "SELECT id FROM articles WHERE id = ?", (art_id,)
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO articles "
                "(id, submitted_at, headline, url, source, date, body, submitted_by, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    art_id,
                    _utc_now(),
                    article.get("headline") or article.get("title") or "",
                    article.get("url") or "",
                    article.get("source") or "",
                    article.get("date") or "",
                    article.get("body") or article.get("relevance") or article.get("text") or "",
                    submitted_by,
                    json.dumps(article, ensure_ascii=True, default=str),
                ),
            )
        record = self.get_article(art_id)
        record["deduped"] = bool(existing)
        return record

    def get_article(self, article_id: str) -> dict | None:
        with contextlib.closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM articles WHERE id = ?", (article_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_articles(self, limit: int = 50) -> list[dict]:
        with contextlib.closing(self._conn()) as conn, conn:
            rows = conn.execute(
                "SELECT id, submitted_at, headline, url, source, date, submitted_by "
                "FROM articles ORDER BY submitted_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- proposals --

    def add_proposal(
        self,
        article_id: str,
        slug: str,
        action: str,
        indicator_id: str = "",
        observed_value: float | None = None,
        rationale: str = "",
        missing_direction: str = "",
    ) -> dict:
        pid = new_proposal_id()
        with contextlib.closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO proposals "
                "(id, article_id, slug, action, indicator_id, observed_value, "
                " rationale, missing_direction, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    pid, article_id, slug, action, indicator_id, observed_value,
                    rationale, missing_direction, _utc_now(),
                ),
            )
        return self.get_proposal(pid)

    def get_proposal(self, proposal_id: str) -> dict | None:
        with contextlib.closing(self._conn()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if not row:
            return None
        record = dict(row)
        if record.get("result"):
            try:
                record["result"] = json.loads(record["result"])
            except Exception:
                pass
        return record

    def list_proposals(self, slug: str = "", status: str = "", limit: int = 50) -> list[dict]:
        query = "SELECT * FROM proposals"
        clauses, params = [], []
        if slug:
            clauses.append("slug = ?")
            params.append(slug)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with contextlib.closing(self._conn()) as conn, conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            record = dict(r)
            if record.get("result"):
                try:
                    record["result"] = json.loads(record["result"])
                except Exception:
                    pass
            out.append(record)
        return out

    def mark_proposal(
        self, proposal_id: str, status: str, note: str = "", result: dict | None = None
    ) -> dict | None:
        with contextlib.closing(self._conn()) as conn, conn:
            conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ?, decision_note = ?, "
                "result = ? WHERE id = ?",
                (
                    status,
                    _utc_now(),
                    note,
                    json.dumps(result, ensure_ascii=True, default=str) if result else None,
                    proposal_id,
                ),
            )
        return self.get_proposal(proposal_id)
