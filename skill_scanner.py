"""Scan for Claude Code skills, commands, and modules.

Three sources of slash commands:
1. CC built-in CLI commands — hardcoded, always available
2. CC pluggable skills — discovered via `claude skills --output-format json`
3. User skills — .claude/skills/<name>/SKILL.md in project directories
"""

import os
import re
from pathlib import Path


# ── Built-in CC CLI commands (not discoverable via `claude skills`) ──────────
# These are built into the CC binary. We categorize them:
#   "headless"  = works via -p (translatable to NL prompt)
#   "meta"      = Loom handles natively (not sent to CC)
#   "cli-only"  = only makes sense in interactive CC terminal
#
# cli-only commands are excluded from the UI.

BUILTIN_COMMANDS = [
    # ── Git & code review ──
    {"name": "commit",          "command": "/commit",          "description": "Create a git commit with a well-crafted message", "mode": "headless",
     "prompt_template": "Create a git commit. Review all staged and unstaged changes, draft a concise commit message that focuses on the 'why', and commit. Follow the repository's commit conventions."},
    {"name": "review-pr",       "command": "/review-pr",       "description": "Review a GitHub pull request and provide feedback", "mode": "headless",
     "prompt_template": "Review the pull request {args}. Analyze the changes, check for bugs, suggest improvements, and provide a summary."},
    {"name": "pr-comments",     "command": "/pr-comments",     "description": "View and address open PR review comments", "mode": "headless",
     "prompt_template": "Look at the open PR review comments for this repository. Summarize them and address any actionable feedback. {args}"},
    {"name": "security-review", "command": "/security-review", "description": "Audit code for security vulnerabilities (OWASP, injection, etc.)", "mode": "headless",
     "prompt_template": "Perform a security review of the codebase. Check for OWASP top 10 vulnerabilities, injection risks, hardcoded secrets, insecure dependencies, and other security issues. {args}"},

    # ── Code quality ──
    {"name": "simplify",        "command": "/simplify",        "description": "Review changed code for reuse, quality, and efficiency", "mode": "headless",
     "prompt_template": "Review the recently changed code for opportunities to simplify, improve reuse, quality, and efficiency. Fix any issues you find."},
    {"name": "debug",           "command": "/debug",           "description": "Debug a failing test, error, or unexpected behavior", "mode": "headless",
     "prompt_template": "Debug the following issue: {args}. Investigate the root cause, check logs, reproduce if possible, and fix it."},

    # ── Project setup & config ──
    {"name": "init",            "command": "/init",            "description": "Initialize a project with CLAUDE.md and conventions", "mode": "headless",
     "prompt_template": "Initialize this project for Claude Code. Create or update CLAUDE.md with project conventions, structure, build/test commands, and any relevant context. {args}"},
    {"name": "update-config",   "command": "/update-config",   "description": "Configure Claude Code settings, hooks, and behaviors", "mode": "headless",
     "prompt_template": "Update the Claude Code configuration. {args}. Modify settings.json hooks or automated behaviors as needed."},
    {"name": "hooks",           "command": "/hooks",           "description": "View or manage CC hooks configuration", "mode": "headless",
     "prompt_template": "Show the current Claude Code hooks configuration. List all configured hooks, their triggers, and what they do. {args}"},

    # ── Agents & skills ──
    {"name": "agents",          "command": "/agents",          "description": "Create, list, or manage custom sub-agent definitions", "mode": "headless",
     "prompt_template": "Manage sub-agent definitions. List existing agents in .claude/agents/, or create/edit agent markdown files as needed. {args}"},
    {"name": "skills",          "command": "/skills",          "description": "List available skills and slash commands", "mode": "meta",
     "prompt_template": None},

    # ── Scheduling & automation ──
    {"name": "loop",            "command": "/loop",            "description": "Run a prompt or slash command on a recurring interval", "mode": "headless",
     "prompt_template": "Set up a recurring task that runs every {args}. Use a loop or scheduled approach to repeatedly execute the specified action at the given interval."},
    {"name": "schedule",        "command": "/schedule",        "description": "Create or manage scheduled remote agents (cron triggers)", "mode": "headless",
     "prompt_template": "Manage scheduled agents/triggers. {args}"},
    {"name": "batch",           "command": "/batch",           "description": "Run a prompt against multiple files or inputs in parallel", "mode": "headless",
     "prompt_template": "Run the following operation in batch across the specified files or inputs: {args}"},

    # ── Context & memory ──
    {"name": "context",         "command": "/context",         "description": "View or manage conversation context and included files", "mode": "headless",
     "prompt_template": "Show what files and context are currently included in this conversation. {args}"},
    {"name": "memory",          "command": "/memory",          "description": "View or edit CLAUDE.md project memory", "mode": "headless",
     "prompt_template": "Show or update the project memory (CLAUDE.md). {args}"},
    {"name": "plan",            "command": "/plan",            "description": "Create or review an implementation plan for a task", "mode": "headless",
     "prompt_template": "Create a detailed implementation plan for: {args}. Break it down into steps, identify files to change, and note any risks."},
    {"name": "tasks",           "command": "/tasks",           "description": "View or manage the current task list / todo items", "mode": "headless",
     "prompt_template": "Show the current task list and their status. {args}"},

    # ── API & SDK ──
    {"name": "claude-api",      "command": "/claude-api",      "description": "Help building apps with the Claude API or Anthropic SDK", "mode": "headless",
     "prompt_template": "Help build or integrate with the Claude API / Anthropic SDK. {args}"},

    # ── Info & status ──
    {"name": "status",          "command": "/status",          "description": "Show generation status, active tasks, and session info", "mode": "meta",
     "prompt_template": None},
    {"name": "stats",           "command": "/stats",           "description": "Show token usage, costs, and session statistics", "mode": "meta",
     "prompt_template": None},
    {"name": "usage",           "command": "/usage",           "description": "Show API usage and rate limit information", "mode": "meta",
     "prompt_template": None},
    {"name": "permissions",     "command": "/permissions",     "description": "View or manage tool permissions (allow/deny rules)", "mode": "meta",
     "prompt_template": None},
    {"name": "export",          "command": "/export",          "description": "Export conversation as JSON or markdown", "mode": "meta",
     "prompt_template": None},

    # ── Settings & modes ──
    {"name": "settings",        "command": "/settings",        "description": "Open Claude Code settings", "mode": "meta",
     "prompt_template": None},
    {"name": "fast",            "command": "/fast",            "description": "Toggle fast mode (same model, faster output)", "mode": "meta",
     "prompt_template": None},
    {"name": "passes",          "command": "/passes",          "description": "Set number of review passes for code changes", "mode": "meta",
     "prompt_template": None},
    {"name": "privacy",         "command": "/privacy",         "description": "View or change privacy and data sharing settings", "mode": "meta",
     "prompt_template": None},

    # ── Plugin & extension management ──
    {"name": "mcp",             "command": "/mcp",             "description": "Manage MCP (Model Context Protocol) server connections", "mode": "headless",
     "prompt_template": "Manage MCP server connections. List, add, remove, or configure MCP servers. {args}"},
    {"name": "install-github",  "command": "/install-github",  "description": "Install a skill or plugin from a GitHub repository", "mode": "headless",
     "prompt_template": "Install the skill or plugin from GitHub: {args}"},

    # ── CLI-only (excluded from Loom UI) ──
    {"name": "help",            "command": "/help",            "description": "Show help and available commands", "mode": "cli-only"},
    {"name": "terminal-help",   "command": "/terminal-help",   "description": "Help with terminal and shell usage", "mode": "cli-only"},
    {"name": "keybindings-help","command": "/keybindings-help", "description": "Customize keyboard shortcuts", "mode": "cli-only"},
    {"name": "vim",             "command": "/vim",             "description": "Toggle vim keybindings in CC terminal", "mode": "cli-only"},
    {"name": "voice",           "command": "/voice",           "description": "Toggle voice input mode", "mode": "cli-only"},
    {"name": "ide",             "command": "/ide",             "description": "Open file in IDE", "mode": "cli-only"},
    {"name": "mobile",          "command": "/mobile",          "description": "Generate QR code for mobile access", "mode": "cli-only"},
    {"name": "rename",          "command": "/rename",          "description": "Rename the current CC session", "mode": "cli-only"},
    {"name": "feedback",        "command": "/feedback",        "description": "Send feedback to Anthropic", "mode": "cli-only"},
    {"name": "release-notes",   "command": "/release-notes",   "description": "Show CC release notes", "mode": "cli-only"},
    {"name": "stickers",        "command": "/stickers",        "description": "Claude Code sticker pack", "mode": "cli-only"},
    {"name": "reload-plugins",  "command": "/reload-plugins",  "description": "Reload installed plugins", "mode": "cli-only"},
    {"name": "plugin",          "command": "/plugin",          "description": "Manage CC plugins", "mode": "cli-only"},
]

# Legacy alias for imports
BUILTIN_SKILLS = [c for c in BUILTIN_COMMANDS if c.get("mode") == "headless"]


def scan_skills_dir(project_dir: str) -> list[dict]:
    """Scan .claude/skills/ in a project directory for custom skill definitions.

    Returns a list of skill dicts with: id, name, command, description, prompt_template, source_path.
    """
    skills_dir = Path(project_dir) / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []

    found = []
    for skill_path in skills_dir.iterdir():
        if not skill_path.is_dir():
            continue
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError):
            continue

        # Parse frontmatter and body
        name = skill_path.name
        description = ""
        prompt_template = ""

        # Try to extract description from frontmatter or first paragraph
        lines = content.strip().split("\n")
        if lines and lines[0].strip() == "---":
            # Has YAML frontmatter
            end_idx = None
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    end_idx = i
                    break
            if end_idx:
                frontmatter = "\n".join(lines[1:end_idx])
                body = "\n".join(lines[end_idx + 1:]).strip()
                # Extract description from frontmatter
                desc_match = re.search(r'^description:\s*(.+)$', frontmatter, re.MULTILINE)
                if desc_match:
                    description = desc_match.group(1).strip().strip('"\'')
                # The body IS the prompt template
                prompt_template = body
        else:
            # No frontmatter — first line is description, rest is template
            description = lines[0].strip().lstrip("# ")
            prompt_template = content

        found.append({
            "id": f"skill:custom:{name}",
            "name": name,
            "command": f"/{name}",
            "description": description or f"Custom skill: {name}",
            "prompt_template": prompt_template,
            "source_path": str(skill_md),
        })

    return found


_cc_skills_cache: list[dict] | None = None


def discover_cc_skills() -> list[dict]:
    """Run `claude skills --output-format json` to discover pluggable CC skills.

    Parses the markdown table from the result. Returns only the *discovered*
    pluggable skills (these get merged with BUILTIN_COMMANDS in get_all_skills).
    """
    global _cc_skills_cache
    if _cc_skills_cache is not None:
        return _cc_skills_cache

    import subprocess
    import json

    discovered = []
    try:
        result = subprocess.run(
            ["claude", "skills", "--output-format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            table_text = data.get("result", "")

            for match in re.finditer(r'\|\s*`(/[\w-]+)`\s*\|\s*(.+?)\s*\|', table_text):
                command = match.group(1)
                description = match.group(2).strip()
                name = command.lstrip("/")

                # Use our hardcoded prompt_template if we have one
                hardcoded = next((c for c in BUILTIN_COMMANDS if c["name"] == name), None)
                prompt_template = (hardcoded or {}).get("prompt_template") or f"Run the {name} skill. {{args}}"

                discovered.append({
                    "name": name,
                    "command": command,
                    "description": description,
                    "prompt_template": prompt_template,
                })

            print(f"[SKILLS] Discovered {len(discovered)} pluggable skills from CC: {[s['command'] for s in discovered]}")
        else:
            print(f"[SKILLS] `claude skills` exited {result.returncode}")

    except FileNotFoundError:
        print("[SKILLS] `claude` not on PATH")
    except Exception as e:
        print(f"[SKILLS] Discovery failed: {e}")

    _cc_skills_cache = discovered
    return _cc_skills_cache


def invalidate_skills_cache():
    """Clear the cached CC skills list (call after config changes)."""
    global _cc_skills_cache
    _cc_skills_cache = None


def get_all_skills(project_dir: str = None) -> list[dict]:
    """Get all available commands/skills from three sources:

    1. Built-in CC commands (headless + meta) — always present
    2. Discovered pluggable skills from `claude skills` — merged, deduped
    3. User skills from .claude/skills/ — project-specific

    Each entry gets: id, name, command, description, prompt_template,
    source ('system'|'user'), mode ('headless'|'meta').
    CLI-only commands are excluded.
    """
    seen = set()
    skills = []

    # 1) Discovered pluggable skills from CC (highest priority descriptions)
    for s in discover_cc_skills():
        if s["name"] in seen:
            continue
        seen.add(s["name"])
        skills.append({
            "id": f"skill:{s['name']}",
            "name": s["name"],
            "command": s["command"],
            "description": s["description"],
            "prompt_template": s["prompt_template"],
            "source": "system",
            "mode": "headless",
        })

    # 2) Built-in CC commands (headless + meta, skip cli-only)
    for cmd in BUILTIN_COMMANDS:
        if cmd["name"] in seen or cmd.get("mode") == "cli-only":
            continue
        seen.add(cmd["name"])
        skills.append({
            "id": f"cmd:{cmd['name']}",
            "name": cmd["name"],
            "command": cmd["command"],
            "description": cmd["description"],
            "prompt_template": cmd.get("prompt_template"),
            "source": "system",
            "mode": cmd.get("mode", "headless"),
        })

    # 3) User skills from project directory
    if project_dir:
        for s in scan_skills_dir(project_dir):
            s["source"] = "user"
            s["mode"] = "headless"
            skills.append(s)

    return skills
