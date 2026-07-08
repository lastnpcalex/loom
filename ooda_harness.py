"""OODA Harness for Weave RP mode.

Single-pass generation with repair fallback:
  Pass:    Model emits a structured <ooda> block (observe, state reads,
           orient, state updates, decide) followed by 1-3 paragraphs of
           in-character prose, in one shot.
  Repair:  If parse_ooda_block returns None (truncated </ooda>, or loose
           update_state tags with no wrapper), repair_ooda_block attempts a
           regex-level recovery before state deltas are lost.
  Fallback:Only if repair also fails AND no prose was extracted does the
           server run a second sync_chat pass asking the model to re-emit a
           valid block. This keeps the common case single-pass (low latency)
           while recovering the state-tracking guarantees the two-pass design
           originally provided.

Inspired by metacog (tools as cognitive scaffolding) and popup-mcp
(amortize latency into fewer, richer passes).
"""

import json
import re
from typing import Optional

import database as db


# ── OODA System Prompt Builder ──

OODA_TOOL_DEFINITIONS = """
## OODA Workflow

Before writing your response, you MUST emit an <ooda> block. This is your cognitive scaffold — observe the scene, orient your character, decide what to do, then act through prose. Never skip it.

### State Operations

Use these tags inside your <ooda> block to track the world:

**Read** — pull current state before reacting:
  <read_state schema="character_state" label="CharacterName"/>
  <read_state schema="scene_state" label="current"/>
  <read_state schema="lore" label="LoreTitle"/>

**Update** — record what changed this beat:
  <update_state schema="character_state" label="CharacterName" field="current_mood" value="alarmed"/>
  <update_state schema="scene_state" label="current" field="atmosphere" value="tense"/>

**Create** — introduce a new tracked entity:
  <create_state schema="character_state" label="NewCharacter">{"personality": "gruff", "appearance": "scarred face"}</create_state>

### Required Structure

```
<ooda>
  <observe>What just happened — the player's action, dialogue, or choice. Be specific.</observe>
  <read_state schema="character_state" label="CharacterName"/>
  <read_state schema="scene_state" label="current"/>
  <orient>How your character feels about what happened, given their personality, mood, and relationship to the player. What would they naturally do next?</orient>
  <update_state schema="character_state" label="CharacterName" field="current_mood" value="..."/>
  <update_state schema="scene_state" label="current" field="recent_events" value="..."/>
  <decide>Plan exactly: one physical action, one line of dialogue or reaction, one sensory detail (sound, smell, texture, light). Keep it to 1-3 paragraphs.</decide>
</ooda>
```

### Update Checklist

Every turn, ALWAYS update:
- `current_mood` — mood shifts constantly; stale mood = flat character
- `recent_events` — one sentence capturing what just happened

Update when relevant:
- `physical_state` — injuries, exhaustion, position changes
- `atmosphere` — when the emotional tone of the scene shifts
- `location` — when anyone moves to a new place
- `characters_present` — when characters enter or leave the scene
- `relationship_to_player` — when trust, tension, or intimacy changes
- `current_goal` — when the character's immediate objective shifts
- `secrets` — when a secret is revealed or a new one forms

### Writing Rules

Your prose after the </ooda> block must follow these rules:
- 1-3 paragraphs. Tight scenes, not novels.
- Show, don't tell. "Her hand trembled" not "She was nervous."
- No markdown, no bullet lists, no headers. Pure prose and dialogue.
- End on something the player can react to — a question, an action, a look.
- Dialogue should sound like real speech: contractions, interruptions, trailing off.
- Ground every beat in a sensory detail: what the character sees, hears, smells, feels.
""".strip()


def _merge_state_tiers(conv_cards: list[dict], global_cards: list[dict]) -> list[dict]:
    """Merge Tier 1 (global) into Tier 2 (conversation) — empty fields inherit from global."""
    if not global_cards:
        return conv_cards

    # Index global cards by (schema_id, label)
    global_index = {}
    for gc in global_cards:
        gdata = json.loads(gc["data"]) if isinstance(gc["data"], str) else gc["data"]
        global_index[(gc["schema_id"], gc["label"])] = gdata

    merged = []
    for card in conv_cards:
        data = json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        gdata = global_index.get((card["schema_id"], card["label"]), {})
        # Inherit empty fields from global
        for k, v in gdata.items():
            if k not in data or not data[k]:
                data[k] = v
        merged.append({**card, "data": data})
    return merged


def build_ooda_system_prompt(base_system_prompt: str, state_cards: list[dict],
                             global_cards: list[dict] = None) -> str:
    """Build the full system prompt with OODA tools and current state summary.

    state_cards: Tier 2 (conversation-level)
    global_cards: Tier 1 (character-level) — empty fields in Tier 2 inherit from these
    """
    parts = [base_system_prompt, "", OODA_TOOL_DEFINITIONS]

    effective_cards = _merge_state_tiers(state_cards, global_cards or [])

    if effective_cards:
        parts.append("")
        parts.append("## Current State Cards")
        parts.append("")
        for card in effective_cards:
            data = card["data"] if isinstance(card["data"], dict) else json.loads(card["data"])
            schema = card["schema_id"]
            label = card["label"]
            fields = ", ".join(f"{k}={v}" for k, v in data.items() if v)
            parts.append(f"[{schema}: {label}] {fields}")

    return "\n".join(parts)


# ── XML Parser ──

def _strip_think_blocks(text: str) -> str:
    """Strip think reasoning blocks and trim. Shared by parse + repair."""
    return re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()


def parse_ooda_block(text: str) -> Optional[dict]:
    """Parse an <ooda>...</ooda> block from model output.

    Returns dict with observe, orient, decide, reads, updates, creates.
    Returns None if no <ooda> block found.
    """
    # Strip <think> blocks first
    text = _strip_think_blocks(text)

    match = re.search(r'<ooda>(.*?)</ooda>', text, re.DOTALL)
    if not match:
        return None

    block = match.group(1)

    result = {
        "observe": "",
        "orient": "",
        "decide": "",
        "reads": [],
        "updates": [],
        "creates": [],
    }

    # Extract text tags
    for tag in ("observe", "orient", "decide"):
        m = re.search(rf'<{tag}>(.*?)</{tag}>', block, re.DOTALL)
        if m:
            result[tag] = m.group(1).strip()

    # Extract read_state tags — XML <read_state .../> or bracket [read_state ...]
    for m in re.finditer(r'<read_state\s+schema="([^"]+)"\s+label="([^"]+)"\s*/>', block):
        result["reads"].append({"schema_id": m.group(1), "label": m.group(2)})
    for m in re.finditer(r'\[read_state\s+schema="([^"]+)"\s+label="([^"]+)"\]', block):
        result["reads"].append({"schema_id": m.group(1), "label": m.group(2)})

    # Extract update_state tags — XML or bracket, with or without closing tag
    for m in re.finditer(
        r'<update_state\s+schema="([^"]+)"\s+label="([^"]+)"\s+field="([^"]+)"\s+value="([^"]+)"\s*/?>(?:</update_state>)?',
        block
    ):
        result["updates"].append({
            "schema_id": m.group(1), "label": m.group(2),
            "field": m.group(3), "value": m.group(4),
        })
    for m in re.finditer(
        r'\[update_state\s+schema="([^"]+)"\s+label="([^"]+)"\s+field="([^"]+)"\s+value="([^"]+)"\]',
        block
    ):
        result["updates"].append({
            "schema_id": m.group(1), "label": m.group(2),
            "field": m.group(3), "value": m.group(4),
        })

    # Extract create_state tags
    for m in re.finditer(
        r'<create_state\s+schema="([^"]+)"\s+label="([^"]+)">(.*?)</create_state>',
        block, re.DOTALL
    ):
        try:
            data = json.loads(m.group(3).strip())
        except (json.JSONDecodeError, ValueError):
            data = {"content": m.group(3).strip()}
        result["creates"].append({
            "schema_id": m.group(1), "label": m.group(2), "data": data,
        })

    return result


def repair_ooda_block(text: str) -> Optional[dict]:
    """Attempt to recover an OODA block from truncated/malformed model output.

    Covers the two common failure modes that cause parse_ooda_block to return
    None (and thus silently drop state deltas):

    1. Unclosed <ooda> — the model ran out of tokens (or the canvas committed
       mid-block) before emitting </ooda>. We append a closing tag and re-parse.
    2. Loose <update_state .../> or <create_state> tags emitted with no
       <ooda> wrapper at all. We wrap the whole output synthetically and
       re-parse so the deltas survive.

    Returns the parsed dict, or None if nothing could be recovered.
    """
    cleaned = _strip_think_blocks(text)

    # Case 1: unclosed <ooda>
    if "<ooda>" in cleaned and "</ooda>" not in cleaned:
        repaired = cleaned + "\n</ooda>"
        r = parse_ooda_block(repaired)
        if r and (r["observe"] or r["updates"] or r["creates"] or r["reads"]):
            return r

    # Case 2: loose update/create tags with no <ooda> wrapper at all
    if "<ooda>" not in cleaned and (
        re.search(r"<update_state\s", cleaned) or re.search(r"<create_state\s", cleaned)
    ):
        synthetic = "<ooda>\n" + cleaned + "\n</ooda>"
        r = parse_ooda_block(synthetic)
        if r and (r["updates"] or r["creates"]):
            return r

    return None


# ── Tool Executors ──

async def execute_ooda_reads(conv_id: int, reads: list[dict]) -> list[dict]:
    """Batch-execute read_state operations. Returns resolved state data."""
    results = []
    for read in reads:
        card = await db.get_state_card_by_label(conv_id, read["schema_id"], read["label"])
        if card:
            data = json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
            results.append({
                "schema_id": read["schema_id"],
                "label": read["label"],
                "data": data,
            })
        else:
            results.append({
                "schema_id": read["schema_id"],
                "label": read["label"],
                "data": None,
                "note": "No state card found for this label.",
            })
    return results


async def execute_ooda_updates(conv_id: int, updates: list[dict], creates: list[dict]) -> list[dict]:
    """Apply all update_state and create_state operations. Returns changed cards."""
    changed = []
    for upd in updates:
        card = await db.update_state_card_field(
            conv_id, upd["schema_id"], upd["label"], upd["field"], upd["value"]
        )
        if card:
            changed.append(card)
    for cr in creates:
        card = await db.create_state_card(
            conv_id, cr["schema_id"], cr["label"], cr["data"]
        )
        if card:
            changed.append(card)
    return changed


# ── Post-OODA Prose Extraction ──

def extract_post_ooda_prose(text: str) -> str:
    """Extract any prose the model wrote after the </ooda> closing tag."""
    text = _strip_think_blocks(text)
    match = re.search(r'</ooda>\s*(.*)', text, re.DOTALL)
    if match:
        prose = match.group(1).strip()
        # Filter out meta-commentary — if the prose starts with analytical language, skip it
        if prose and not prose.startswith(("Okay,", "Let me", "I need to", "First,", "Wait,")):
            return prose
    return ""


def build_pass2_context(ooda_result: dict, resolved_states: list[dict]) -> str:
    """Build the context message for a second pass from OODA analysis + resolved state data.

    Legacy helper. The production single-pass path no longer calls this; the
    server-side second-pass fallback (in _handle_ooda_generation) uses an inline
    user-message instruction instead. Retained for test_ooda_live.py and as an
    optional context builder if a future design wants the richer framing.

    Framed as in-world state refresh, not meta-instructions, to keep the model in RP mode.
    """
    parts = []

    # Inject resolved states as if they're the character's inner awareness
    if resolved_states:
        for state in resolved_states:
            if state["data"]:
                schema = state["schema_id"].replace("_", " ").title()
                fields = "; ".join(f"{k}: {v}" for k, v in state["data"].items() if v)
                parts.append(f"[{schema} — {state['label']}] {fields}")

    # Feed the model's own orient/decide back as grounding
    if ooda_result.get("orient"):
        parts.append(f"\n[Internal — orientation] {ooda_result['orient']}")
    if ooda_result.get("decide"):
        parts.append(f"[Internal — intent] {ooda_result['decide']}")

    return "\n".join(parts)
