"""Unit tests for the OODA harness — parser, prompt builder, prose extraction."""

import json
import pytest
from ooda_harness import (
    parse_ooda_block,
    repair_ooda_block,
    extract_post_ooda_prose,
    build_ooda_system_prompt,
    build_pass2_context,
)


# ── Parser Tests ──

class TestParseOodaBlock:
    async def test_well_formed_block(self):
        text = """<ooda>
  <observe>The stranger approached cautiously.</observe>
  <read_state schema="character_state" label="Vera"/>
  <read_state schema="scene_state" label="current"/>
  <orient>Vera is suspicious. The alley feels tense.</orient>
  <update_state schema="character_state" label="Vera" field="current_mood" value="alarmed"/>
  <update_state schema="scene_state" label="current" field="atmosphere" value="tense"/>
  <decide>Keep distance, speak curtly, end with a question.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert result is not None
        assert "stranger approached" in result["observe"]
        assert "suspicious" in result["orient"]
        assert "Keep distance" in result["decide"]
        assert len(result["reads"]) == 2
        assert result["reads"][0] == {"schema_id": "character_state", "label": "Vera"}
        assert len(result["updates"]) == 2
        assert result["updates"][0]["field"] == "current_mood"
        assert result["updates"][0]["value"] == "alarmed"

    async def test_bracket_syntax(self):
        """Model sometimes uses [tag ...] instead of <tag .../>."""
        text = """<ooda>
  <observe>Test observation.</observe>
  [read_state schema="character_state" label="Alice"]
  [update_state schema="scene_state" label="current" field="mood" value="dark"]
  <orient>Test orientation.</orient>
  <decide>Test decision.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert result is not None
        assert len(result["reads"]) == 1
        assert result["reads"][0]["label"] == "Alice"
        assert len(result["updates"]) == 1
        assert result["updates"][0]["value"] == "dark"

    async def test_no_ooda_block(self):
        result = parse_ooda_block("Just some regular prose without any OODA block.")
        assert result is None

    async def test_partial_block(self):
        """Only some OODA tags present."""
        text = """<ooda>
  <observe>Something happened.</observe>
  <decide>Respond with action.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert result is not None
        assert result["observe"] == "Something happened."
        assert result["orient"] == ""
        assert result["decide"] == "Respond with action."
        assert len(result["reads"]) == 0
        assert len(result["updates"]) == 0

    async def test_think_blocks_stripped(self):
        text = """<think>Let me reason about this...</think>
<ooda>
  <observe>After thinking, I observe this.</observe>
  <orient>Orientation here.</orient>
  <decide>Decision here.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert result is not None
        assert "After thinking" in result["observe"]

    async def test_create_state_with_json(self):
        text = """<ooda>
  <observe>A new character enters.</observe>
  <create_state schema="character_state" label="Marcus">{"personality": "gruff", "appearance": "scarred"}</create_state>
  <decide>Introduce Marcus.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert len(result["creates"]) == 1
        assert result["creates"][0]["label"] == "Marcus"
        assert result["creates"][0]["data"]["personality"] == "gruff"

    async def test_update_state_with_closing_tag(self):
        """Model sometimes adds a closing tag to self-closing elements."""
        text = """<ooda>
  <observe>Test.</observe>
  <update_state schema="character_state" label="Vera" field="mood" value="happy"></update_state>
  <decide>Continue.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert len(result["updates"]) == 1
        assert result["updates"][0]["value"] == "happy"

    async def test_multiple_reads_and_updates(self):
        text = """<ooda>
  <observe>Complex scene.</observe>
  <read_state schema="character_state" label="Vera"/>
  <read_state schema="character_state" label="Marcus"/>
  <read_state schema="scene_state" label="current"/>
  <read_state schema="lore" label="The Jade Dragon"/>
  <orient>Multiple characters involved.</orient>
  <update_state schema="character_state" label="Vera" field="mood" value="angry"/>
  <update_state schema="character_state" label="Marcus" field="mood" value="worried"/>
  <update_state schema="scene_state" label="current" field="atmosphere" value="explosive"/>
  <decide>Confrontation scene.</decide>
</ooda>"""
        result = parse_ooda_block(text)
        assert len(result["reads"]) == 4
        assert len(result["updates"]) == 3


# ── Repair Tests ──

class TestRepairOodaBlock:
    """repair_ooda_block recovers state deltas when parse_ooda_block returns None.

    Covers the two failure modes that previously caused the harness to silently
    drop state updates: unclosed <ooda> (model ran out of tokens) and loose
    <update_state/> tags emitted with no <ooda> wrapper at all.
    """

    async def test_unclosed_ooda_block(self):
        """Model truncated before </ooda> — repair should close + re-parse."""
        text = (
            "<ooda>\n"
            "  <observe>The stranger approached cautiously.</observe>\n"
            '  <update_state schema="character_state" label="Vera" '
            'field="current_mood" value="alarmed"/>\n'
            "  <decide>Keep distance, speak curtly.</decide>"
            # NOTE: no </ooda> — model ran out of tokens
        )
        # parse_ooda_block must fail (no closing tag)
        assert parse_ooda_block(text) is None
        repaired = repair_ooda_block(text)
        assert repaired is not None
        assert "stranger approached" in repaired["observe"]
        assert "Keep distance" in repaired["decide"]
        assert len(repaired["updates"]) == 1
        assert repaired["updates"][0]["field"] == "current_mood"
        assert repaired["updates"][0]["value"] == "alarmed"

    async def test_loose_update_tags_no_wrapper(self):
        """Model emitted <update_state/> tags with no <ooda> wrapper at all."""
        text = (
            'Some preamble the model wrote.\n'
            '<update_state schema="character_state" label="Vera" '
            'field="current_mood" value="angry"/>\n'
            '<update_state schema="scene_state" label="current" '
            'field="atmosphere" value="tense"/>\n'
            'Then some prose.'
        )
        assert parse_ooda_block(text) is None
        repaired = repair_ooda_block(text)
        assert repaired is not None
        assert len(repaired["updates"]) == 2
        labels = {u["label"] for u in repaired["updates"]}
        assert labels == {"Vera", "current"}

    async def test_well_formed_returns_none(self):
        """Already-well-formed input should not need repair — returns None."""
        text = (
            "<ooda>\n"
            "  <observe>Test.</observe>\n"
            '  <update_state schema="character_state" label="Vera" '
            'field="mood" value="calm"/>\n'
            "  <decide>Act.</decide>\n"
            "</ooda>"
        )
        # parse_ooda_block already succeeds, so repair is not invoked in prod;
        # but if called directly it must not double-repair.
        assert parse_ooda_block(text) is not None
        assert repair_ooda_block(text) is None

    async def test_pure_prose_returns_none(self):
        """No OODA tags at all — nothing to recover."""
        text = "Just prose, no tags here. The wind picked up outside."
        assert parse_ooda_block(text) is None
        assert repair_ooda_block(text) is None

    async def test_think_blocks_stripped_before_repair(self):
        """Reasoning blocks prefixing a truncated <ooda> must not block repair."""
        # Use the exact think-tag characters the model emits and that
        # ooda_harness._strip_think_blocks recognizes.
        text = (
            "<think>Let me think about what Vera would do.</think>"
            "<ooda>\n"
            "  <observe>The stranger approached.</observe>\n"
            '  <update_state schema="character_state" label="Vera" '
            'field="current_mood" value="wary"/>\n'
            "  <decide>Step back.</decide>"
            # no </ooda>
        )
        assert parse_ooda_block(text) is None
        repaired = repair_ooda_block(text)
        assert repaired is not None
        assert "stranger approached" in repaired["observe"]
        assert len(repaired["updates"]) == 1
        assert repaired["updates"][0]["value"] == "wary"

    async def test_unclosed_ooda_empty_returns_none(self):
        """Unclosed <ooda> with no recoverable content should return None."""
        text = "<ooda>\n   \n"  # unclosed but empty
        assert parse_ooda_block(text) is None
        # repair tries to close + parse, but there's no observe/updates/creates
        assert repair_ooda_block(text) is None


class TestExtractPostOodaProse:
    async def test_prose_after_ooda(self):
        text = """<ooda>
  <observe>Test.</observe>
  <decide>Plan.</decide>
</ooda>

Vera stepped back into the shadows, her hand near her knife."""
        prose = extract_post_ooda_prose(text)
        assert "Vera stepped back" in prose

    async def test_no_prose_after_ooda(self):
        text = """<ooda>
  <observe>Test.</observe>
  <decide>Plan.</decide>
</ooda>"""
        prose = extract_post_ooda_prose(text)
        assert prose == ""

    async def test_meta_commentary_filtered(self):
        text = """<ooda><observe>X</observe><decide>Y</decide></ooda>
Okay, let me think about how to write this scene..."""
        prose = extract_post_ooda_prose(text)
        assert prose == ""

    async def test_no_ooda_block(self):
        prose = extract_post_ooda_prose("Just regular text without ooda.")
        assert prose == ""

    async def test_think_blocks_stripped(self):
        text = """<think>reasoning</think>
<ooda><observe>X</observe><decide>Y</decide></ooda>

The rain fell harder now."""
        prose = extract_post_ooda_prose(text)
        assert "rain fell harder" in prose


class TestBuildOodaSystemPrompt:
    async def test_includes_base_and_tools(self):
        prompt = build_ooda_system_prompt("Base prompt here.", [])
        assert "Base prompt here." in prompt
        assert "OODA Workflow" in prompt
        assert "read_state" in prompt
        assert "update_state" in prompt

    async def test_includes_state_summary(self):
        cards = [
            {"schema_id": "character_state", "label": "Vera", "data": json.dumps({"mood": "wary", "goals": "survive"})},
            {"schema_id": "scene_state", "label": "current", "data": json.dumps({"location": "alley"})},
        ]
        prompt = build_ooda_system_prompt("Base.", cards)
        assert "character_state: Vera" in prompt
        assert "mood=wary" in prompt
        assert "scene_state: current" in prompt

    async def test_empty_state_cards(self):
        prompt = build_ooda_system_prompt("Base.", [])
        assert "Current State Cards" not in prompt


class TestBuildPass2Context:
    async def test_includes_orientation_and_states(self):
        ooda = {"observe": "Test obs", "orient": "Test orient", "decide": "Test decide",
                "reads": [], "updates": [], "creates": []}
        resolved = [{"schema_id": "character_state", "label": "Vera", "data": {"mood": "wary"}}]
        ctx = build_pass2_context(ooda, resolved)
        assert "Test orient" in ctx
        assert "Test decide" in ctx
        assert "mood: wary" in ctx

    async def test_empty_resolved(self):
        ooda = {"observe": "", "orient": "Just orient", "decide": "Just decide",
                "reads": [], "updates": [], "creates": []}
        ctx = build_pass2_context(ooda, [])
        assert "Just orient" in ctx
