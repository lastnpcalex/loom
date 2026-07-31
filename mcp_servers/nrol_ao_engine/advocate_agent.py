"""Advocate subagent runner (Track A phase 2).

Drives the advocate deliberation stage: for each candidate article, the agent
reads the topic's indicator schema (cached across articles by the model itself
re-using the first call's result), optionally checks recent evidence for
duplicate context, then calls ``propose_advocate`` once with a multi-paragraph
``analysis`` field. No rebut, no jury yet — that is Phase 3.

This is a thin wrapper over the phase-1 ``run_engine_agent`` loop. It builds
the user prompt, runs the loop with ``force_first_tool_call=True`` (the Phase-1
finding: DiffusionGemma needs the nudge to emit a tool call instead of narrating
intent), then harvests the proposals the advocate recorded in the in-memory
``advocate._proposals`` store.

SAFETY (phase 2, carried from phase 1):
  - **No commits, no topic mutation, no posterior movement.** ``propose_advocate``
    RECORDS a proposal in an in-process list. There is no import of
    ``pipeline.apply_decisions`` / ``process_evidence`` / ``save_topic`` here.
    Commit is a later phase and will route through the *existing* MCP commit
    gates (Loom approval, governance) — never a new path introduced here.
  - **In-process.** No second MCP server process (A.7 phase-1/2 rule).

Prompt-engineering note (§6 + Phase-1 finding): the system prompt is TERSE and
IMPERATIVE. A verbose prompt describing tool availability ("you have tools…")
causes DiffusionGemma to narrate intent or refuse. The multi-paragraph
analysis demand lives in the ``propose_advocate`` tool description, NOT here —
per the Phase-1 finding that tool-description constraints are honored where
system-prompt prose is not.
"""

from __future__ import annotations

from typing import Any

from .tools import advocate
from .engine_agent import run_engine_agent

ADVOCATE_TOOL_NAMES = (
    "read_indicator_schema",
    "read_recent_evidence",
    "propose_advocate",
)

# Terse + imperative. Names the first action (read the schema) and the
# per-article action (call propose_advocate). The >400-char / citation demand
# is in the tool description, not here — the Phase-1 finding.
ADVOCATE_SYSTEM_PROMPT = (
    "Read the indicator schema, then for each article call propose_advocate "
    "once with a multi-paragraph analysis citing evidence and indicator ids."
)

DEFAULT_MAX_TURNS = 20  # schema read + N articles (each 1-2 turns) + stop
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 900.0


def _build_user_prompt(slug: str, articles: list[dict[str, Any]]) -> str:
    """Build the per-run user prompt listing the articles to advocate on.

    Mirrors the context the legacy ``build_advocate_prompt`` inlined (article
    id, headline, url, source, text) but as a compact list — the agent reads
    the indicator schema via the tool, not from the prompt, so the prompt stays
    short and imperative (Phase-1 finding: terse prompts succeed where verbose
    ones cause refusal).
    """
    lines = [
        f"Topic slug: {slug}",
        "",
        "Articles to evaluate:",
        "",
    ]
    for art in articles:
        aid = art.get("article_id") or art.get("url") or ""
        lines.append(f"[{aid}]")
        lines.append(f"headline: {art.get('headline', '')}")
        lines.append(f"url: {art.get('url', '')}")
        lines.append(f"source: {art.get('source', '')}")
        text = str(art.get("text") or "")
        # Keep the prompt bounded — full article text can be large; the agent
        # already has fetch_article if it needs more, but for phase 2 we hand
        # it the excerpt the scan already extracted.
        if text:
            lines.append(f"text: {text[:2000]}")
        lines.append("")
    lines.append(
        "Call read_indicator_schema first. Then for EACH article above, call "
        "propose_advocate with that article_id, a verdict, proposed_action, a "
        "citation, and a multi-paragraph analysis (>400 chars) citing specific "
        "indicator ids and evidence from the article. Do not skip articles."
    )
    return "\n".join(lines)


def run_advocate(
    slug: str,
    articles: list[dict[str, Any]],
    *,
    model: str | None = None,
    host: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run the advocate stage over a list of articles.

    ``articles`` is a list of ``{article_id, url, headline, source, text}``.

    Returns:
        {
          "slug": str,
          "proposals": list[dict],   # the advocate's recorded proposals
          "trace": dict,             # the raw run_engine_agent trace
          "article_ids": list[str],  # the ids we asked the agent to cover
        }

    The proposals are harvested from the in-memory ``advocate._proposals`` store
    (reset at the start of each run so runs don't bleed). Only proposals whose
    ``article_id`` matches one we passed in are returned — if the model emits a
    spurious call for an unknown article, it's recorded but not surfaced here.
    """
    advocate.reset_proposals()
    article_ids = [str(a.get("article_id") or a.get("url") or "") for a in articles]
    # Also collect the raw urls — the prompt shows both ``[article_id]`` and a
    # ``url:`` line per article, and DiffusionGemma (a diffusion text model,
    # non-deterministic even at temp 0.2) sometimes uses the URL as the
    # article_id in its tool call. Accepting either keeps a legitimate proposal
    # from being filtered out as "hallucinated." A genuinely unknown id (neither
    # a asked article_id nor a asked url) is still dropped.
    article_urls = [str(a.get("url") or "") for a in articles]
    accepted_ids = {aid for aid in (article_ids + article_urls) if aid}

    user_prompt = _build_user_prompt(slug, articles)
    trace = run_engine_agent(
        user_prompt,
        system_prompt=ADVOCATE_SYSTEM_PROMPT,
        model=model,
        host=host,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        force_first_tool_call=True,
        tool_names=ADVOCATE_TOOL_NAMES,
    )

    # Harvest the proposals this run recorded. filter to the article ids (or
    # urls) we asked about so a hallucinated article_id doesn't pollute the
    # result.
    all_proposals = advocate.list_proposals()
    proposals = [p for p in all_proposals if p.get("article_id") in accepted_ids]

    return {
        "slug": slug,
        "article_ids": article_ids,
        "proposals": proposals,
        "trace": trace,
    }
