"""Standalone test: Run the OODA two-pass loop against live Ollama.

Usage:
    python test_ooda_live.py

Requires Ollama running with qwen3.5:9b (or whatever config.ollama_model is set to).
Creates a temporary conversation in the DB, seeds state cards, runs the loop, prints results.
"""

import asyncio
import json
import re
import time

import database as db
from ollama_client import sync_chat
from ooda_harness import (
    build_ooda_system_prompt,
    parse_ooda_block,
    extract_post_ooda_prose,
    execute_ooda_reads,
    execute_ooda_updates,
    build_pass2_context,
)
from prompt_engine import build_system_prompt


async def run_test():
    # ── Setup ──
    await db.init_db()
    print("=" * 60)
    print("OODA HARNESS — LIVE TEST")
    print("=" * 60)

    # Create a temp conversation
    conv = await db.create_conversation("OODA Test", mode="weave")
    conv_id = conv["id"]
    print(f"\nConversation: id={conv_id}")

    # Seed state cards
    await db.create_state_card(conv_id, "character_state", "Vera", {
        "personality": "Sardonic, street-smart, fiercely independent. Distrusts authority. Dry humor masks deep loyalty.",
        "appearance": "Late 20s, dark bobbed hair, leather jacket over a faded band tee, scuffed boots. Scar on left eyebrow.",
        "current_mood": "guarded but curious",
        "goals": "Find out who's been following her. Survive the night.",
        "relationships": "Owes a debt to Marcus. Doesn't trust the stranger yet.",
        "physical_situation": "Standing in a rain-soaked alley behind the Jade Dragon bar, hand near the knife in her boot.",
    })

    await db.create_state_card(conv_id, "scene_state", "current", {
        "location": "Narrow alley behind the Jade Dragon, neon signs reflecting off puddles",
        "time_of_day": "Late night, around 2 AM",
        "atmosphere": "Tense. Rain falling. Distant sirens. The smell of wet concrete and cheap food.",
        "present_characters": "Vera, the stranger (player)",
        "recent_events": "Vera ducked into the alley after noticing someone tailing her. The stranger followed.",
    })

    await db.create_state_card(conv_id, "lore", "The Jade Dragon", {
        "content": "A dive bar in the Hollows district. Run by Marcus Chen, an ex-fixer who went legitimate. Known as a neutral ground — no weapons policy, but everyone ignores it. Back alley is a common dead drop location.",
    }, is_readonly=True)

    state_cards = await db.get_state_cards(conv_id)
    print(f"State cards seeded: {len(state_cards)}")
    for card in state_cards:
        print(f"  [{card['schema_id']}] {card['label']}")

    # ── Build system prompt ──
    # Simulate a character (minimal, since we're testing the harness not the character loader)
    base_system = build_system_prompt(
        character={
            "name": "Vera",
            "personality": "Sardonic, street-smart, fiercely independent.",
            "scenario": "Cyberpunk noir. Rain-soaked city. Trust is a luxury.",
        },
        style_nudge_index=4,  # Sensory-Descriptive
    )

    ooda_system = build_ooda_system_prompt(base_system, state_cards)
    print(f"\nSystem prompt length: {len(ooda_system)} chars")
    print(f"System prompt preview:\n{ooda_system[:500]}...")

    # ── Build messages ──
    user_message = "Hey. You Vera? Marcus said you might be able to help me with something. *steps closer, hands visible*"

    messages = [
        {"role": "system", "content": ooda_system},
        {"role": "user", "content": user_message},
    ]

    # ── Pass 1: Orient ──
    print("\n" + "=" * 60)
    print("PASS 1: ORIENT")
    print("=" * 60)

    t0 = time.time()
    raw_pass1 = await sync_chat(messages, max_tokens=1024)
    t1 = time.time()

    # Strip <think> blocks
    cleaned_pass1 = re.sub(r'<think>[\s\S]*?</think>\s*', '', raw_pass1).strip()

    print(f"\nPass 1 time: {t1 - t0:.1f}s")
    print(f"Raw output length: {len(raw_pass1)} chars")
    print(f"Cleaned output length: {len(cleaned_pass1)} chars")
    print(f"\n--- Pass 1 Output ---\n{cleaned_pass1}\n--- End ---")

    # Parse OODA block
    ooda = parse_ooda_block(cleaned_pass1)
    if not ooda:
        print("\n⚠ No <ooda> block found! Model didn't follow the format.")
        print("This would fall back to treating the output as direct prose.")
        # Cleanup
        await db.delete_conversation(conv_id)
        return

    print(f"\n--- Parsed OODA ---")
    print(f"Observe: {ooda['observe'][:200]}")
    print(f"Orient:  {ooda['orient'][:200]}")
    print(f"Decide:  {ooda['decide'][:200]}")
    print(f"Reads:   {ooda['reads']}")
    print(f"Updates: {ooda['updates']}")
    print(f"Creates: {ooda['creates']}")

    # ── Execute state operations ──
    resolved = await execute_ooda_reads(conv_id, ooda["reads"])
    print(f"\nResolved {len(resolved)} state reads")
    for r in resolved:
        print(f"  [{r['schema_id']}: {r['label']}] {json.dumps(r['data'])[:120] if r['data'] else 'NOT FOUND'}")

    changed = await execute_ooda_updates(conv_id, ooda["updates"], ooda["creates"])
    print(f"Applied {len(changed)} state changes")

    # ── Check for Pass 1 prose (approach C) ──
    pass1_prose = extract_post_ooda_prose(cleaned_pass1)

    if pass1_prose:
        print(f"\n{'=' * 60}")
        print("PASS 1 PROSE DETECTED — skipping Pass 2")
        print(f"{'=' * 60}\n")
        print(pass1_prose)
        final_prose = pass1_prose
        t3 = t1  # no pass 2
    else:
        # ── Pass 2: Act ──
        print("\n" + "=" * 60)
        print("PASS 2: ACT (no Pass 1 prose, running refinement pass)")
        print("=" * 60)

        pass2_context = build_pass2_context(ooda, resolved)
        print(f"\nPass 2 context:\n{pass2_context}\n")

        # Feed the OODA block as assistant turn, resolved state as system context,
        # then let the model continue the conversation naturally
        ooda_only = cleaned_pass1[:cleaned_pass1.index("</ooda>") + len("</ooda>")]
        pass2_messages = list(messages)  # copy original messages
        # Insert resolved state as a system message before the conversation
        pass2_messages.insert(0, {"role": "system", "content": pass2_context})
        # The assistant already spoke (the OODA block), now continue
        pass2_messages.append({"role": "assistant", "content": ooda_only})

        t2 = time.time()
        raw_pass2 = await sync_chat(pass2_messages, max_tokens=1024)
        t3 = time.time()

        cleaned_pass2 = re.sub(r'<think>[\s\S]*?</think>\s*', '', raw_pass2).strip()
        cleaned_pass2 = re.sub(r'<ooda>[\s\S]*?</ooda>\s*', '', cleaned_pass2).strip()

        print(f"\nPass 2 time: {t3 - t2:.1f}s")
        print(f"\n{'=' * 60}")
        print("FINAL RP OUTPUT (Pass 2)")
        print(f"{'=' * 60}\n")
        print(cleaned_pass2)
        final_prose = cleaned_pass2

    print(f"\nTotal time: {t3 - t0:.1f}s")

    # ── Check updated state ──
    print(f"\n{'=' * 60}")
    print("UPDATED STATE CARDS")
    print(f"{'=' * 60}")
    final_cards = await db.get_state_cards(conv_id)
    for card in final_cards:
        data = json.loads(card["data"]) if isinstance(card["data"], str) else card["data"]
        print(f"\n[{card['schema_id']}: {card['label']}]")
        for k, v in data.items():
            print(f"  {k}: {v}")

    # ── Cleanup ──
    await db.delete_conversation(conv_id)
    print(f"\nTest conversation cleaned up.")


if __name__ == "__main__":
    asyncio.run(run_test())
