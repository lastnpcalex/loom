"""Style nudges and prompt assembly."""

from config import config

# ── Base System Prompt ──

BASE_SYSTEM_PROMPT = """You are a collaborative fiction writer in an ongoing roleplay. Stay in character, never break the fourth wall, and never speak or act for the player's character. Show emotion through action and dialogue — vary your openings, sentence length, and structure between responses. End at natural pause points that invite the other writer to respond."""

# ── Style Nudges (user-selectable, not auto-rotating) ──

STYLE_NUDGES = [
    {
        "name": "Natural",
        "prompt": "",  # No nudge — respond naturally to whatever's happening
    },
    {
        "name": "Cinematic Action",
        "prompt": """CURRENT STYLE EMPHASIS — CINEMATIC ACTION:
Write this response with emphasis on physical movement, spatial awareness, and choreography.
Use film-direction language: tracking shots, close-ups, cuts between perspectives.
Let the body tell the story — posture, gesture, the mechanics of action.
Keep dialogue minimal; let movement speak.""",
    },
    {
        "name": "Dialogue-Driven",
        "prompt": """CURRENT STYLE EMPHASIS — DIALOGUE-DRIVEN:
Write this response with emphasis on conversation and verbal exchange.
Layer subtext beneath the words — what's said vs. what's meant.
Give characters distinct speech patterns, rhythms, and verbal tics.
Use dialogue tags sparingly; let the words identify the speaker.
Weave action beats between lines of dialogue rather than long description blocks.""",
    },
    {
        "name": "Introspective",
        "prompt": """CURRENT STYLE EMPHASIS — INTROSPECTIVE:
Write this response with emphasis on inner experience and psychological depth.
Dive into the character's subjective perception — how they interpret what's happening.
Use memory fragments, associations, and private thoughts.
Let the external world filter through the character's emotional state.
Slow the pace; linger on moments of internal shift.""",
    },
    {
        "name": "Sensory-Descriptive",
        "prompt": """CURRENT STYLE EMPHASIS — SENSORY-DESCRIPTIVE:
Write this response with emphasis on atmosphere and the five senses.
Ground every moment in what can be seen, heard, smelled, tasted, touched.
Let the environment carry emotional weight — pathetic fallacy, symbolic detail.
Use texture: rough, smooth, damp, electric, hollow.
Paint the scene so vividly the reader feels present in it.""",
    },
    {
        "name": "Tension/Suspense",
        "prompt": """CURRENT STYLE EMPHASIS — TENSION/SUSPENSE:
Write this response with emphasis on building unease and narrative tension.
Control pacing: slow reveals, withheld information, pregnant pauses.
Include small wrong details — things slightly off, noticed but not explained.
Use short sentences to accelerate. Long ones to create dread.
End on an unresolved note. Make the reader need to know what happens next.""",
    },
    {
        "name": "Lyrical/Poetic",
        "prompt": """CURRENT STYLE EMPHASIS — LYRICAL/POETIC:
Write this response with emphasis on language as music.
Use rhythm, cadence, and carefully chosen metaphor.
Let sentences flow with intentional prosody — read it aloud in your head.
Favor precision over ornament: the right word, not the fancy word.
Find beauty in unexpected comparisons. Make familiar things strange again.""",
    },
]

def get_style_nudge(index: int) -> dict:
    """Get the current style nudge by rotation index."""
    return STYLE_NUDGES[index % len(STYLE_NUDGES)]


def build_system_prompt(character: dict = None, style_nudge_index: int = 0,
                        scenario_override: str = None) -> str:
    """Assemble the full system prompt."""
    parts = [BASE_SYSTEM_PROMPT]

    # Character info
    if character:
        if character.get("personality"):
            parts.append(f"\nCHARACTER — {character['name'].upper()}:\n{character['personality']}")
        scenario = scenario_override or character.get("scenario", "")
        if scenario:
            parts.append(f"\nSCENARIO:\n{scenario}")

    # Style nudge (only if not "Natural")
    nudge = get_style_nudge(style_nudge_index)
    if nudge["prompt"]:
        parts.append(f"\n{nudge['prompt']}")

    return "\n".join(parts)


def assemble_prompt(system_prompt: str, example_messages: list[dict] = None,
                    summary: str = None, conversation_messages: list[dict] = None,
                    persona: dict = None, lore_entries: list[dict] = None) -> list[dict]:
    """Build the full message array for the LLM.

    Order:
    1. System prompt (character + scenario + style nudge)
    2. Example messages (few-shot)
    3. Persona + lore (as a USER turn so the LLM doesn't confuse identities)
    4. Summary of older context
    5. Recent verbatim messages
    """
    messages = [{"role": "system", "content": system_prompt}]

    # Few-shot examples
    if example_messages:
        for ex in example_messages:
            messages.append({"role": ex["role"], "content": ex["content"]})

    # Persona + lore as a USER turn (keeps LLM clear on who's who)
    context_parts = []
    if persona:
        context_parts.append(f"[My character: {persona['name']}]\n{persona['content']}")
    if lore_entries:
        for entry in lore_entries:
            context_parts.append(f"[Background — {entry['name']}]\n{entry['content']}")
    if context_parts:
        messages.append({
            "role": "user",
            "content": "\n\n".join(context_parts)
        })
        # Fake assistant acknowledgment to keep turn order valid
        messages.append({
            "role": "assistant",
            "content": "(Understood. I'll keep this context in mind and stay in character.)"
        })

    # Summary
    if summary:
        messages.append({
            "role": "system",
            "content": f"STORY SO FAR (summary of earlier events):\n{summary}"
        })

    # Verbatim conversation messages
    if conversation_messages:
        for msg in conversation_messages:
            entry = {"role": msg["role"], "content": msg["content"]}
            if msg.get("image_path"):
                entry["image_path"] = msg["image_path"]  # may be string or JSON array
            messages.append(entry)

    return messages
