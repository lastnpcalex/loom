# What Does the Loom Do? — Conceptual & Operational Overview

## Core Philosophy: The Constant & The Shuttles

A **Loom** is not an LLM application or a simple wrapper around a specific model. It is a self-hosted, multi-engine orchestration layer built around a simple truth: **no single AI model or agent architecture is optimal for all tasks.**

Different engines excel at different workloads:
*   **Frontier Models** (via [claude_client.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/claude_client.py) or [codex_client.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/codex_client.py)) handle hard reasoning, repository-scale refactoring, and complex tool-use loops.
*   **Local GGUF Models** (via [llama_client.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/llama_client.py)) provide offline, always-on utility, low-cost execution, and private data handling.
*   **Hermes Agent** (via [hermes_client.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/hermes_client.py) over ACP) offers a conversational, tool-capable agent using standard protocol specifications.
*   **Weave/OODA** (via [ooda_harness.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/ooda_harness.py)) guides local models through structured reasoning loops for creative writing and roleplay.
*   **NROL-AO** (via [mcp_servers/nrol_ao/server.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/mcp_servers/nrol_ao/server.py)) acts as a highly constrained epistemic operator for structured belief updates and evidence evaluation.

The Loom provides the shared, immutable infrastructure—the **warp** of the fabric—while the LLM clients serve as interchangeable **shuttles** weaving threads through the database, visual tree interface, and unified permissions system.

---

## 1. Hosting at Home, Control from Anywhere (Tailnet Web GUI)

A major friction point with local developer agents (like Claude Code or llama.cpp) is their execution context: they run locally on your workspace, locking you to a physical terminal or local server port.

The Loom bridges this gap by decoupling **where the code runs** from **where the operator controls it**:
*   **Zero-Config Tailnet Access**: [server.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/server.py) binds to `0.0.0.0` over HTTPS, automatically detecting SSL certificates in the `certs/` directory. This allows you to expose the Loom securely on your private **Tailscale Tailnet**.
*   **On-the-Go Control**: You can run massive reasoning loops or code generation sweeps on your high-performance home workstation, but trigger and monitor them from a mobile browser or tablet while on the move.
*   **Active Persistence**: If you lose connection or close your browser tab mid-turn, the Loom's **progressive draft saving** continues the run in the background. The generation survives network disruptions and tab reloads, and you can pick up the stream when you reconnect.

---

## 2. The Permission Hook: Remote Human-in-the-Loop Security

Running autonomous agents with terminal access on your primary development machine is inherently risky. If an agent executes a malicious bash command or overwrites a critical file, the damage is immediate.

The Loom solves this through a unified, external permission gateway:
*   [cc_permission_hook.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/cc_permission_hook.py) is a modular script injected into agent processes (Claude Code, agy, Codex) as a `PreToolUse` or permission hook.
*   Instead of letting the agent execute commands natively or using simple terminal prompts, the hook halts execution, serializes the request (command line, target file path, write buffers), and sends a POST request back to the Loom server.
*   The Loom server blocks the agent's execution and pushes a real-time prompt to your active browser tab (and triggers the notification bell).
*   Whether you are at your desk or accessing the Loom via your phone on your tailnet, you review the exact diff, tool arguments, or shell command before granting permission. The decision is securely passed back to the hook to resume or abort the tool call.

---

## 3. Epistemic Governance & Complex Workflows

Loom extends beyond traditional coding and chat by integrating structured workflows:
*   **NROL-AO Facade**: The epistemic forecasting engine ([mcp_servers/nrol_ao/server.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/mcp_servers/nrol_ao/server.py)) is exposed as a narrow, typed MCP server. The agent cannot modify belief stores directly; it must propose transitions (e.g., submitting evidence, proposing matches) that route through a fail-closed commit protocol, requiring operator sign-off.
*   **Background Jobs & Cron**: You can schedule automated scans, cron checks, or code audits. The Loom handles running these scripts asynchronously, compiling digests, and logging results directly to the admin interface.
*   **External Service Control**: Through the admin panel ([admin_server.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/admin_server.py)), Loom manages the lifecycles of crucial sidecar processes—spinning up and shutting down local `llama-server.exe` instances, clearing VRAM, and managing creative pipelines (like ComfyUI) directly from the Web GUI.

---

## 4. Multi-Paradigm Interoperability

Loom translates raw message logs and CLI outputs into a single, unified database schema. This enables powerful interoperation features:
*   **Mid-Conversation Handoff**: You can start debugging a complex codebase issue with frontier Claude on a high-tier model, and then seamlessly switch the dropdown to a local model (via [llama_client.py](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/llama_client.py)) to run local test iterations or write simple boilerplates, preserving the entire tree context.
*   **Visual Tree Navigation**: Every branch, fork, and edit is captured. The interactive tree UI ([static/tree.js](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/static/tree.js)) visualizes alternate paths as nodes. You can jump between different model generations, compare their output side-by-side, and branch from any node.
*   **Interactive Canvas**: Start a canvas workspace ([static/canvas-sdk.js](file:///C:/Users/exast/OneDrive/Documents/Loom2/a-shadow-loom/static/canvas-sdk.js)) where the agent acts as an editor on a live front-end page, refreshing dynamically as edits are authorized, combining visual sandbox prototyping with conversational instruction.
