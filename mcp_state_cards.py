"""MCP server exposing state-card CRUD for the Backstage agent.

Scoped to a single parent conversation via env vars set by the Loom server
when it launches the CC subprocess for a backstage conversation:

    LOOM_API_URL              e.g. http://127.0.0.1:3000
    LOOM_BACKSTAGE_PARENT_ID  integer conversation id whose cards to edit

All write paths go through conversation-scoped endpoints so a compromised or
confused agent cannot mutate cards in unrelated conversations even by
guessing card ids.
"""

import json
import os
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("loom-state-cards")


def _cfg() -> tuple[str, int] | str:
    api = os.environ.get("LOOM_API_URL", "").rstrip("/")
    parent = os.environ.get("LOOM_BACKSTAGE_PARENT_ID", "")
    if not api or not parent:
        return "Backstage not configured: LOOM_API_URL / LOOM_BACKSTAGE_PARENT_ID missing"
    # Defense in depth: only allow loopback even though env is server-controlled
    host = urlparse(api).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        return f"Blocked non-loopback LOOM_API_URL host: {host!r}"
    try:
        return api, int(parent)
    except ValueError:
        return f"Invalid LOOM_BACKSTAGE_PARENT_ID: {parent!r}"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


@mcp.tool()
def list_schemas() -> str:
    """List all available state card schemas (builtin and user-defined).

    Returns schema id, name, and field definitions. Use this before
    create_card to see what schema_ids are valid and what fields each expects.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, _ = cfg
    try:
        r = httpx.get(f"{api}/api/state-schemas", timeout=10)
        r.raise_for_status()
        return _json(r.json())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def list_cards(schema_id: str = "") -> str:
    """List state cards for the conversation being edited.

    Optionally filter by schema_id (e.g. "character_state", "scene_state",
    "lore"). Omit schema_id to return every card.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, parent = cfg
    try:
        params = {"schema_id": schema_id} if schema_id else None
        r = httpx.get(
            f"{api}/api/conversations/{parent}/state", params=params, timeout=10
        )
        r.raise_for_status()
        return _json(r.json())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_card(card_id: int) -> str:
    """Read the full data of a single state card by its numeric id.

    Use list_cards first to find ids. Returns the card as JSON including its
    schema_id, label, and data fields.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, parent = cfg
    try:
        r = httpx.get(f"{api}/api/conversations/{parent}/state", timeout=10)
        r.raise_for_status()
        for card in r.json():
            if card.get("id") == card_id:
                return _json(card)
        return f"Card {card_id} not found"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def create_card(schema_id: str, label: str, data: str = "{}") -> str:
    """Create a new state card.

    schema_id: one of the ids from list_schemas (e.g. "character_state").
    label: human-readable name, unique per (conversation, schema).
    data: JSON object string with the field values. Defaults to an empty object.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, parent = cfg
    try:
        data_obj = json.loads(data) if data else {}
    except json.JSONDecodeError as e:
        return f"Invalid JSON for data: {e}"
    try:
        r = httpx.post(
            f"{api}/api/conversations/{parent}/state",
            json={"schema_id": schema_id, "label": label, "data": data_obj},
            timeout=10,
        )
        r.raise_for_status()
        return _json(r.json())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def update_card(card_id: int, data: str) -> str:
    """Overwrite a card's data field with the provided JSON object.

    This replaces the entire data object — to patch a single field, first
    read_card, merge, then update_card with the merged result.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, parent = cfg
    try:
        data_obj = json.loads(data)
    except json.JSONDecodeError as e:
        return f"Invalid JSON for data: {e}"
    try:
        r = httpx.put(
            f"{api}/api/conversations/{parent}/state/{card_id}",
            json={"data": data_obj},
            timeout=10,
        )
        r.raise_for_status()
        return _json(r.json())
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def delete_card(card_id: int) -> str:
    """Permanently delete a state card by id. This cannot be undone."""
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    api, parent = cfg
    try:
        r = httpx.delete(
            f"{api}/api/conversations/{parent}/state/{card_id}", timeout=10
        )
        r.raise_for_status()
        return _json(r.json())
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
