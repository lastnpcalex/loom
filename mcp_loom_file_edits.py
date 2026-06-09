"""Deprecated compatibility stub.

Codex workspace permissions are implemented in mcp_loom_workspace.py. This file
is intentionally not an MCP server; update any stale config to use
`loom-workspace` instead of `loom-file-edits`.
"""

raise RuntimeError(
    "mcp_loom_file_edits.py is deprecated; use mcp_loom_workspace.py / loom-workspace"
)
