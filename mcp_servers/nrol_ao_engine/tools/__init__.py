"""Engine-side tool surface for the NROL-AO engine agent (Track A).

Each tool module is a thin wrapper. Phase 1 shipped ``fetch_article``
(read-only). Phase 2 added the reading tools (``read_topic``,
``read_indicator_schema``, ``read_recent_evidence``) and the advocate
deliberation tool (``propose_advocate`` — RECORDS a proposal, never commits).
Phase 3 adds the rebuttal and jury deliberation tools (``propose_rebut``,
``submit_jury`` — RECORD verdicts, never commit). Later phases add action
tools (``fire_indicator`` etc.); the action tools will wrap the existing
``framework/pipeline.py`` update functions through the *same* commit gates the
operator MCP enforces — no new commit path is introduced here.

Tools expose:
  - a ``SCHEMA`` dict (OpenAI tool spec) the agent registers with Dream
  - a ``call(**kwargs)`` function the agent dispatches to
"""

from __future__ import annotations

from . import advocate, fetch, jury, read, rebut

# Registry of available tools: name -> {"schema": <OpenAI tool spec>,
# "fn": <callable>}. engine_agent.py builds this into the tools payload.
TOOLS: dict[str, dict] = {
    "fetch_article": {"schema": fetch.SCHEMA, "fn": fetch.fetch_article},
    "read_topic": {"schema": read.READ_TOPIC_SCHEMA, "fn": read.read_topic},
    "read_indicator_schema": {
        "schema": read.READ_INDICATOR_SCHEMA_SCHEMA,
        "fn": read.read_indicator_schema,
    },
    "read_recent_evidence": {
        "schema": read.READ_RECENT_EVIDENCE_SCHEMA,
        "fn": read.read_recent_evidence,
    },
    "propose_advocate": {"schema": advocate.SCHEMA, "fn": advocate.propose_advocate},
    "propose_rebut": {"schema": rebut.SCHEMA, "fn": rebut.propose_rebut},
    "submit_jury": {"schema": jury.SCHEMA, "fn": jury.submit_jury},
}

__all__ = ["advocate", "fetch", "jury", "read", "rebut", "TOOLS"]
