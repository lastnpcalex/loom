"""NROL-AO engine-agent package (Track A, Phases 1-3).

This package holds the engine-side tool surface and the in-process tool-call
agent harness that drives Dream (DiffusionGemma) via OpenAI-format tool calls.

It is deliberately NOT a second long-running MCP server process
(A.7): it is an importable package the operator MCP can call in-process. The
engine agent's tool calls dispatch to plain Python functions in
``mcp_servers/nrol_ao_engine/tools/``; no commits, no topic mutation, and no
posterior movement happen here. The state layer (engine repo's
``framework/pipeline.py``, ``topics/*.json``) is reached read-only via the same
``_import_from_repo`` shim ``mcp_servers/nrol_ao/server.py`` already uses.

Phase 2 added the advocate stage: reading tools (``read_topic``,
``read_indicator_schema``, ``read_recent_evidence``) plus the
``propose_advocate`` deliberation tool (RECORDS a proposal — never commits), and
the ``advocate_agent.run_advocate`` runner. Phase 3 adds the rebuttal and jury
deliberation tools (``propose_rebut``, ``submit_jury`` — RECORD verdicts, never
commit) and the ``deliberation_agent.run_deliberation`` runner, which drives
the full advocate → rebut → jury packet. See
``mcp_servers/nrol_ao/docs/ARCHITECTURE_UPDATE.md`` §0.6, Track A, and §4.1.
"""

from __future__ import annotations

__all__ = ["imports", "dream_client", "engine_agent", "advocate_agent", "deliberation_agent"]
