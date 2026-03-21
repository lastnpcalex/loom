"""Anti-repetition prompt engineering, style nudges, and prompt assembly."""

import re
from collections import Counter
from config import config

# ── Base System Prompt ──

BASE_SYSTEM_PROMPT = """You are a collaborative fiction writer engaged in an ongoing roleplay. Follow these rules precisely:

WRITING CRAFT:
- Vary your openings: rotate among action, dialogue, thought, sensory detail, and environmental description. Never open two consecutive responses the same way.
- Vary sentence length deliberately — mix short punchy fragments with longer flowing sentences.
- Choose specific, vivid verbs and concrete nouns over generic ones. "Sprinted" not "moved quickly." "Oak door" not "the door."
- Show emotion through physical tells (tight jaw, restless hands, averted gaze), not direct statements like "she felt angry."
- Balance action, dialogue, and interiority. No response should be all one mode.
- End at tension points or natural pauses. Leave space for the other writer to act.

ANTI-REPETITION:
- Never repeat a distinctive phrase or sentence structure you used in your last 3 responses.
- Track your own patterns — if you notice yourself gravitating toward a construction, break away from it.
- Vary paragraph count and length between responses."""

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

# ── Common words to exclude from overuse detection ──

COMMON_WORDS = set("""
the a an and or but in on at to for of is it was were be been being
have has had do does did will would could should may might shall can
that this these those with from by as not no nor so if then than
he she they them their his her its we our you your i my me us
said says say just like very much more most some any all each every
which what who whom how when where why there here then now also still
even been into out up down back over again about through before after
between under around without within along across behind beyond
know get go make see come take want look use find give tell think
other another new good first last long great little own old right big
""".split())


class RepetitionDetector:
    """Analyze recent assistant messages for repetitive patterns."""

    def __init__(self, lookback: int = None):
        self.lookback = lookback or config.ngram_lookback

    def extract_ngrams(self, text: str, n: int) -> list[str]:
        words = re.findall(r'\b[a-z]+\b', text.lower())
        return [' '.join(words[i:i+n]) for i in range(len(words) - n + 1)]

    def analyze(self, assistant_messages: list[str]) -> dict:
        """Run full repetition analysis. Returns detected issues."""
        recent = assistant_messages[-self.lookback:] if len(assistant_messages) > self.lookback else assistant_messages
        if len(recent) < 2:
            return {"issues": [], "alert_level": 0}

        issues = []

        # N-gram analysis (3, 4, 5-grams)
        repeated_phrases = []
        for n in (3, 4, 5):
            ngram_sources = {}  # ngram -> set of message indices
            for i, msg in enumerate(recent):
                for ng in self.extract_ngrams(msg, n):
                    ngram_sources.setdefault(ng, set()).add(i)
            for ng, sources in ngram_sources.items():
                if len(sources) >= config.ngram_repeat_threshold:
                    # Skip if it's just common words
                    words = ng.split()
                    if not all(w in COMMON_WORDS for w in words):
                        repeated_phrases.append(ng)

        if repeated_phrases:
            top = repeated_phrases[:5]
            issues.append({
                "type": "repeated_phrases",
                "phrases": top,
                "directive": f"CRITICAL — BANNED PHRASES: Do NOT use any of these phrases or close variants: {', '.join(repr(p) for p in top)}. Find completely different ways to express these ideas."
            })

        # Opening pattern detection
        if len(recent) >= 2:
            openings = []
            for msg in recent:
                first_line = msg.strip().split('\n')[0] if msg.strip() else ""
                first_words = ' '.join(first_line.split()[:3]).lower()
                openings.append(first_words)
            # Check for consecutive same openings
            for i in range(1, len(openings)):
                if openings[i] and openings[i] == openings[i-1]:
                    issues.append({
                        "type": "opening_repetition",
                        "pattern": openings[i],
                        "directive": f"CRITICAL — OPENING VARIETY: Your recent responses start the same way ('{openings[i]}...'). Start this response with a completely different construction — try {self._suggest_opening()}."
                    })
                    break

        # Structure repetition (paragraph counts)
        if len(recent) >= 3:
            para_counts = [len([p for p in msg.split('\n\n') if p.strip()]) for msg in recent[-3:]]
            if len(set(para_counts)) == 1 and para_counts[0] > 1:
                issues.append({
                    "type": "structure_repetition",
                    "pattern": f"{para_counts[0]} paragraphs each",
                    "directive": f"CRITICAL — STRUCTURAL VARIETY: Your last 3 responses all had exactly {para_counts[0]} paragraphs. Use a different structure this time — try {'fewer, denser' if para_counts[0] > 3 else 'more, shorter'} paragraphs."
                })

        # Overused words
        all_text = ' '.join(recent)
        words = re.findall(r'\b[a-z]+\b', all_text.lower())
        word_freq = Counter(words)
        total_words = len(words)
        if total_words > 50:
            overused = []
            for word, count in word_freq.most_common(50):
                if word in COMMON_WORDS or len(word) < 4:
                    continue
                expected_freq = total_words / 500  # rough baseline
                if count >= expected_freq * config.overused_word_multiplier:
                    overused.append(word)
            if overused:
                top = overused[:5]
                issues.append({
                    "type": "overused_words",
                    "words": top,
                    "directive": f"CRITICAL — OVERUSED WORDS: You've been overusing these words: {', '.join(repr(w) for w in top)}. Find synonyms or rephrase to avoid them entirely in this response."
                })

        alert_level = min(len(issues), 3)
        return {"issues": issues, "alert_level": alert_level}

    def _suggest_opening(self) -> str:
        import random
        suggestions = [
            "a line of dialogue",
            "a sensory detail (sound, smell, texture)",
            "a physical action or gesture",
            "an environmental description",
            "an internal thought or memory",
            "a question or rhetorical observation",
        ]
        return random.choice(suggestions)


def get_style_nudge(index: int) -> dict:
    """Get the current style nudge by rotation index."""
    return STYLE_NUDGES[index % len(STYLE_NUDGES)]


def build_system_prompt(character: dict = None, style_nudge_index: int = 0,
                        repetition_directives: list[str] = None,
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

    # Anti-repetition directives
    if repetition_directives:
        parts.append("\n" + "\n".join(repetition_directives))

    return "\n".join(parts)


def assemble_prompt(system_prompt: str, example_messages: list[dict] = None,
                    summary: str = None, conversation_messages: list[dict] = None,
                    persona: dict = None, lore_entries: list[dict] = None) -> list[dict]:
    """Build the full message array for the LLM.

    Order:
    1. System prompt (character + scenario + nudge + anti-rep)
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
                entry["image_path"] = msg["image_path"]
            messages.append(entry)

    return messages
