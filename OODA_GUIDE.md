# OODA Harness — System Guide & Best Practices

A comprehensive guide to the OODA (Observe-Orient-Decide-Act) harness in A Shadow Loom, including how the system works, how to write effective state cards, and how to tune for different model sizes.

## How the OODA Harness Works

### The Problem

Local language models (7B-14B parameters) struggle with long RP prompts. Character consistency degrades, scene state gets lost, and prose quality drops as conversations grow. Front-loading everything into the system prompt works for large cloud models but overwhelms smaller ones.

### The Solution

Instead of dumping all context into a single prompt, the OODA harness structures the model's thinking into a deliberate reasoning loop before it writes each response. The model reads structured state cards, reasons about the scene, updates any changes, and only then generates prose — grounded in the analysis it just performed.

This approach is inspired by:
- **[metacog](https://github.com/inanna-malick/metacog)** — the insight that LLMs treat tool results as ground truth. A state card read via a tool call carries more weight than the same information buried in a system prompt.
- **[popup-mcp](https://tidepool.leaflet.pub/3mcbegnuf2k2i)** — amortize expensive LLM calls into fewer, richer passes rather than many small round-trips.

### The Flow

When you send a message in a Weave conversation with OODA enabled:

1. **System prompt assembly** — the base RP prompt, character definition, style nudge, and OODA tool definitions are assembled. Current state cards are summarized and appended.

2. **Single-pass generation** — the model receives the full prompt and emits:
   ```xml
   <ooda>
     <observe>What just happened in the scene</observe>
     <read_state schema="character_state" label="Vera"/>
     <read_state schema="scene_state" label="current"/>
     <orient>How Vera would react given her mood, goals, the situation</orient>
     <update_state schema="character_state" label="Vera" field="current_mood" value="alarmed"/>
     <update_state schema="scene_state" label="current" field="atmosphere" value="tense"/>
     <decide>Plan for the response — key beats, dialogue, sensory details</decide>
   </ooda>

   [Prose response follows the closing tag]
   ```

3. **Server processing** — the OODA block is parsed. State reads resolve against the database. State updates are saved as deltas on the message (not applied to the base cards). The prose after `</ooda>` becomes the displayed response.

4. **Visibility** — the Observe, Orient, and Decide steps appear as collapsible tool blocks in the conversation, so you can see the model's reasoning.

### State Card Tiers

State cards exist at three levels:

| Tier | Scope | Mutated by OODA? | Purpose |
|------|-------|-------------------|---------|
| **Tier 1** (Character Global) | Per-character, shared across conversations | No | Baseline template — personality, default appearance, backstory |
| **Tier 2** (Conversation) | Per-conversation, shared across branches | No | Starting state for this scenario — copied from Tier 1 on creation |
| **Tier 3** (Branch Deltas) | Per-message on each branch | Yes | Changes that happen during the story — mood shifts, scene evolution |

When the model reads state during generation, it sees: **Tier 2 base + Tier 3 deltas along the current branch path**. Different branches naturally diverge because each carries its own delta chain.

## State Card Types

### Character State

Tracks the current state of an NPC or significant character.

| Field | Purpose | Best Practice |
|-------|---------|---------------|
| `personality` | Core traits that drive behavior | 3-5 defining traits. Be specific: "sardonic, distrustful, secretly loyal" not "complex personality" |
| `appearance` | Physical details for sensory writing | Include clothing, posture, distinctive features. The model uses these for action descriptions. |
| `current_mood` | Emotional baseline for the next response | Updated by the model each turn. Edit manually to steer emotional direction. |
| `goals` | What the character is actively pursuing | Drives proactive behavior. "Find the artifact" generates different scenes than "survive the night" |
| `relationships` | How this character feels about others | Key driver of dialogue tone. "Distrusts the stranger" vs "Owes them a debt" |
| `physical_situation` | Where and how the character is positioned | Grounds the prose physically. "Standing in doorway, hand on knife" |

### Scene State

Tracks the current scene environment.

| Field | Purpose | Best Practice |
|-------|---------|---------------|
| `location` | Where the scene takes place | Be specific enough to inspire sensory detail. "Rain-soaked alley behind a neon-lit bar" not just "an alley" |
| `time_of_day` | Affects atmosphere and character energy | "Late night, 2 AM" gives the model more to work with than "night" |
| `atmosphere` | Emotional texture of the scene | The model updates this as tension shifts. "Tense, rain falling, distant sirens" |
| `present_characters` | Who is in the scene | The model won't write absent characters if this is maintained |
| `recent_events` | What just happened | Prevents repetition and keeps continuity tight across turns |

### Persona State

Tracks the player's character (your avatar in the RP).

| Field | Purpose | Best Practice |
|-------|---------|---------------|
| `description` | Who you are in this RP | Personality, background, skills, mannerisms |
| `appearance` | Your physical details | What the NPC characters see when they look at you |
| `goals` | Your active motivations | Helps the model anticipate your character's direction |

### Lore

Read-only background information referenced when relevant.

| Field | Purpose | Best Practice |
|-------|---------|---------------|
| `content` | Background world information | Factions, history, locations, rules of the world. Keep entries focused — one lore card per topic. |

## Best Practices by Model Size

### Small Models (1B-4B parameters)

These models have limited reasoning capacity. The OODA harness helps most here, but you need to keep things simple.

**State cards:**
- Keep personality to 1-2 sentences
- Use single words or short phrases for mood, atmosphere
- Limit to 2-3 state cards total (one character, one scene)
- Skip lore cards — the model can't juggle too many inputs
- Goals should be one clear sentence

**Example:**
```
[character_state: Vera]
personality: Sarcastic, wary
appearance: Dark hair, leather jacket
current_mood: suspicious
goals: Find out who's following her
```

**What to expect:** The model will follow the OODA structure but may produce shorter, less nuanced prose. The state updates may be simplistic ("mood: happy"). This is fine — the harness still prevents character drift.

### Medium Models (7B-14B parameters)

The sweet spot for the OODA harness. Models like Qwen 3.5 9B handle the XML format well and produce quality state reasoning.

**State cards:**
- Personality can be a full paragraph
- Use descriptive phrases for mood and atmosphere
- 3-5 state cards work well (character, scene, persona, 1-2 lore)
- Include relationships and physical_situation
- Goals can be multi-part

**Example:**
```
[character_state: Vera]
personality: Sardonic, street-smart, fiercely independent. Distrusts authority. Dry humor masks deep loyalty.
appearance: Late 20s, dark bobbed hair, leather jacket over a faded band tee, scuffed boots. Scar on left eyebrow.
current_mood: guarded but curious
goals: Find out who's been following her. Survive the night.
relationships: Owes a debt to Marcus. Doesn't trust the stranger yet.
physical_situation: Standing in a rain-soaked alley, hand near the knife in her boot
```

**What to expect:** Rich OODA blocks with nuanced observations and multi-field state updates. Prose quality is noticeably better than without the harness. The model tracks mood shifts, updates atmosphere, and maintains character voice.

### Large Models (30B+ parameters)

These models can handle more complex state without the harness, but the OODA structure still helps with consistency across long conversations.

**State cards:**
- Full character sheets with detailed backstory
- Multiple relationship entries with nuance
- Detailed scene state with sensory information
- Multiple lore cards for world-building depth
- Persona with full personality and motivation

**Example:**
```
[character_state: Vera]
personality: Sardonic, street-smart, fiercely independent. Grew up running cons in Thornhaven port.
  Distrusts authority figures on principle but respects competence. Dry humor masks a deep capacity
  for loyalty that she considers a weakness. Fights dirty when she fights at all — prefers to talk
  or sneak her way through problems. Well-read despite claiming otherwise.
appearance: Late 20s, dark bobbed hair with copper streaks, leather jacket over band tees. Collection
  of scars she considers "conversation starters." Carries a locket she claims is "just valuable"
  but never sells. Sharp features, quick eyes that catalogue details like a fence appraising goods.
current_mood: restless, between jobs, bored enough to be dangerous
goals: Find work worth doing. Figure out what's making travelers disappear in the Ashenmire.
  Don't think too hard about why she keeps the locket.
relationships: Knows the barkeep (useful, not trusted). Owes a favor in Thornhaven she's avoiding.
  Marcus: dead drop contact, professional respect only. The stranger: unknown quantity, interesting.
physical_situation: Sitting at a tavern table, nursing a drink, boot resting on chair, watching the door
```

**What to expect:** Detailed OODA blocks that read like character analysis. The model may create new state cards for newly introduced characters. Prose is rich with callbacks to established details.

### Cloud Models (Claude, GPT-4)

Cloud models through Loom mode don't use the OODA harness (it's Weave-only), but the state card system still provides useful context structuring if you reference state cards in your prompts.

## Tips for All Model Sizes

1. **current_mood is the most impactful field.** It directly shapes tone, dialogue, and action in every response. If only one field gets updated per turn, this should be it.

2. **recent_events prevents repetition.** Without this, the model may re-describe the same scene setup. With it, the model knows what already happened.

3. **Relationships drive dialogue quality.** "Distrusts the stranger" produces fundamentally different dialogue than "cautiously curious about the stranger."

4. **Edit between turns to steer.** If the story is going somewhere you don't want, change a mood or goal before your next message. The model reads these fresh each turn.

5. **The OODA block is visible.** Read it. If the model's orientation is wrong ("Vera would be happy about the betrayal"), that tells you the state cards need updating.

6. **Don't over-describe in personality.** The personality field sets the baseline but the model's actual behavior comes from the combination of personality + mood + goals + relationships + what just happened. A terse personality with rich state produces better RP than a novel-length personality with empty state.

7. **Scene atmosphere is emotional, not physical.** "Tense, claustrophobic, the smell of ozone and cheap coffee" is more useful than "a room with fluorescent lights." The model generates its own physical details — atmosphere guides the emotional register.

8. **Lore cards are referenced, not recited.** The model doesn't dump lore into the response — it uses lore to inform decisions. "The Jade Dragon is neutral ground, no weapons" means the model knows a fight there breaks the rules, without stating that explicitly in the prose.

## Troubleshooting

### Model doesn't emit `<ooda>` blocks
The model may not follow the XML format. The harness falls back to treating the entire output as prose. Try a larger model or check that the OODA system prompt is being included (check server.log for `[OODA] System prompt:` lines).

### State updates aren't happening
Check server.log for `[OODA] Resolved X reads, applying Y updates`. If Y is 0, the model isn't emitting `<update_state>` tags. The prompt instructs "If nothing changed, you aren't paying attention" — but smaller models may still skip updates. Consider manually updating state between turns.

### Prose quality is low
Check the Decide step in the tool blocks. If the decision is vague ("write a response"), the state cards may not have enough detail to ground the model. Add more specificity to personality, mood, and goals.

### Branch state shows wrong data
Deltas are saved per-message. If you navigate to a branch that was created before the OODA fix (before Tier 3), older messages won't have deltas and the branch state will reflect the base cards. Only new generations save branch-specific deltas.
