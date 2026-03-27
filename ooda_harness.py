"""OODA Harness for Weave RP mode.

Two-pass generation loop:
  Pass 1 (Orient): Model emits structured <ooda> block with observations,
                    state reads, orientation, state updates, and a decision.
  Pass 2 (Act):    Server resolves states, feeds enriched context back,
                    model generates final prose.

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

Before writing your response, you MUST emit an <ooda> block that follows the Observe-Orient-Decide-Act cycle. This structures your thinking and keeps the scene grounded.

### Available State Operations

Inside your <ooda> block, you can use these tags:

**Read state** — refresh your understanding of a character, scene, or lore entry:
  <read_state schema="character_state" label="CharacterName"/>
  <read_state schema="scene_state" label="current"/>
  <read_state schema="lore" label="LoreTitle"/>

**Update state** — record changes that happen during this scene beat:
  <update_state schema="character_state" label="CharacterName" field="current_mood" value="alarmed"/>
  <update_state schema="scene_state" label="current" field="atmosphere" value="tense"/>

**Create state** — introduce a new entity:
  <create_state schema="character_state" label="NewCharacter">{"personality": "gruff", "appearance": "scarred face"}</create_state>

### Required OODA Structure

You MUST include update_state tags whenever something changes — moods shift, scenes evolve, relationships develop, characters move. State updates are how the story tracks continuity. If nothing changed, you aren't paying attention.

```
<ooda>
  <observe>What just happened — the user's action, dialogue, or scene development</observe>
  <read_state schema="character_state" label="CharacterName"/>
  <read_state schema="scene_state" label="current"/>
  <orient>How the characters feel and would react, given their states and the situation</orient>
  <update_state schema="character_state" label="CharacterName" field="current_mood" value="new mood based on what happened"/>
  <update_state schema="scene_state" label="current" field="atmosphere" value="how the scene feels now"/>
  <update_state schema="scene_state" label="current" field="recent_events" value="what just happened"/>
  <decide>Plan for your response — key beats, dialogue points, sensory details, pacing</decide>
</ooda>
```

IMPORTANT: Always update at least current_mood and recent_events. If the user did something dramatic, also update atmosphere, physical_situation, and relationships as appropriate.
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

def parse_ooda_block(text: str) -> Optional[dict]:
    """Parse an <ooda>...</ooda> block from model output.

    Returns dict with observe, orient, decide, reads, updates, creates.
    Returns None if no <ooda> block found.
    """
    # Strip <think> blocks first
    text = re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()

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


# ── Pass 2 Context Builder ──

def extract_post_ooda_prose(text: str) -> str:
    """Extract any prose the model wrote after the </ooda> closing tag."""
    text = re.sub(r'<think>[\s\S]*?</think>\s*', '', text).strip()
    match = re.search(r'</ooda>\s*(.*)', text, re.DOTALL)
    if match:
        prose = match.group(1).strip()
        # Filter out meta-commentary — if the prose starts with analytical language, skip it
        if prose and not prose.startswith(("Okay,", "Let me", "I need to", "First,", "Wait,")):
            return prose
    return ""


def build_pass2_context(ooda_result: dict, resolved_states: list[dict]) -> str:
    """Build the context message for Pass 2 from OODA analysis + resolved state data.

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
