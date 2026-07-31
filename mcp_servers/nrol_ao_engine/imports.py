"""Shared import shim for the engine-agent package.

Mirrors ``mcp_servers/nrol_ao/server.py``'s ``_import_from_repo`` pattern: the
NROL-AO engine + framework live in a *separate repo* (default
``C:\\Claude-Code\\NROL-AO\\temp-repo``) and are imported at runtime by putting
that repo on ``sys.path``. Engine modules are NOT in ``a-shadow-loom``; they
cannot be edited here. The new engine-side tools (this package) are thin
wrappers that call into the engine repo's state layer through this shim — the
same pattern the operator MCP already uses, factored to a shared location.

State stays read-only for phase 1: ``fetch_article`` and the reading tools
never mutate topic JSON. When action tools (``fire_indicator`` etc.) land in
later phases, they will wrap the existing ``framework/pipeline.py`` update
functions through the *same* commit gates (Loom approval, governance) the
operator MCP already enforces — no new commit path is introduced here.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

_DEFAULT_REPO = Path(r"C:\Claude-Code\NROL-AO\temp-repo")


def repo_path() -> Path:
    """Configured engine repo root (``NROL_AO_REPO`` env or default)."""
    configured = os.environ.get("NROL_AO_REPO", "").strip()
    root = Path(configured) if configured else _DEFAULT_REPO
    return root.resolve()


def ensure_repo() -> Path:
    """Put the engine repo on ``sys.path`` and return its resolved root.

    Raises FileNotFoundError if the repo is missing or doesn't look like the
    NROL-AO engine (no ``engine.py`` / ``governor.py``). Importing the engine
    repo puts topic state on disk in reach — but nothing here writes it.
    """
    root = repo_path()
    if not root.exists():
        raise FileNotFoundError(
            f"NROL-AO engine repo not found at {root}. Set NROL_AO_REPO to the repo path."
        )
    if not (root / "engine.py").is_file() or not (root / "governor.py").is_file():
        raise FileNotFoundError(f"{root} does not look like an NROL-AO engine repo")
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
        importlib.invalidate_caches()
    return root


def import_from_repo(module_name: str) -> Any:
    """Import a module from the engine repo, asserting it really came from there.

    Verifies the imported module's file actually lives under the configured repo
    root (guards against a same-named module elsewhere on sys.path shadowing
    it). Identical semantics to ``server._import_from_repo``.
    """
    root = ensure_repo()
    module = importlib.import_module(module_name)
    module_file = Path(getattr(module, "__file__", "") or "").resolve()
    if module_file != root / f"{module_name}.py" and root not in module_file.parents:
        raise RuntimeError(
            f"Imported {module_name} from {module_file}, not configured repo {root}"
        )
    return module
