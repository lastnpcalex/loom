"""Tool definitions and execution for Local mode agent.

Provides file-system tools that Ollama models can call via the tool-calling API,
giving Local mode the ability to read, list, and write files in the working directory.
"""

import os
import json
from pathlib import Path

# Maximum file size we'll read (256KB)
MAX_READ_SIZE = 256 * 1024

# Extensions to skip in directory listings
SKIP_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".mp3", ".mp4", ".wav", ".avi", ".mkv", ".mov",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
    ".pyc", ".pyo", ".class", ".o", ".obj",
    ".whl", ".egg",
}

SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", ".svn", ".hg",
    "venv", ".venv", "env", ".env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt",
    "target", "out", "bin", "obj",
}

# Tool definitions in Ollama/OpenAI function-calling format
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file. Returns the file text. Use this to examine source code, configs, docs, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from the working directory (e.g. 'src/main.py', 'README.md')"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and subdirectories in a directory. Returns names with [dir] or [file] markers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from the working directory. Use '.' or '' for the root."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Creates parent directories as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from the working directory"
                    },
                    "content": {
                        "type": "string",
                        "description": "The full file content to write"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a text pattern across files in the working directory. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or substring to search for (case-insensitive)"
                    },
                    "glob": {
                        "type": "string",
                        "description": "Optional file glob pattern to filter (e.g. '*.py', '*.js'). Defaults to all text files."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
]


def _resolve_path(project_dir: str, rel_path: str) -> Path:
    """Resolve a relative path within the project directory, preventing traversal."""
    base = Path(project_dir).resolve()
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError(f"Path traversal blocked: {rel_path}")
    return target


def build_directory_tree(project_dir: str, max_depth: int = 3, max_entries: int = 200) -> str:
    """Build a concise directory tree string for the system prompt."""
    base = Path(project_dir).resolve()
    if not base.exists():
        return f"(directory does not exist: {project_dir})"

    lines = []
    count = 0

    def walk(path: Path, prefix: str, depth: int):
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}(permission denied)")
            return

        dirs = []
        files = []
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in (".env.example",):
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                dirs.append(entry)
            else:
                if entry.suffix.lower() in SKIP_EXTENSIONS:
                    continue
                files.append(entry)

        for d in dirs:
            if count >= max_entries:
                lines.append(f"{prefix}... (truncated)")
                return
            lines.append(f"{prefix}{d.name}/")
            count += 1
            walk(d, prefix + "  ", depth + 1)

        for f in files:
            if count >= max_entries:
                lines.append(f"{prefix}... (truncated)")
                return
            size = f.stat().st_size
            if size < 1024:
                size_str = f"{size}B"
            elif size < 1024 * 1024:
                size_str = f"{size // 1024}KB"
            else:
                size_str = f"{size // (1024*1024)}MB"
            lines.append(f"{prefix}{f.name} ({size_str})")
            count += 1

    walk(base, "", 0)
    return "\n".join(lines) if lines else "(empty directory)"


def build_system_prompt(project_dir: str) -> str:
    """Build a system prompt that gives the model awareness of the project."""
    tree = build_directory_tree(project_dir)
    return f"""You are a helpful coding assistant with access to the project at: {project_dir}

## Available tools
You have tools to interact with the file system:
- **read_file**: Read file contents
- **list_directory**: List directory contents
- **write_file**: Write/create files
- **search_files**: Search for text patterns across files

These are the ONLY tools available to you. Do NOT attempt to call any other tools (no bash, no shell, no terminal commands). Use these tools to examine the codebase before answering questions. When asked to make changes, read the relevant files first, then write the updated versions.

## Project structure
```
{tree}
```"""


def execute_tool(project_dir: str, tool_name: str, arguments: dict) -> str:
    """Execute a tool call and return the result as a string."""
    try:
        if tool_name == "read_file":
            return _exec_read_file(project_dir, arguments)
        elif tool_name == "list_directory":
            return _exec_list_directory(project_dir, arguments)
        elif tool_name == "write_file":
            return _exec_write_file(project_dir, arguments)
        elif tool_name == "search_files":
            return _exec_search_files(project_dir, arguments)
        else:
            return f"Unknown tool: {tool_name}"
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error executing {tool_name}: {e}"


def _exec_read_file(project_dir: str, args: dict) -> str:
    path = _resolve_path(project_dir, args.get("path", ""))
    if not path.exists():
        return f"File not found: {args.get('path')}"
    if not path.is_file():
        return f"Not a file: {args.get('path')}"
    if path.stat().st_size > MAX_READ_SIZE:
        return f"File too large ({path.stat().st_size} bytes, max {MAX_READ_SIZE}). Try reading a specific section."
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Failed to read file: {e}"


def _exec_list_directory(project_dir: str, args: dict) -> str:
    rel = args.get("path", ".") or "."
    path = _resolve_path(project_dir, rel)
    if not path.exists():
        return f"Directory not found: {rel}"
    if not path.is_dir():
        return f"Not a directory: {rel}"
    try:
        entries = sorted(path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for entry in entries[:100]:
            if entry.is_dir():
                lines.append(f"[dir]  {entry.name}/")
            else:
                lines.append(f"[file] {entry.name}")
        if len(entries) > 100:
            lines.append(f"... and {len(entries) - 100} more entries")
        return "\n".join(lines) if lines else "(empty directory)"
    except PermissionError:
        return f"Permission denied: {rel}"


def _exec_write_file(project_dir: str, args: dict) -> str:
    path = _resolve_path(project_dir, args.get("path", ""))
    content = args.get("content", "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Written {len(content)} bytes to {args.get('path')}"


def _exec_search_files(project_dir: str, args: dict) -> str:
    pattern = args.get("pattern", "").lower()
    glob_pattern = args.get("glob", None)
    if not pattern:
        return "No search pattern provided"

    base = Path(project_dir).resolve()
    results = []
    max_results = 50

    for path in base.rglob(glob_pattern or "*"):
        if len(results) >= max_results:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() in SKIP_EXTENSIONS:
            continue
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue
        if path.stat().st_size > MAX_READ_SIZE:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if pattern in line.lower():
                    rel = path.relative_to(base)
                    results.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                    if len(results) >= max_results:
                        break
        except Exception:
            continue

    if not results:
        return f"No matches found for '{args.get('pattern')}'"
    header = f"Found {len(results)} match(es):\n"
    return header + "\n".join(results)
