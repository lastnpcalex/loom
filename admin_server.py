"""Lightweight admin/status server for Loom instances.

Runs on its own port (default 3002) and provides:
  - Status dashboard showing all Loom instances
  - Graceful shutdown for any instance
  - Restart capability (stop + relaunch)
  - Admin tools: auth refresh, VRAM cleanup, etc.

Usage:
    python admin_server.py                  # port 3002
    ADMIN_PORT=3003 python admin_server.py  # custom port
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import httpx
import io

ADMIN_PORT = int(os.getenv("ADMIN_PORT", "3002"))

# Known Loom instances to monitor
INSTANCES = {
    "main": {"port": 3000, "label": "Main Loom", "db": "loom.db"},
    "test": {"port": 3001, "label": "Test Server", "db": "loom_test.db"},
}

_server_ref: list = []
# Track child processes we've launched (for restart)
_child_procs: dict[str, subprocess.Popen] = {}

app = FastAPI(title="Loom Admin")


async def check_instance(name: str, info: dict) -> dict:
    """Probe an instance for liveness."""
    port = info["port"]
    result = {
        "name": name,
        "label": info["label"],
        "port": port,
        "db": info["db"],
        "status": "offline",
        "pid": None,
    }
    # Try HTTPS first (main server uses SSL), then HTTP
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=2.0, verify=False) as client:
                resp = await client.get(f"{scheme}://localhost:{port}/api/config")
                if resp.status_code == 200:
                    result["status"] = "online"
                    result["scheme"] = scheme
                    result["config"] = resp.json()
                    break
        except Exception:
            continue

    # Check if we have a tracked child process
    # On Windows with DETACHED_PROCESS, poll() is unreliable — cross-check with port status
    proc = _child_procs.get(name)
    if proc:
        proc.poll()  # refresh returncode
        if proc.returncode is not None:
            # Process exited — clean up
            _child_procs.pop(name, None)
            result["managed"] = False
        elif result["status"] == "offline":
            # Handle says alive but port is dead — stale handle
            _child_procs.pop(name, None)
            result["managed"] = False
        else:
            result["pid"] = proc.pid
            result["managed"] = True
    else:
        result["managed"] = False

    return result


# ---------------------------------------------------------------------------
# Admin tools — each returns {status, output} JSON
# ---------------------------------------------------------------------------

@app.post("/tools/auth-status")
async def tool_auth_status():
    """Check Claude auth status."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "respond with only: auth ok"],
            capture_output=True, text=True, timeout=15,
            cwd=str(Path(__file__).parent),
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        return JSONResponse({
            "status": "ok" if ok else "error",
            "output": output.strip() or "(no output)",
            "exit_code": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"status": "error", "output": "Timed out after 15s"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": str(e)})


# Holds the running `claude auth login` process while waiting for OAuth
_auth_login_proc: subprocess.Popen | None = None


@app.post("/tools/auth-refresh")
async def tool_auth_refresh():
    """Start the OAuth login flow — returns a clickable link."""
    global _auth_login_proc
    import re, threading

    # Kill any previous login attempt
    if _auth_login_proc and _auth_login_proc.poll() is None:
        _auth_login_proc.kill()
        _auth_login_proc = None

    # First check if auth already works
    try:
        r = subprocess.run(
            ["claude", "-p", "respond with only: ok"],
            capture_output=True, text=True, timeout=15,
            cwd=str(Path(__file__).parent),
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            return JSONResponse({
                "status": "ok",
                "output": f"Auth is already working. Claude responded: {(r.stdout or '').strip()[:100]}\n\nNo refresh needed.",
            })
    except Exception:
        pass  # Auth is broken, proceed with login

    # Start `claude auth login` and capture the URL
    proc = subprocess.Popen(
        ["claude", "auth", "login"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
    )
    _auth_login_proc = proc

    # Read output lines in a thread to avoid blocking
    captured = []
    def reader():
        for line in proc.stdout:
            captured.append(line.strip())
    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Wait up to 8s for the URL to appear
    for _ in range(16):
        await asyncio.sleep(0.5)
        if len(captured) >= 2:
            break

    # Extract the URL
    url = None
    for line in captured:
        match = re.search(r'https://claude\.com/\S+', line)
        if match:
            url = match.group(0)
            break

    if not url:
        proc.kill()
        _auth_login_proc = None
        return JSONResponse({
            "status": "error",
            "output": "Could not get login URL.\n\nOutput:\n" + "\n".join(captured),
        })

    return JSONResponse({
        "status": "login_started",
        "output": "Login flow started. Tap the link below to authenticate:\n\n"
                  "(The process is waiting — once you complete login in the browser, it will finish automatically.)",
        "url": url,
    })


@app.get("/tools/auth-login-status")
async def tool_auth_login_status():
    """Check if the running auth login process has completed."""
    global _auth_login_proc
    if _auth_login_proc is None:
        return JSONResponse({"status": "idle", "output": "No login in progress."})

    rc = _auth_login_proc.poll()
    if rc is None:
        return JSONResponse({"status": "waiting", "output": "Still waiting for you to authenticate..."})

    # Process finished
    _auth_login_proc = None
    if rc == 0:
        return JSONResponse({"status": "ok", "output": "Login successful!"})
    else:
        return JSONResponse({"status": "error", "output": f"Login failed (exit code {rc})."})


@app.post("/tools/clear-vram")
async def tool_clear_vram():
    """Unload all Ollama models from VRAM."""
    lines = []
    try:
        r = subprocess.run(
            ["ollama", "ps"],
            capture_output=True, text=True, timeout=10,
        )
        ps_output = r.stdout.strip()
        lines.append(f"Before:\n{ps_output or '(no models loaded)'}")

        # Parse loaded model names from 'ollama ps' output
        models = []
        for line in ps_output.split("\n")[1:]:  # skip header
            parts = line.split()
            if parts:
                models.append(parts[0])

        if not models:
            lines.append("\nNo models loaded in VRAM.")
        else:
            for model in models:
                try:
                    # Generate with keep_alive=0 tells Ollama to unload immediately
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            "http://127.0.0.1:11434/api/generate",
                            json={"model": model, "keep_alive": 0},
                        )
                        lines.append(f"Unloaded {model}: {resp.status_code}")
                except Exception as e:
                    lines.append(f"Failed to unload {model}: {e}")

        # Show state after
        r2 = subprocess.run(
            ["ollama", "ps"],
            capture_output=True, text=True, timeout=10,
        )
        lines.append(f"\nAfter:\n{r2.stdout.strip() or '(no models loaded)'}")

    except FileNotFoundError:
        lines.append("ollama not found on PATH")
    except Exception as e:
        lines.append(f"Error: {e}")

    return JSONResponse({"status": "ok", "output": "\n".join(lines)})


@app.post("/tools/ollama-ps")
async def tool_ollama_ps():
    """Show currently loaded Ollama models."""
    try:
        r = subprocess.run(
            ["ollama", "ps"],
            capture_output=True, text=True, timeout=10,
        )
        return JSONResponse({
            "status": "ok",
            "output": (r.stdout or r.stderr or "(no output)").strip(),
        })
    except FileNotFoundError:
        return JSONResponse({"status": "error", "output": "ollama not found on PATH"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": str(e)})


@app.post("/tools/ollama-models")
async def tool_ollama_models():
    """List available Ollama models."""
    try:
        r = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return JSONResponse({
            "status": "ok",
            "output": (r.stdout or r.stderr or "(no output)").strip(),
        })
    except FileNotFoundError:
        return JSONResponse({"status": "error", "output": "ollama not found on PATH"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": str(e)})


COMFYUI_URL = "http://127.0.0.1:8188"


@app.post("/tools/comfyui-status")
async def tool_comfyui_status():
    """Check if ComfyUI is running and show system/device info."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{COMFYUI_URL}/system_stats")
        if resp.status_code == 200:
            data = resp.json()
            lines = ["ComfyUI is RUNNING on port 8188", ""]
            sys_info = data.get("system", {})
            if sys_info:
                lines.append(f"OS: {sys_info.get('os', '?')}")
                lines.append(f"Python: {sys_info.get('python_version', '?')}")
                lines.append(f"Embedded Python: {sys_info.get('embedded_python', '?')}")
            devices = data.get("devices", [])
            for i, dev in enumerate(devices):
                lines.append(f"\nDevice {i}: {dev.get('name', '?')}")
                lines.append(f"  Type: {dev.get('type', '?')}")
                vram_total = dev.get('vram_total', 0)
                vram_free = dev.get('vram_free', 0)
                vram_used = vram_total - vram_free
                if vram_total:
                    lines.append(f"  VRAM: {vram_used / 1e9:.1f} / {vram_total / 1e9:.1f} GB used")
                torch_vram = dev.get('torch_vram_total', 0)
                torch_free = dev.get('torch_vram_free', 0)
                if torch_vram:
                    lines.append(f"  Torch VRAM: {(torch_vram - torch_free) / 1e9:.1f} / {torch_vram / 1e9:.1f} GB used")
            return JSONResponse({"status": "ok", "output": "\n".join(lines)})
        else:
            return JSONResponse({"status": "error", "output": f"ComfyUI responded with status {resp.status_code}"})
    except httpx.ConnectError:
        return JSONResponse({"status": "ok", "output": "ComfyUI is NOT running (port 8188 not responding)"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Error checking ComfyUI: {e}"})


@app.post("/tools/comfyui-free")
async def tool_comfyui_free():
    """Free ComfyUI VRAM by unloading models and freeing memory."""
    lines = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Get before stats
            try:
                before = await client.get(f"{COMFYUI_URL}/system_stats")
                if before.status_code == 200:
                    devs = before.json().get("devices", [])
                    for dev in devs:
                        vt = dev.get("vram_total", 0)
                        vf = dev.get("vram_free", 0)
                        if vt:
                            lines.append(f"Before: {(vt - vf) / 1e9:.1f} / {vt / 1e9:.1f} GB VRAM used")
            except Exception:
                pass

            # Call /free to unload models and free memory
            resp = await client.post(
                f"{COMFYUI_URL}/free",
                json={"unload_models": True, "free_memory": True},
            )
            if resp.status_code == 200:
                lines.append("Sent free request (unload_models + free_memory)")
            else:
                lines.append(f"Free request returned status {resp.status_code}")

            # Get after stats
            try:
                after = await client.get(f"{COMFYUI_URL}/system_stats")
                if after.status_code == 200:
                    devs = after.json().get("devices", [])
                    for dev in devs:
                        vt = dev.get("vram_total", 0)
                        vf = dev.get("vram_free", 0)
                        if vt:
                            lines.append(f"After:  {(vt - vf) / 1e9:.1f} / {vt / 1e9:.1f} GB VRAM used")
            except Exception:
                pass

        return JSONResponse({"status": "ok", "output": "\n".join(lines)})
    except httpx.ConnectError:
        return JSONResponse({"status": "error", "output": "ComfyUI is not running (port 8188 not responding)"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Error: {e}"})


@app.post("/tools/disk-usage")
async def tool_disk_usage():
    """Show disk usage for the Loom directory and DB files."""
    cwd = Path(__file__).parent
    lines = []
    for db in sorted(cwd.glob("*.db")):
        size_mb = db.stat().st_size / (1024 * 1024)
        lines.append(f"{db.name}: {size_mb:.1f} MB")
    for wal in sorted(cwd.glob("*.db-wal")):
        size_mb = wal.stat().st_size / (1024 * 1024)
        lines.append(f"{wal.name}: {size_mb:.1f} MB")
    for log in sorted(cwd.glob("*_server.log")):
        size_mb = log.stat().st_size / (1024 * 1024)
        lines.append(f"{log.name}: {size_mb:.1f} MB")
    return JSONResponse({
        "status": "ok",
        "output": "\n".join(lines) or "(no files found)",
    })


@app.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket):
    """Interactive terminal — launches Claude Code and streams output."""
    await websocket.accept()

    cwd = Path(__file__).parent
    proc = subprocess.Popen(
        ["claude"],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _child_procs["claude_term"] = proc

    # Read output and forward input
    try:
        while proc.poll() is None:
            # Read output
            data = proc.stdout.read(4096)
            if data:
                try:
                    await websocket.send_text(data.decode("utf-8", errors="replace"))
                except Exception:
                    break

            # Forward input from client
            try:
                msg = await websocket.receive_text()
                if proc.stdin:
                    proc.stdin.write(msg.encode("utf-8"))
                    proc.stdin.flush()
            except Exception:
                break

            await asyncio.sleep(0.01)
    except Exception as e:
        pass
    finally:
        proc.terminate()
        proc.wait()
        _child_procs.pop("claude_term", None)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    statuses = await asyncio.gather(
        *[check_instance(name, info) for name, info in INSTANCES.items()]
    )

    rows = ""
    for s in statuses:
        color = "#0f6" if s["status"] == "online" else "#f44"
        dot = f'<span style="color:{color}; font-size:20px;">&#9679;</span>'
        managed_tag = ' <span class="tag">managed</span>' if s.get("managed") else ""
        pid_info = f"PID {s['pid']}" if s.get("pid") else "\u2014"

        actions = ""
        if s["status"] == "online":
            actions += f'<button onclick="doAction(\'{s["name"]}\', \'shutdown\')" class="btn btn-warn">Shutdown</button> '
            actions += f'<button onclick="doAction(\'{s["name"]}\', \'restart\')" class="btn btn-cyan">Restart</button>'
        else:
            actions += f'<button onclick="doAction(\'{s["name"]}\', \'start\')" class="btn btn-green">Start</button>'

        rows += f"""
        <tr>
            <td>{dot} {s['label']}{managed_tag}</td>
            <td>:{s['port']}</td>
            <td>{s['db']}</td>
            <td>{pid_info}</td>
            <td>{actions}</td>
        </tr>"""

    # Check terminal status
    term_proc = _child_procs.get("claude_term")
    term_status = "online" if term_proc and term_proc.poll() is None else "offline"
    term_pid = term_proc.pid if term_proc and term_proc.poll() is None else None

    # Terminal section HTML
    if term_status == "online":
        terminal_html = f"""
<h2>Terminal</h2>
<div style="margin-bottom: 12px;">
    <button onclick="doAction('claude_term', 'shutdown')" class="btn btn-warn">Stop Terminal</button>
</div>
<div style="margin-bottom: 8px;">
    <div style="font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; padding: 10px; background: #000; border-radius: 6px; height: 200px; overflow-y: auto; white-space: pre-wrap; color: #0f6;">
        {""}
    </div>
    <textarea id="term-input" style="width: 100%; box-sizing: border-box; font-family: 'Consolas', monospace; font-size: 12px; padding: 8px; background: #111; border: 1px solid #0ff; color: #0f6; border-radius: 4px;" placeholder="Type commands here (e.g., claude auth status, /auth refresh)..."></textarea>
    <div style="font-size: 11px; color: #666; margin-top: 6px;">Commands execute in Claude Code. Press Ctrl+C in the terminal to exit.</div>
</div>
"""
    else:
        terminal_html = """
<h2>Terminal</h2>
<button onclick="doAction('claude_term', 'start')" class="btn btn-green">Start Terminal</button>
"""

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Loom Admin</title>
<style>
    body {{ font-family: 'Segoe UI', sans-serif; background: #0a0a19; color: #ddd; margin: 0; padding: 24px; }}
    h1 {{ color: #0ff; font-size: 22px; margin-bottom: 20px; }}
    h2 {{ color: #0ff; font-size: 16px; margin: 24px 0 12px 0; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; color: #888; font-size: 12px; text-transform: uppercase; padding: 8px; border-bottom: 1px solid #333; }}
    td {{ padding: 12px 8px; border-bottom: 1px solid #1a1a2e; }}
    .btn {{ padding: 6px 14px; border: 1px solid; border-radius: 4px; cursor: pointer; font-size: 13px; background: none; transition: 0.2s; }}
    .btn-warn {{ color: #f90; border-color: #f90; }}
    .btn-warn:hover {{ background: rgba(255,153,0,0.15); }}
    .btn-cyan {{ color: #0ff; border-color: #0ff; }}
    .btn-cyan:hover {{ background: rgba(0,255,255,0.15); }}
    .btn-green {{ color: #0f6; border-color: #0f6; }}
    .btn-green:hover {{ background: rgba(0,255,102,0.15); }}
    .tag {{ font-size: 10px; background: rgba(0,255,255,0.15); color: #0ff; padding: 2px 6px; border-radius: 3px; }}
    #toast {{ position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; background: #1a1a2e; border: 1px solid #0ff; border-radius: 6px; display: none; z-index: 100; }}
    .refresh-note {{ color: #666; font-size: 12px; margin-top: 16px; }}
    .tools-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; margin-bottom: 12px; }}
    .tool-btn {{ padding: 10px 14px; border: 1px solid #444; border-radius: 6px; cursor: pointer; font-size: 13px; background: rgba(255,255,255,0.03); color: #ccc; transition: 0.2s; text-align: left; }}
    .tool-btn:hover {{ background: rgba(0,255,255,0.08); border-color: #0ff; color: #fff; }}
    .tool-btn .icon {{ font-size: 18px; display: block; margin-bottom: 4px; }}
    .tool-btn .label {{ font-size: 12px; color: #888; }}
    #tool-output {{ font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; padding: 12px; background: #000; border-radius: 6px; min-height: 60px; max-height: 300px; overflow-y: auto; white-space: pre-wrap; color: #0f6; border: 1px solid #1a1a2e; display: none; }}
    #tool-output.visible {{ display: block; }}
    #tool-output.error {{ color: #f66; }}
    .spinner {{ display: inline-block; width: 14px; height: 14px; border: 2px solid #0ff; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .terminal-input {{ width: 100%; box-sizing: border-box; font-family: 'Consolas', monospace; font-size: 12px; padding: 8px; background: #111; border: 1px solid #0ff; color: #0f6; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Loom Admin</h1>
<table>
    <tr><th>Instance</th><th>Port</th><th>Database</th><th>PID</th><th>Actions</th></tr>
    {rows}
</table>
{terminal_html}
<h2>Tools</h2>
<div class="tools-grid">
    <button class="tool-btn" onclick="runTool('auth-status')">
        <span class="icon">&#128273;</span> Auth Status
        <span class="label">Check Claude login</span>
    </button>
    <button class="tool-btn" onclick="runTool('auth-refresh')">
        <span class="icon">&#128260;</span> Auth Check/Fix
        <span class="label">Test auth, show fix steps</span>
    </button>
    <button class="tool-btn" onclick="runTool('clear-vram')">
        <span class="icon">&#128165;</span> Clear VRAM
        <span class="label">Unload all models</span>
    </button>
    <button class="tool-btn" onclick="runTool('ollama-ps')">
        <span class="icon">&#128202;</span> Ollama PS
        <span class="label">Loaded models</span>
    </button>
    <button class="tool-btn" onclick="runTool('ollama-models')">
        <span class="icon">&#128451;</span> Model List
        <span class="label">Available models</span>
    </button>
    <button class="tool-btn" onclick="runTool('comfyui-status')">
        <span class="icon">&#127912;</span> ComfyUI Status
        <span class="label">Is it running?</span>
    </button>
    <button class="tool-btn" onclick="runTool('comfyui-free')">
        <span class="icon">&#128165;</span> ComfyUI Free
        <span class="label">Unload models &amp; free VRAM</span>
    </button>
    <button class="tool-btn" onclick="runTool('disk-usage')">
        <span class="icon">&#128190;</span> Disk Usage
        <span class="label">DB &amp; log sizes</span>
    </button>
</div>
<div id="tool-output"></div>

<h2>Admin Server</h2>
<div style="margin-bottom: 12px;">
    <button onclick="adminAction('restart')" class="btn btn-cyan">Restart Admin</button>
    <button onclick="adminAction('shutdown')" class="btn btn-warn">Shutdown Admin</button>
    <span style="color:#666; font-size:12px; margin-left:10px;">(connection drops; page auto-reloads after restart)</span>
</div>
<p class="refresh-note">Auto-refreshes every 10s &mdash; admin running on :{ADMIN_PORT}</p>
<div id="toast"></div>
<script>
    function showToast(msg) {{
        const t = document.getElementById('toast');
        t.textContent = msg;
        t.style.display = 'block';
        setTimeout(() => t.style.display = 'none', 3000);
    }}
    async function doAction(name, action) {{
        showToast(action + 'ing ' + name + '...');
        const r = await fetch('/action/' + name + '/' + action, {{method: 'POST'}});
        const d = await r.json();
        showToast(d.status || d.error || 'done');
        setTimeout(() => location.reload(), 2000);
    }}

    async function adminAction(action) {{
        showToast('admin ' + action + '...');
        const url = action === 'shutdown' ? '/shutdown' : '/admin/restart';
        try {{
            const r = await fetch(url, {{method: 'POST'}});
            const d = await r.json();
            showToast(d.status || 'done');
        }} catch (e) {{ /* connection drops — expected */ }}
        if (action === 'restart') {{
            // Poll until admin comes back, then reload
            showToast('admin restarting — waiting for :' + {ADMIN_PORT} + '...');
            const start = Date.now();
            const poll = async () => {{
                try {{
                    const p = await fetch('/api/status', {{cache: 'no-store'}});
                    if (p.ok) {{ location.reload(); return; }}
                }} catch (e) {{}}
                if (Date.now() - start < 20000) setTimeout(poll, 1000);
                else showToast('admin did not come back — check logs');
            }};
            setTimeout(poll, 2500);
        }}
    }}

    let refreshTimer = setTimeout(() => location.reload(), 10000);

    async function runTool(name) {{
        clearTimeout(refreshTimer);
        const out = document.getElementById('tool-output');
        out.className = 'visible';
        out.innerHTML = '<span class="spinner"></span> Running ' + name + '...';
        try {{
            const r = await fetch('/tools/' + name, {{method: 'POST'}});
            const d = await r.json();
            if (d.status === 'login_started' && d.url) {{
                out.innerHTML = d.output.replace(/\\n/g, '<br>') +
                    '<br><br><a href="' + d.url + '" target="_blank" style="color:#0ff; font-size:14px; word-break:break-all;">' + d.url + '</a>' +
                    '<br><br><span id="login-poll" style="color:#888;">Waiting for login to complete...</span>';
                pollLoginStatus();
                return;
            }}
            out.textContent = d.output || '(no output)';
            out.className = d.status === 'error' ? 'visible error' : 'visible';
        }} catch (e) {{
            out.textContent = 'Request failed: ' + e;
            out.className = 'visible error';
        }}
        refreshTimer = setTimeout(() => location.reload(), 30000);
    }}

    async function pollLoginStatus() {{
        const poll = document.getElementById('login-poll');
        if (!poll) return;
        try {{
            const r = await fetch('/tools/auth-login-status');
            const d = await r.json();
            if (d.status === 'waiting') {{
                poll.innerHTML = '<span class="spinner"></span> ' + d.output;
                setTimeout(pollLoginStatus, 3000);
            }} else if (d.status === 'ok') {{
                poll.style.color = '#0f6';
                poll.textContent = d.output;
                refreshTimer = setTimeout(() => location.reload(), 5000);
            }} else {{
                poll.style.color = '#f66';
                poll.textContent = d.output;
                refreshTimer = setTimeout(() => location.reload(), 10000);
            }}
        }} catch (e) {{
            poll.textContent = 'Poll failed: ' + e;
            refreshTimer = setTimeout(() => location.reload(), 10000);
        }}
    }}

    // Terminal WebSocket
    const termSocket = new WebSocket(`ws://localhost:{{ ADMIN_PORT }}/ws/terminal`);
    termSocket.onopen = () => {{
        document.getElementById('term-input')?.focus();
    }};
    termSocket.onmessage = (event) => {{
        const output = document.querySelector('.terminal-output');
        if (output) {{
            output.textContent = output.textContent + event.data;
            output.scrollTop = output.scrollHeight;
        }}
    }};
    termSocket.onerror = () => {{
        // Terminal may not have started yet
    }};
    // Enter key handler
    document.getElementById('term-input')?.addEventListener('keydown', (e) => {{
        if (e.key === 'Enter') {{
            const input = document.getElementById('term-input');
            const cmd = input.value.trim();
            if (cmd && termSocket.readyState === WebSocket.OPEN) {{
                termSocket.send(cmd);
                input.value = '';
            }}
        }}
    }});
</script>
</body>
</html>"""


@app.get("/api/status")
async def api_status():
    statuses = await asyncio.gather(
        *[check_instance(name, info) for name, info in INSTANCES.items()]
    )
    return JSONResponse({"instances": statuses, "admin_port": ADMIN_PORT})


async def _post_instance(port: int, path: str) -> httpx.Response:
    """POST to an instance, trying HTTPS then HTTP."""
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                return await client.post(f"{scheme}://localhost:{port}{path}")
        except Exception:
            continue
    raise ConnectionError(f"Cannot reach localhost:{port}")


async def _get_instance(port: int, path: str) -> httpx.Response:
    """GET from an instance, trying HTTPS then HTTP."""
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=2.0, verify=False) as client:
                return await client.get(f"{scheme}://localhost:{port}{path}")
        except Exception:
            continue
    raise ConnectionError(f"Cannot reach localhost:{port}")


@app.post("/action/{name}/shutdown")
async def action_shutdown(name: str):
    if name not in INSTANCES:
        return JSONResponse({"error": f"Unknown instance: {name}"}, status_code=404)
    port = INSTANCES[name]["port"]
    try:
        resp = await _post_instance(port, "/shutdown")
        return JSONResponse({"status": f"{name} shutting down", "response": resp.json()})
    except Exception as e:
        return JSONResponse({"error": f"Could not reach {name} on :{port}: {e}"}, status_code=502)


@app.post("/action/{name}/start")
async def action_start(name: str):
    if name not in INSTANCES:
        return JSONResponse({"error": f"Unknown instance: {name}"}, status_code=404)

    info = INSTANCES[name]
    port = info["port"]

    # Clean up stale Popen handles (Windows DETACHED_PROCESS makes poll() unreliable)
    proc = _child_procs.get(name)
    if proc:
        try:
            proc.poll()
        except Exception:
            pass
        if proc.returncode is not None:
            _child_procs.pop(name, None)

    # Wait for port to be available (old process may still be releasing it)
    for attempt in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break  # Port is free
        if attempt == 0:
            # First check failed — port in use. If we have a stale handle, kill it
            if name in _child_procs:
                try:
                    _child_procs[name].kill()
                except Exception:
                    pass
                _child_procs.pop(name, None)
        await asyncio.sleep(1)
    else:
        return JSONResponse(
            {"error": f"Port {port} still in use after 10s — old process may be stuck"},
            status_code=409,
        )

    env = os.environ.copy()
    env["LOOM_PORT"] = str(port)
    env["LOOM_DB"] = info["db"]

    # Determine the server.py path — for test, use worktree if available
    server_dir = Path(__file__).parent
    server_py = server_dir / "server.py"

    log_file = server_dir / f"{name}_server.log"
    log_handle = open(log_file, "a")
    proc = subprocess.Popen(
        [sys.executable, str(server_py)],
        env=env,
        cwd=str(server_dir),
        stdout=log_handle,
        stderr=log_handle,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _child_procs[name] = proc

    # Verify the process didn't crash immediately
    await asyncio.sleep(2)
    if proc.poll() is not None:
        log_handle.close()
        return JSONResponse(
            {"error": f"{name} crashed on startup (exit code {proc.returncode}). Check {log_file.name}"},
            status_code=500,
        )

    return JSONResponse({"status": f"{name} starting on :{port}", "pid": proc.pid})


@app.post("/action/{name}/restart")
async def action_restart(name: str):
    if name not in INSTANCES:
        return JSONResponse({"error": f"Unknown instance: {name}"}, status_code=404)

    port = INSTANCES[name]["port"]

    # Step 1: graceful shutdown
    try:
        await _post_instance(port, "/shutdown")
        # Wait for it to die
        for _ in range(10):
            await asyncio.sleep(0.5)
            try:
                await _get_instance(port, "/api/config")
            except Exception:
                break  # It's down
    except Exception:
        pass  # Already down

    # Step 2: clear stale Popen handle — on Windows, detached processes
    # leave zombie handles that make poll() return None forever
    _child_procs.pop(name, None)

    # Step 3: wait for port release, then start
    await asyncio.sleep(2)
    return await action_start(name)


@app.post("/shutdown")
async def admin_shutdown():
    """Shut down the admin server itself."""
    if _server_ref:
        _server_ref[0].should_exit = True
        return JSONResponse({"status": "admin shutting down"})
    os.kill(os.getpid(), signal.SIGINT)
    return JSONResponse({"status": "admin shutting down (signal)"})


@app.post("/admin/restart")
async def admin_restart():
    """Respawn the admin server. Client must reconnect after ~2s."""
    admin_dir = Path(__file__).parent
    child_log = admin_dir / "admin_server.log"
    # Detached child must have stdout/stderr redirected — inheriting the
    # parent's (about-to-close) console handles makes uvicorn's logger crash.
    spawn_code = (
        "import time, subprocess, sys;"
        "time.sleep(2);"
        f"log = open(r'{child_log}', 'a');"
        "flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0);"
        f"subprocess.Popen([sys.executable, r'{__file__}'], cwd=r'{admin_dir}', "
        "creationflags=flags, stdout=log, stderr=log)"
    )
    subprocess.Popen(
        [sys.executable, "-c", spawn_code],
        env=os.environ.copy(),
        cwd=str(admin_dir),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    async def _exit_soon():
        await asyncio.sleep(0.5)
        if _server_ref:
            _server_ref[0].should_exit = True
        else:
            os.kill(os.getpid(), signal.SIGINT)

    asyncio.create_task(_exit_soon())
    return JSONResponse({"status": f"admin restarting on :{ADMIN_PORT}"})


if __name__ == "__main__":
    print(f"[ADMIN] Starting admin server on :{ADMIN_PORT}")
    print(f"[ADMIN] Dashboard: http://localhost:{ADMIN_PORT}")

    uv_config = uvicorn.Config(
        app, host="0.0.0.0", port=ADMIN_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(uv_config)
    _server_ref.append(server)
    server.run()
