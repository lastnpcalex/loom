"""Token counting, context window management, and incremental summarization.

Summarization strategy:
- All messages stay verbatim until total context exceeds max_context_tokens.
- Once over budget, keep the last `verbatim_window` messages verbatim.
- Everything older is covered by a rolling summary, built incrementally:
  each batch of messages is summarized independently by Gemma (fits its 4K context)
  and appended to the running summary, tagged with turn numbers.
  Gemma never re-reads the existing summary — just appends.
"""

from config import config
import database as db
import local_summary
import logging

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (rough: 1 token per ~3 chars)."""
    return len(text) // 3


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a list of messages."""
    total = 0
    for msg in messages:
        total += estimate_tokens(msg.get("content", ""))
        total += 4  # role/formatting overhead
    return total


async def get_context_for_generation(conv_id: int, character: dict = None) -> dict:
    """Build the context window for generation.

    Returns:
        {
            "summary": str or None,
            "verbatim_messages": list[dict],
            "total_tokens": int,
            "was_compactified": bool,
        }
    """
    branch = await db.get_active_branch(conv_id)
    if not branch:
        return {
            "summary": None,
            "verbatim_messages": [],
            "total_tokens": 0,
            "was_compactified": False,
        }

    # Estimate system prompt overhead
    system_overhead = 2000
    if character:
        system_overhead += estimate_tokens(character.get("personality", ""))
        system_overhead += estimate_tokens(character.get("scenario", ""))
        for ex in character.get("example_messages", []):
            system_overhead += estimate_tokens(ex.get("content", ""))

    available_budget = config.max_context_tokens - system_overhead
    total_branch_tokens = sum(msg.get("token_estimate", 0) for msg in branch)

    # If everything fits, no compactification needed
    if total_branch_tokens <= available_budget:
        return {
            "summary": None,
            "verbatim_messages": branch,
            "total_tokens": total_branch_tokens + system_overhead,
            "was_compactified": False,
        }

    # Need compactification: keep last N messages verbatim, use rolling summary for the rest
    verbatim_window = config.verbatim_window
    verbatim_msgs = branch[-verbatim_window:] if len(branch) > verbatim_window else branch
    verbatim_tokens = sum(msg.get("token_estimate", 0) for msg in verbatim_msgs)

    # Get the existing rolling summary
    branch_ids = [m["id"] for m in branch]
    existing_summary = await db.get_summary(conv_id, branch_ids[:5])
    summary_text = existing_summary["content"] if existing_summary else None

    summary_tokens = estimate_tokens(summary_text) if summary_text else 0

    return {
        "summary": summary_text,
        "verbatim_messages": verbatim_msgs,
        "total_tokens": system_overhead + summary_tokens + verbatim_tokens,
        "was_compactified": True,
    }


async def update_rolling_summary(conv_id: int):
    """Incrementally summarize messages that have aged out of the verbatim window.

    Called in the background after each message. Summarizes new messages in small
    batches that fit Gemma's 4K context, appending each batch summary to the
    running summary text. Gemma never re-reads the existing summary.
    """
    branch = await db.get_active_branch(conv_id)
    if not branch or len(branch) <= config.verbatim_window:
        return  # nothing to summarize yet

    # Messages outside the verbatim window
    msgs_outside = branch[:-config.verbatim_window]
    if not msgs_outside:
        return

    # Get existing summary to find what's already covered
    branch_ids = [m["id"] for m in branch]
    existing_summary = await db.get_summary(conv_id, branch_ids[:5])
    last_covered_id = existing_summary["covers_up_to"] if existing_summary else 0
    existing_text = existing_summary["content"] if existing_summary else ""

    # Find uncovered messages — skip system messages (prompt scaffolding, not RP content)
    uncovered = [m for m in msgs_outside
                 if m["id"] > last_covered_id and m["role"] in ("user", "assistant")]
    if not uncovered:
        return  # already up to date

    # Summarize in small batches (~4-6 messages each, fits Gemma's 4K context)
    BATCH_SIZE = 4
    new_chunks = []

    for i in range(0, len(uncovered), BATCH_SIZE):
        batch = uncovered[i:i + BATCH_SIZE]

        # Figure out turn numbers for labeling
        first_turn = msgs_outside.index(batch[0]) + 1 if batch[0] in msgs_outside else i + 1
        last_turn = first_turn + len(batch) - 1

        # Build text for this batch only
        batch_text = ""
        for msg in batch:
            role_label = "Player" if msg["role"] == "user" else "Character"
            text = msg["content"][:1500]  # truncate very long messages
            if msg.get("image_alt"):
                text += f" [Image: {msg['image_alt']}]"
            batch_text += f"[{role_label}]: {text}\n\n"

        try:
            chunk_summary = await local_summary.summarize(
                batch_text,
                max_tokens=200,
                temperature=config.summary_temperature,
            )
            new_chunks.append(f"[Turns {first_turn}-{last_turn}]: {chunk_summary}")
        except Exception as e:
            logger.error(f"Rolling summary batch failed: {e}")
            # Fallback: extract first lines
            lines = []
            for msg in batch:
                role_label = "Player" if msg["role"] == "user" else "Character"
                first_line = msg["content"].strip().split('\n')[0][:150]
                lines.append(f"{role_label}: {first_line}")
            new_chunks.append(f"[Turns {first_turn}-{last_turn}]: {'; '.join(lines)}")

    if not new_chunks:
        return

    # Append new chunks to existing summary
    if existing_text:
        updated_summary = existing_text + "\n" + "\n".join(new_chunks)
    else:
        updated_summary = "\n".join(new_chunks)

    # Save
    covers_up_to = uncovered[-1]["id"]
    await db.save_summary(
        conv_id,
        branch_ids[:5],
        updated_summary,
        covers_up_to
    )

    logger.info(
        f"Rolling summary updated: +{len(new_chunks)} chunks, "
        f"covers up to msg {covers_up_to}"
    )
