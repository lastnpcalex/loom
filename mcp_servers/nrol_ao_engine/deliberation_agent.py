"""Deliberation runner: advocate → rebut → jury (Track A phase 3).

Drives the full three-stage deliberation packet. ``run_deliberation`` runs the
advocate stage (phase 2's ``advocate_agent.run_advocate``), then runs a rebut
subagent with the advocate's structured proposals injected as context, then
runs a jury subagent with both the advocate and rebut records injected. Each
subagent calls its typed tool (``propose_rebut`` / ``submit_jury``) which
RECORDS the verdict in an in-process list — no commit, no topic mutation, no
posterior movement (§A.6 phase 3).

This is a thin composition over the phase-1 ``run_engine_agent`` loop. Each
stage gets its own loop with a terse imperative system prompt (Phase-1
finding: DiffusionGemma needs the nudge, not verbose "you have tools" prose)
and ``force_first_tool_call=True``. The multi-paragraph analysis / citation
demands live in the tool descriptions, not the system prompts.

SAFETY (phase 3, carried from phases 1-2):
  - **No commits, no topic mutation, no posterior movement.** ``propose_rebut``
    and ``submit_jury`` RECORD verdicts in in-process lists. There is no
    import of ``pipeline.apply_decisions`` / ``process_evidence`` / ``save_topic``
    here. Commit is a later phase and will route through the *existing* commit
    gates (Loom approval, governance) — never a new path introduced here.
  - **In-process.** No second MCP server process (A.7 phase-3 rule).

Context injection (A.3): the rebut subagent receives the article text/metadata
PLUS the full advocate proposal records (including the multi-paragraph
``analysis``). The jury subagent receives the article text/metadata PLUS both
the advocate and rebut records. This is the structured-record flow the legacy
``build_rebut_prompt`` / ``build_jury_prompt`` collapsed to one-liners — here
the full ``analysis`` / ``rebuttal_analysis`` text is passed verbatim, so the
jury sees the actual argument, not a sentence.
"""

from __future__ import annotations

import json
from typing import Any

from .engine_agent import run_engine_agent
from .tools import advocate, jury, rebut

REBUT_TOOL_NAMES = (
    "read_indicator_schema",
    "propose_rebut",
)

JURY_TOOL_NAMES = (
    "read_indicator_schema",
    "submit_jury",
)

# ──────────────────────────────────────────────────────────────────────────
# Terse imperative system prompts (Phase-1 finding: terse > verbose)
# ──────────────────────────────────────────────────────────────────────────
#
# Each names the action the subagent must take. The multi-paragraph / citation
# / cross-reference demands live in the tool descriptions (propose_rebut,
# submit_jury), NOT here — per the Phase-1 finding that tool-description
# constraints are honored where system-prompt prose is not.

REBUT_SYSTEM_PROMPT = (
    "Read the indicator schema, then for each advocate proposal call "
    "propose_rebut with objections or agreement."
)

JURY_SYSTEM_PROMPT = (
    "Read the indicator schema, then for each case call submit_jury with the "
    "final action."
)

DEFAULT_MAX_TURNS = 30  # schema read + N proposals per stage + stop, across 3 stages
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 900.0


# ──────────────────────────────────────────────────────────────────────────
# Prompt builders
# ──────────────────────────────────────────────────────────────────────────


def _format_article(art: dict[str, Any]) -> list[str]:
    """Render one article's metadata + text as prompt lines."""
    aid = art.get("article_id") or art.get("url") or ""
    lines = [f"[{aid}]"]
    lines.append(f"headline: {art.get('headline', '')}")
    lines.append(f"url: {art.get('url', '')}")
    lines.append(f"source: {art.get('source', '')}")
    text = str(art.get("text") or "")
    if text:
        lines.append(f"text: {text[:2000]}")
    return lines


def _build_rebut_prompt(
    slug: str,
    articles: list[dict[str, Any]],
    advocate_proposals: list[dict[str, Any]],
) -> str:
    """Build the rebut subagent's user prompt.

    Lists the articles AND the full advocate proposal records (including the
    multi-paragraph analysis) as structured context. The rebut sees the
    advocate's actual argument, not a collapsed one-liner (A.3).
    """
    lines: list[str] = [f"Topic slug: {slug}", "", "Articles:", ""]
    for art in articles:
        lines.extend(_format_article(art))
        lines.append("")

    lines.append("Advocate proposals (scrutinize each):")
    lines.append("")
    for p in advocate_proposals:
        lines.append(f"article_id: {p.get('article_id', '')}")
        lines.append(f"advocate_proposal_id: {p.get('proposal_id', '')}")
        lines.append(f"verdict: {p.get('verdict', '')}")
        lines.append(f"proposed_action: {json.dumps(p.get('proposed_action') or {}, ensure_ascii=True)}")
        lines.append(f"citation: {p.get('citation', '')}")
        # The full multi-paragraph analysis — passed verbatim so the rebut can
        # engage with the actual argument, not a summary.
        lines.append(f"analysis: {p.get('analysis', '')}")
        lines.append("")

    lines.append(
        "Call read_indicator_schema first. Then for EACH advocate proposal "
        "above, call propose_rebut with that article_id and "
        "advocate_proposal_id, a verdict, whether you object, a corrected "
        "action, and a multi-paragraph rebuttal_analysis (>300 chars) that "
        "references the advocate's proposal and cites indicator/evidence ids. "
        "Do not skip proposals."
    )
    return "\n".join(lines)


def _build_jury_prompt(
    slug: str,
    articles: list[dict[str, Any]],
    advocate_proposals: list[dict[str, Any]],
    rebuttals: list[dict[str, Any]],
) -> str:
    """Build the jury subagent's user prompt.

    Lists the articles PLUS both the advocate and rebut records (full analysis
    text) as structured context. The jury is a fresh voter that sees both prior
    rounds' actual arguments, not one-liners (A.3).
    """
    lines: list[str] = [f"Topic slug: {slug}", "", "Articles:", ""]
    for art in articles:
        lines.extend(_format_article(art))
        lines.append("")

    # Index proposals + rebuttals by article_id so the jury sees each case as
    # advocate+rebut paired. A proposal/rebuttal may carry either the
    # article_id or the url as its ``article_id`` field (DiffusionGemma
    # sometimes uses the URL), so index under BOTH keys.
    prop_by_aid: dict[str, dict[str, Any]] = {}
    for p in advocate_proposals:
        prop_by_aid[str(p.get("article_id") or "")] = p
    reb_by_aid: dict[str, dict[str, Any]] = {}
    for r in rebuttals:
        reb_by_aid[str(r.get("article_id") or "")] = r

    lines.append("Cases (advocate + rebuttal records):")
    lines.append("")
    for art in articles:
        aid = str(art.get("article_id") or "")
        url = str(art.get("url") or "")
        # Look up by either key (article_id or url) to tolerate the model's
        # choice of identifier.
        p = prop_by_aid.get(aid) or prop_by_aid.get(url) or {}
        r = reb_by_aid.get(aid) or reb_by_aid.get(url) or {}
        label = aid or url
        lines.append(f"=== {label} ===")
        lines.append(f"advocate_proposal_id: {p.get('proposal_id', '')}")
        lines.append(f"advocate_verdict: {p.get('verdict', '')}")
        lines.append(f"advocate_proposed_action: {json.dumps(p.get('proposed_action') or {}, ensure_ascii=True)}")
        lines.append(f"advocate_citation: {p.get('citation', '')}")
        lines.append(f"advocate_analysis: {p.get('analysis', '')}")
        lines.append(f"rebuttal_id: {r.get('rebuttal_id', '')}")
        lines.append(f"rebut_verdict: {r.get('verdict', '')}")
        lines.append(f"rebut_objection_raised: {r.get('objection_raised', '')}")
        lines.append(f"rebut_objection_details: {r.get('objection_details', '')}")
        lines.append(f"rebut_corrected_action: {json.dumps(r.get('corrected_action') or {}, ensure_ascii=True)}")
        lines.append(f"rebut_rebuttal_analysis: {r.get('rebuttal_analysis', '')}")
        lines.append("")

    lines.append(
        "Call read_indicator_schema first. Then for EACH case above, call "
        "submit_jury with that article_id, its advocate_proposal_id and "
        "rebuttal_id, a final_action, and a multi-paragraph jury_rationale "
        "(>300 chars) that references BOTH the advocate and rebuttal records "
        "and explains why the final action accepts, modifies, or rejects the "
        "advocate proposal. Do not skip cases."
    )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Stage runners
# ──────────────────────────────────────────────────────────────────────────


def _run_rebut(
    slug: str,
    articles: list[dict[str, Any]],
    advocate_proposals: list[dict[str, Any]],
    *,
    model: str | None,
    host: str | None,
    max_turns: int,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    """Run the rebut stage. Returns ``{rebuttals, trace}``.

    Harvests recorded rebuttals filtered to the advocate proposal ids we
    passed in — a hallucinated advocate_proposal_id is recorded but not surfaced.
    """
    rebut.reset_rebuttals()
    article_ids = [str(a.get("article_id") or a.get("url") or "") for a in articles]
    article_urls = [str(a.get("url") or "") for a in articles]
    accepted_ids = {aid for aid in (article_ids + article_urls) if aid}
    proposal_ids = {str(p.get("proposal_id") or "") for p in advocate_proposals}

    user_prompt = _build_rebut_prompt(slug, articles, advocate_proposals)
    trace = run_engine_agent(
        user_prompt,
        system_prompt=REBUT_SYSTEM_PROMPT,
        model=model,
        host=host,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        force_first_tool_call=True,
        tool_names=REBUT_TOOL_NAMES,
    )

    all_rebuttals = rebut.list_rebuttals()
    # Keep rebuttals whose advocate_proposal_id matches one we asked about, OR
    # whose article_id matches a asked article_id or url — the model sometimes
    # uses the URL as the article_id (same flakiness the advocate harvest sees).
    rebuttals = [
        r for r in all_rebuttals
        if r.get("advocate_proposal_id") in proposal_ids
        or r.get("article_id") in accepted_ids
    ]
    return {"rebuttals": rebuttals, "trace": trace}


def _run_jury(
    slug: str,
    articles: list[dict[str, Any]],
    advocate_proposals: list[dict[str, Any]],
    rebuttals: list[dict[str, Any]],
    *,
    model: str | None,
    host: str | None,
    max_turns: int,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    """Run the jury stage. Returns ``{verdicts, trace}``.

    Harvests recorded verdicts filtered to the article ids we passed in.
    """
    jury.reset_verdicts()
    article_ids = [str(a.get("article_id") or a.get("url") or "") for a in articles]
    article_urls = [str(a.get("url") or "") for a in articles]
    accepted_ids = {aid for aid in (article_ids + article_urls) if aid}

    user_prompt = _build_jury_prompt(slug, articles, advocate_proposals, rebuttals)
    trace = run_engine_agent(
        user_prompt,
        system_prompt=JURY_SYSTEM_PROMPT,
        model=model,
        host=host,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        force_first_tool_call=True,
        tool_names=JURY_TOOL_NAMES,
    )

    all_verdicts = jury.list_verdicts()
    verdicts = [v for v in all_verdicts if v.get("article_id") in accepted_ids]
    return {"verdicts": verdicts, "trace": trace}


# ──────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────


def run_deliberation(
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
    """Run the full advocate → rebut → jury deliberation packet.

    ``articles`` is a list of ``{article_id, url, headline, source, text}``.

    Returns:
        {
          "slug": str,
          "advocate_proposals": list[dict],  # advocate's recorded proposals
          "rebuttals": list[dict],           # rebut's recorded rebuttals
          "jury_verdicts": list[dict],       # jury's recorded verdicts
          "traces": {                        # per-stage run_engine_agent traces
            "advocate": dict,
            "rebut": dict,
            "jury": dict,
          },
        }

    No commit, no topic mutation, no posterior movement. The records live in
    in-process lists (``advocate._proposals``, ``rebut._rebuttals``,
    ``jury._verdicts``); persistence is out of scope for phase 3.
    """
    # Import lazily so the package can be imported even if advocate_agent
    # transiently fails (it won't, but the cycle stays clean).
    from . import advocate_agent

    # Reset all three stores up front so a prior run never bleeds in. (Each
    # stage runner also resets its own store, but doing it here keeps the
    # contract explicit at the composition boundary.)
    advocate.reset_proposals()
    rebut.reset_rebuttals()
    jury.reset_verdicts()

    # Stage 1: advocate. run_advocate resets its own store, runs, and harvests.
    adv_result = advocate_agent.run_advocate(
        slug,
        articles,
        model=model,
        host=host,
        max_turns=max_turns,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    advocate_proposals = adv_result["proposals"]
    adv_trace = adv_result["trace"]

    # If the advocate recorded nothing, there is nothing to rebut or judge.
    # Still return the traces so the caller can see why (e.g. turn cap hit).
    if not advocate_proposals:
        return {
            "slug": slug,
            "advocate_proposals": [],
            "rebuttals": [],
            "jury_verdicts": [],
            "traces": {
                "advocate": adv_trace,
                "rebut": {"ok": False, "error": "no advocate proposals to rebut"},
                "jury": {"ok": False, "error": "no advocate proposals to judge"},
            },
        }

    # Stage 2: rebut (advocate proposals injected as structured context).
    reb_result = _run_rebut(
        slug, articles, advocate_proposals,
        model=model, host=host, max_turns=max_turns,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
    )
    rebuttals = reb_result["rebuttals"]

    # Stage 3: jury (advocate + rebut records injected as structured context).
    jur_result = _run_jury(
        slug, articles, advocate_proposals, rebuttals,
        model=model, host=host, max_turns=max_turns,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
    )
    verdicts = jur_result["verdicts"]

    return {
        "slug": slug,
        "advocate_proposals": advocate_proposals,
        "rebuttals": rebuttals,
        "jury_verdicts": verdicts,
        "traces": {
            "advocate": adv_trace,
            "rebut": reb_result["trace"],
            "jury": jur_result["trace"],
        },
    }
