"""MCP workspace tools that route Codex actions through Loom permissions.

Codex's native shell and edit tools may run inside a host sandbox that blocks
operations before Loom can ask the user. This server provides one explicit,
workspace-scoped permission bridge for file edits, local commands, and sensitive
reads. Every mutating or sensitive operation asks Loom first.
"""

import os
import ssl
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("loom-workspace")

_MAX_TIMEOUT_SEC = 600
_DEFAULT_OUTPUT_CHARS = 40000
_MAX_OUTPUT_CHARS = 200000


def _cfg() -> tuple[Path, str, str] | str:
    root = os.environ.get("LOOM_WORKSPACE_ROOT") or os.getcwd()
    port = os.environ.get("LOOM_PORT", "3000")
    conv_id = os.environ.get("LOOM_CONV_ID", "")
    if not conv_id:
        return "Loom conversation id missing; refusing to access workspace."
    try:
        root_path = Path(root).resolve()
    except Exception as e:
        return f"Invalid LOOM_WORKSPACE_ROOT: {e}"
    return root_path, port, conv_id


def _resolve_inside(root: Path, path: str | None) -> Path:
    candidate = Path(path or ".")
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Refusing to access outside workspace: {path}")
    return resolved


def _post_json(port: str, endpoint: str, payload: dict, timeout: float | None = None) -> dict:
    protocols = ("http", "https")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last_error = ""
    for proto in protocols:
        url = f"{proto}://127.0.0.1:{port}{endpoint}"
        try:
            with httpx.Client(verify=False, timeout=timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return response.json() if response.content else {}
        except Exception as e:
            last_error = f"{proto}: {e}"
    raise RuntimeError(last_error)


def _ask_permission(tool_name: str, tool_input: dict) -> str | None:
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    _root, port, conv_id = cfg
    tool_id = f"loom-workspace-{uuid.uuid4().hex[:12]}"
    payload = {
        "loom_conv_id": conv_id,
        "tool_name": tool_name,
        "tool_id": tool_id,
        "tool_input": tool_input,
    }
    try:
        _post_json(port, "/api/cc-tool-start", payload, timeout=3)
    except Exception:
        pass
    try:
        response = _post_json(port, "/api/cc-permission", payload, timeout=None)
    except Exception as e:
        return f"Loom permission request failed: {e}"
    if not response.get("allow"):
        return response.get("message") or "Denied by user in Loom UI"
    return None


def _limit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n\n[truncated {omitted} chars]"


def _output_limit(max_chars: int) -> int:
    try:
        return max(1000, min(int(max_chars), _MAX_OUTPUT_CHARS))
    except Exception:
        return _DEFAULT_OUTPUT_CHARS


@mcp.tool()
def write_file(file_path: str, content: str) -> str:
    """Create or replace a UTF-8 text file after Loom approval.

    file_path must be inside the current Loom workspace. Use this instead of
    shell redirection, apply_patch, or direct filesystem writes.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    root, _port, _conv_id = cfg
    try:
        target = _resolve_inside(root, file_path)
    except Exception as e:
        return f"Error: {e}"

    denied = _ask_permission("Write", {"file_path": str(target), "content": content})
    if denied:
        return f"Denied: {denied}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {target}"
    except Exception as e:
        return f"Error writing {target}: {e}"


@mcp.tool()
def replace_in_file(file_path: str, old_string: str, new_string: str) -> str:
    """Replace exactly one UTF-8 text occurrence after Loom approval."""
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    root, _port, _conv_id = cfg
    try:
        target = _resolve_inside(root, file_path)
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: {e}"
    count = text.count(old_string)
    if count != 1:
        return f"Error: expected exactly one match, found {count}"

    denied = _ask_permission(
        "Edit",
        {"file_path": str(target), "old_string": old_string, "new_string": new_string},
    )
    if denied:
        return f"Denied: {denied}"
    try:
        target.write_text(text.replace(old_string, new_string), encoding="utf-8")
        return f"Updated {target}"
    except Exception as e:
        return f"Error writing {target}: {e}"


@mcp.tool()
def apply_unified_patch(patch: str) -> str:
    """Apply a unified diff after Loom approval.

    The patch must only touch files under the current Loom workspace.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    root, _port, _conv_id = cfg
    denied = _ask_permission("apply_patch", {"command": patch})
    if denied:
        return f"Denied: {denied}"

    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".patch") as f:
            f.write(patch)
            patch_path = f.name
        try:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", patch_path],
                cwd=str(root),
                text=True,
                capture_output=True,
                timeout=30,
            )
        finally:
            Path(patch_path).unlink(missing_ok=True)
    except Exception as e:
        return f"Error applying patch: {e}"
    if proc.returncode != 0:
        return f"Patch failed:\n{proc.stderr or proc.stdout}"
    return "Patch applied"


@mcp.tool()
def run_command(command: str, cwd: str = ".", timeout_sec: int = 60, max_output_chars: int = _DEFAULT_OUTPUT_CHARS) -> str:
    """Run a local shell command after Loom approval.

    The working directory must stay inside LOOM_WORKSPACE_ROOT. Use this for
    commands that need real user approval, such as git, deploy checks, package
    managers, or commands that source local environment files.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    root, _port, _conv_id = cfg
    try:
        workdir = _resolve_inside(root, cwd)
    except Exception as e:
        return f"Error: {e}"
    if not workdir.exists() or not workdir.is_dir():
        return f"Error: cwd is not a directory: {workdir}"

    try:
        timeout = max(1, min(int(timeout_sec), _MAX_TIMEOUT_SEC))
    except Exception:
        timeout = 60
    output_limit = _output_limit(max_output_chars)

    denied = _ask_permission(
        "Bash",
        {"command": command, "cwd": str(workdir), "timeout_sec": timeout},
    )
    if denied:
        return f"Denied: {denied}"

    if sys.platform == "win32":
        argv = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
    else:
        argv = ["bash", "-lc", command]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        return (
            f"Command timed out after {timeout}s\n"
            f"stdout:\n{_limit_text(stdout, output_limit)}\n\n"
            f"stderr:\n{_limit_text(stderr, output_limit)}"
        )
    except Exception as e:
        return f"Error running command: {e}"

    stdout = _limit_text(proc.stdout or "", output_limit)
    stderr = _limit_text(proc.stderr or "", output_limit)
    return f"exit_code: {proc.returncode}\nstdout:\n{stdout}\n\nstderr:\n{stderr}"


@mcp.tool()
def read_text_file(file_path: str, max_chars: int = _DEFAULT_OUTPUT_CHARS) -> str:
    """Read a UTF-8 text file after explicit Loom approval.

    Use this only when normal read tools are blocked or the file is sensitive.
    The path must stay inside LOOM_WORKSPACE_ROOT.
    """
    cfg = _cfg()
    if isinstance(cfg, str):
        return cfg
    root, _port, _conv_id = cfg
    try:
        target = _resolve_inside(root, file_path)
    except Exception as e:
        return f"Error: {e}"
    if not target.exists() or not target.is_file():
        return f"Error: not a file: {target}"

    denied = _ask_permission("SensitiveRead", {"file_path": str(target)})
    if denied:
        return f"Denied: {denied}"

    output_limit = _output_limit(max_chars)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading {target}: {e}"
    return _limit_text(text, output_limit)


if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    mcp.run(transport="stdio")
