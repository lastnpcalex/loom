# Weave Mode + OODA Harness — Implementation Map

This file documents findings only. No changes proposed.

## 1. Weave Mode

### 1.1 Definition & mode storage
- Weave is one of several conversation modes (`weave`, `local`, `hermes`, `dream`, `claude`, `gemini`, `codex`, `umans`).
- Default mode at creation: `mode = data.get("mode", "weave")` — server.py:1920
- Stored on the conversation row, plus `ooda_enabled` flag set automatically for Weave:
  - server.py:1989 — `ooda_enabled=1 if mode == "weave" else 0`
- Weave conversations always carry the OODA flag, so the runtime path is effectively `weave == ooda_enabled`.

### 1.2 Request entry point
- WebSocket generation request → `_handle_generation(websocket, conv_id, data)` — server.py:4700
- Routing logic (server.py:4722–4762):
  - `mode in ("claude","gemini","codex")` → CC-family handlers
  - `mode == "local"` → `_handle_local_generation`
  - `mode == "umans"` → `_handle_umans_generation`
  - `mode == "hermes"` → `_handle_hermes_generation`
  - `mode == "dream"` → `_handle_dream_generation` (server.py:4740)
  - Else: dream-model short-circuit at server.py:4744–4748 — if `conv.local_model` matches `config.dream_model`, route to `_handle_dream_completion` instead.
  - Backstage convs (server.py:4753–4757): if `backstage_parent_id` and a `cc_model` was sent inline, inject it as `local_model` so OODA can pick it up.
  - Finally: `if conv.get("ooda_enabled"): _handle_ooda_generation else: _handle_weave_generation` — server.py:4759–4762

### 1.3 Weave turn payload builder — `_handle_weave_generation` (server.py:7492)
Assembles:
1. Character: `load_character(...)` from `config.characters_dir/{character_id}.md` — server.py:7512–7522
2. Style nudge: resolved by name from `STYLE_NUDGES` → `nudge_index` — server.py:7528–7534
3. Persona: `load_persona("personas/{persona_id}.md")` — server.py:7537–7540
4. Lore entries: parsed from `conv.lore_ids` JSON, each loaded via `load_lore_entry("lore/{lid}.md")` — server.py:7547–7563
5. Context: `get_context_for_generation(conv_id, character, leaf_id=parent_id)` (context_manager.py:34) — returns `{summary, verbatim_messages, total_tokens, was_compactified}`. Regenerate truncates verbatim to `parent_id` (server.py:7569–7572).
6. System prompt: `build_system_prompt(character=..., style_nudge_index=..., scenario_override=custom_scene)` (prompt_engine.py:76) — assembles `BASE_SYSTEM_PROMPT` + character personality + scenario + style nudge.
7. Full message array: `assemble_prompt(system_prompt, example_messages, summary, conversation_messages, persona, lore_entries)` (prompt_engine.py:97) — order: system → few-shot examples → persona+lore as a user turn (with a fake assistant ack) → verbatim conversation messages (later system messages folded/dropped).
8. Streams via `stream_chat(messages, model=weave_model)` — server.py:7666–7667

### 1.4 OODA-enhanced Weave payload builder — `_handle_ooda_generation` (server.py:7170)
Same setup block as Weave (character/style/persona/lore/context), then adds OODA scaffolding:
1. Branch-aware state cards (server.py:7236–7245):
   - `state_cards = await db.get_branch_state(conv_id, parent_id)` if there's a parent, else `db.get_state_cards(conv_id)`
   - `global_cards = await db.get_character_state_cards(conv.character_id)` — Tier 1 fallback
2. OODA-augmented system prompt: `build_ooda_system_prompt(base_system, state_cards, global_cards=...)` — server.py:7246–7248 (defined in ooda_harness.py:107)
3. Messages assembled via the same `assemble_prompt(...)` — server.py:7251–7258
4. Single model call: `sync_chat(messages, max_tokens=2048, think=False, model=weave_model)` — server.py:7313–7316. Note `think=False` to suppress reasoning_content.

## 2. OODA Harness (ooda_harness.py)

### 2.1 Module header — still describes the OLD two-pass design
ooda_harness.py:1–11 docstring still says:
```
"""OODA Harness for Weave RP mode.

Two-pass generation loop:
  Pass 1 (Orient): Model emits structured <ooda> block with observations,
                    state reads, orientation, state updates, and a decision.
  Pass 2 (Act):    Server resolves states, feeds enriched context back,
                    model generates final prose.
...
"""
```
This is the chief remnant of the removed two-pass fallback. The actual server loop (server.py:7303–7457) is single-pass: it runs Pass 1 only, then extracts prose from after `</ooda>`.

### 2.2 Single-pass loop + two-pass remnants
- Single-pass OODA loop: server.py:7303 (`# ── Pass 1: Orient ──`) through server.py:7409.
- The "Pass 2" terminology survives only as:
  - server.py:7173 — docstring `"""Handle OODA-enhanced Weave generation — two-pass with state card scaffolding."""`
  - server.py:7303–7304 — `# ── Pass 1: Orient ──` / `print(f"[OODA] Pass 1: Orient...")`
  - server.py:7322 — `print(f"[OODA] Pass 1 done: ...")`
  - ooda_harness.py:246 — `# ── Pass 2 Context Builder ──` (section header for `build_pass2_context`, now unused in production)
  - ooda_harness.py:261 — `build_pass2_context` docstring still references "Pass 2"
  - test_ooda_live.py:90–173 — the live test still branches: if `pass1_prose` is empty it runs a Pass 2 (`sync_chat` again with `build_pass2_context`). This is the only place the two-pass path still executes.
- The explicit single-pass marker: server.py:7400 — `# ── Extract prose (single-pass: prose comes after </ooda> tag) ──`
- AGENTS.md:43, AGENTS.md:153 also describe OODA as "two-pass generation" — stale.

### 2.3 OODA system prompt + tool definitions injected into the prompt
- `OODA_TOOL_DEFINITIONS` constant — ooda_harness.py:22–81. This is the block that tells the model it MUST emit `<ooda>`, with required structure:
  ```
  <ooda>
    <observe>...</observe>
    <read_state schema="..." label="..."/>
    <orient>...</orient>
    <update_state schema="..." label="..." field="..." value="..."/>
    <decide>...</decide>
  </ooda>
  ```
  Plus writing rules (1-3 paragraphs, no markdown, sensory detail, etc.).
- `build_ooda_system_prompt(base_system_prompt, state_cards, global_cards=None)` — ooda_harness.py:107–129:
  1. Joins `[base_system_prompt, "", OODA_TOOL_DEFINITIONS]`
  2. Merges Tier 1 (global/character) into Tier 2 (conversation) cards via `_merge_state_tiers` (ooda_harness.py:84–104) — empty Tier 2 fields inherit from Tier 1.
  3. Appends `## Current State Cards` section, one line per card:
     `[schema_id: label] k1=v1, k2=v2, ...` (ooda_harness.py:118–127)

### 2.4 Regex parser for OODA XML tags — `parse_ooda_block` (ooda_harness.py:134–201)
- Strips `<think>...</think>` blocks first: `re.sub(r'<think>[\s\S]*?</think>\s*', '', text)` (line 141)
- Outer block: `re.search(r'<ooda>(.*?)</ooda>', text, re.DOTALL)` (line 143). If no match → returns `None` (line 145). NOTE: this regex requires the closing `</ooda>` tag. An unclosed `<ooda>` (model ran out of tokens) yields `None` here, which triggers the prose fallback at server.py:7404–7410.
- Inner text tags (`observe`, `orient`, `decide`): `re.search(rf'<{tag}>(.*?)</{tag}>', block, re.DOTALL)` (line 160).
- `read_state` — XML form `<read_state schema="X" label="Y"/>` (line 165) AND bracket form `[read_state schema="X" label="Y"]` (line 167).
- `update_state` — XML form `<update_state .../>` OR `<update_state ...></update_state>`: regex `r'<update_state\s+schema="([^"]+)"\s+label="([^"]+)"\s+field="([^"]+)"\s+value="([^"]+)"\s*/?>(?:</update_state>)?'` (line 171–174). Bracket form: `r'\[update_state\s+...]"\]'` (line 179–181). Both forms are run sequentially and append to `result["updates"]`.
- `create_state` — XML only, requires closing tag: `r'<create_state\s+schema="([^"]+)"\s+label="([^"]+)">(.*?)</create_state>'` with `re.DOTALL` (line 189–192). Body is parsed as JSON; on failure stored as `{"content": ...}` (line 193–199).
- Returns dict `{observe, orient, decide, reads[], updates[], creates[]}` (line 149–156).
- Bracket forms do NOT support `create_state` — only `read_state` and `update_state`.

### 2.5 `extract_post_ooda_prose` (ooda_harness.py:248–257)
```python
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
```
- Returns `""` if no `</ooda>` tag, or if prose starts with one of the meta-commentary prefixes.

### 2.6 Where `parse_ooda_block` returning None triggers the prose fallback
server.py:7326 — `ooda = parse_ooda_block(cleaned_pass1)`
server.py:7400–7410:
```python
# ── Extract prose (single-pass: prose comes after </ooda> tag) ──
final_prose = ""
if ooda:
    final_prose = extract_post_ooda_prose(cleaned_pass1)
if not final_prose:
    # No OODA block or no prose after it — use the whole output
    final_prose = cleaned_pass1
    # Strip closed ooda blocks
    final_prose = _re.sub(r"<ooda>[\s\S]*?</ooda>\s*", "", final_prose).strip()
    # Strip truncated/unclosed ooda blocks (model ran out of tokens)
    final_prose = _re.sub(r"<ooda>[\s\S]*$", "", final_prose).strip()
```
So if the parser returns None (no `<ooda>...</ooda>` matched, including the unclosed-tag case), the entire cleaned output is treated as prose, with any partial `<ooda>` fragment stripped.
Empty-prose branch: server.py:7418–7446 — if `final_prose.strip()` is empty and `ooda` exists, saves a truncated analysis summary; if `ooda` is also None, deletes the draft and emits an error.

### 2.7 State delta mapping (Tier 3 branch deltas) + DB operations
- `execute_ooda_reads(conv_id, reads)` — ooda_harness.py:206–225: per read, `db.get_state_card_by_label(conv_id, schema_id, label)`, returns data or `{"data": None, "note": "No state card found for this label."}`.
- `execute_ooda_updates(conv_id, updates, creates)` — ooda_harness.py:228–243:
  - For updates: `db.update_state_card_field(conv_id, schema_id, label, field, value)` — this MUTATES the Tier 2 base card directly (database.py:1364–1393).
  - For creates: `db.create_state_card(conv_id, schema_id, label, data)` — `INSERT OR IGNORE` (database.py:1317–1337).
- IMPORTANT: `execute_ooda_updates` is invoked at server.py:7388 only for its return value (`resolved`), but the actual DB writes happen here. Then at server.py:7456–7457, the deltas are ALSO saved on the message via `db.save_state_deltas(draft_msg_id, ooda["updates"])`.
- `db.save_state_deltas(msg_id, deltas)` — database.py:1405–1414: `UPDATE messages SET state_deltas = ? WHERE id = ?`. This is the Tier 3 per-message delta store.
- `db.get_branch_state(conv_id, leaf_msg_id)` — database.py:1417–1457: reconstructs effective state by walking `get_branch_to_root(leaf_msg_id)` in chronological order, applying each message's `state_deltas` JSON onto base cards indexed by `(schema_id, label)`. This is the Tier 3 read path used at server.py:7238.
- NOTE: there is a dual-write inconsistency — `execute_ooda_updates` writes through to Tier 2 base cards (mutating them), AND `save_state_deltas` records the same deltas on the message. The OODA_GUIDE.md:40 says "State updates are saved as deltas on the message (not applied to the base cards)" but the code does both. This is a current implementation detail, not a proposed change.

### 2.8 Visibility of OODA steps
server.py:7328–7386 — emits three `tool_start`/`tool_result` WS pairs for observe/orient/decide with `"ooda": True` flag, so the client renders them as collapsible tool blocks.

## 3. How OODA tags are injected into the model prompt
The model never sees raw XML instructions separately — the entire OODA workflow + tag grammar is concatenated into the system prompt by `build_ooda_system_prompt` (ooda_harness.py:107). The assembled system prompt is:
1. `BASE_SYSTEM_PROMPT` (prompt_engine.py:7) — collaborative fiction writer rules
2. Character block (name + personality + scenario) — added by `build_system_prompt` (prompt_engine.py:82–87)
3. Style nudge (one of the 7 in `STYLE_NUDGES`) — prompt_engine.py:89–92
4. `OODA_TOOL_DEFINITIONS` — the full XML workflow spec (ooda_harness.py:22–81), which tells the model: "Before writing your response, you MUST emit an `<ooda>` block" and shows the required structure with `<observe>`, `<read_state/>`, `<orient>`, `<update_state/>`, `<decide>`, plus the "Writing Rules" that prose must follow `</ooda>`.
5. `## Current State Cards` — effective cards (Tier 2 merged with Tier 1), one line each: `[schema_id: label] field=value, ...`

The model is expected to emit `<ooda>...</ooda>` followed by 1-3 paragraphs of prose.

## 4. Which client does the OODA harness call?
- Direct call: server.py:7313–7316 —
  ```python
  weave_model = conv.get("local_model") or None
  raw_pass1 = await sync_chat(
      messages, max_tokens=2048, think=False, model=weave_model
  )
  ```
- `sync_chat` is imported from `local_llm` (server.py:56 — `from local_llm import health_check, stream_chat, sync_chat, describe_image`).
- `local_llm.sync_chat` (local_llm.py:21–22) is a thin pass-through to `llama_client.sync_chat` — `local_llm.py:9` does `import llama_client`.
- `llama_client.sync_chat` (llama_client.py:441–478) POSTs to `{chat_host}/v1/chat/completions` with `stream: False`.

### Can it route to dream_client?
Not directly through `sync_chat`, but effectively YES via a host-swap inside `llama_client`:
- `llama_client._is_dream_model(model)` (llama_client.py:125–126): returns True if `model` matches `config.dream_model` (via `_model_matches` substring match, line 112–122).
- `llama_client._chat_host_for_model(model)` (llama_client.py:129–135): if `_is_dream_model`, returns `config.dream_host` (default `http://localhost:18081`); else `_llama_host()` (the regular llama-server, port 11434).
- `llama_client.sync_chat` uses `_chat_host_for_model(raw_model)` (line 455) and `target_model = config.dream_model if _is_dream_model(raw_model) else _resolve_model(raw_model)` (line 454).
- So if `conv.local_model` equals `config.dream_model`, the OODA harness's `sync_chat` call transparently hits the Dream Engine sidecar's `/v1/chat/completions` endpoint instead of llama-server — using the SAME `llama_client.sync_chat` interface, NOT `dream_client.dream_chat_sync`.

The dedicated `_handle_dream_completion` path (server.py:6380, which DOES call `dream_client.dream_chat_sync` directly at server.py:6413) is short-circuited BEFORE OODA at server.py:4744–4748 — so a `mode=="weave"` + `ooda_enabled` + dream-model conversation would normally be intercepted and routed to `_handle_dream_completion`, bypassing OODA entirely. OODA only reaches the dream sidecar through the host-swap inside `llama_client` if that short-circuit doesn't fire (e.g., `config.dream_model` not set, but `local_model` somehow points at the dream host).

`hermes_client` is a completely separate path (`_handle_hermes_generation`, server.py:6581+ via `hermes_client.run_hermes`) and is never invoked by the OODA harness.

## Summary of key file:line references
- Weave mode flag set: server.py:1989
- Mode routing: server.py:4700–4762
- `_handle_weave_generation`: server.py:7492
- `_handle_ooda_generation`: server.py:7170
- Single-pass OODA loop: server.py:7303–7410
- OODA system prompt builder: ooda_harness.py:107
- OODA tool definitions injected: ooda_harness.py:22–81
- XML parser (bracket + XML forms): ooda_harness.py:134–201
- `extract_post_ooda_prose`: ooda_harness.py:248
- Prose fallback when parser returns None: server.py:7404–7410
- State delta save: database.py:1405 (`save_state_deltas`)
- Branch state reconstruct: database.py:1417 (`get_branch_state`)
- Client call: server.py:7314 → local_llm.py:21 → llama_client.py:441
- Dream host swap: llama_client.py:125–135, 454–455
- Two-pass remnants: ooda_harness.py:1–11 (docstring), :246, :261; server.py:7173 (docstring), :7303–7322; test_ooda_live.py:90–173; AGENTS.md:43, :153
