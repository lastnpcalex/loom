# A Shadow Loom — Roadmap

## Immediate Todo

- [ ] **Claude Code skills & slash commands** — CC slash commands (`/model`, `/compact`, installed skills) currently do nothing in Loom. These should hook into CC's functionality — either by intercepting them before they're sent as text, or by passing them through CC's CLI interface. Goal: Loom should be as close to feature-complete CC as possible.
- [ ] **Unread message queue** — a section under conversations on the home page showing freshly generated loom messages that haven't been viewed yet, labeled by conversation name. Background generations become visible without opening each conversation.
- [ ] **Bookmark scroll accuracy** — bookmarks currently land the user approximately near the bookmarked message but sometimes slightly above or below in the message stream. Fix scroll-to-message precision.

## Aspirational Goals

### OODA Harness for Agentic Tasks
Extend the OODA harness beyond RP into agentic coding and task execution. The same observe-orient-decide-act loop that improves RP quality could structure how local models approach multi-step coding tasks — reading project state, orienting on the codebase, deciding on an approach, then acting with tool calls.

### State Creation Chat
A mini-chat interface on the state card page where you describe a character in natural language and the model extracts structured state card fields from your description. The OODA harness in reverse — instead of reading states to write prose, it reads prose to write states.

### Branch-Level State Canvas
A spatial canvas view (reusing the tree canvas pan/zoom infrastructure) where state cards are laid out as draggable nodes. Visual relationships between characters, scenes, and lore. Different from the current list/sidebar view.

### Sub-Agent Loom
A loom visualization for agent-spawned sub-tasks. When CC launches subagents (via the Agent tool), show their work as branches on the tree — visible, navigable, and forkable.

### Local / Gemini CLI Subagents
Support for spawning subagents using local models or Gemini CLI alongside the main conversation. Mixed model conversations where different agents handle different tasks.

### Automatic Git-Tree Loom
Visualize git history as a loom. Branches, merges, and commits rendered as a tree with the same pan/zoom canvas. Navigate code changes the same way you navigate conversations.

### Gemini CLI Loom Options
Integration with Gemini CLI as a conversation backend (alongside Ollama and Claude Code). Same loom UI, different model provider.
