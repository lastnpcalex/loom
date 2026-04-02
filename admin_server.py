"""Lightweight admin/status server for Loom instances.

Runs on its own port (default 3002) and provides:
  - Status dashboard showing all Loom instances
  - Graceful shutdown for any instance
  - Restart capability (stop + relaunch)

Usage:
    python admin_server.py                  # port 3002
    ADMIN_PORT=3003 python admin_server.py  # custom port
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import httpx

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
    proc = _child_procs.get(name)
    if proc and proc.poll() is None:
        result["pid"] = proc.pid
        result["managed"] = True
    else:
        result["managed"] = False
        if proc:
            _child_procs.pop(name, None)

    return result


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
        pid_info = f"PID {s['pid']}" if s.get("pid") else "—"

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

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Loom Admin</title>
<style>
    body {{ font-family: 'Segoe UI', sans-serif; background: #0a0a19; color: #ddd; margin: 0; padding: 24px; }}
    h1 {{ color: #0ff; font-size: 22px; margin-bottom: 20px; }}
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
    #toast {{ position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; background: #1a1a2e; border: 1px solid #0ff; border-radius: 6px; display: none; }}
    .refresh-note {{ color: #666; font-size: 12px; margin-top: 16px; }}
</style>
</head>
<body>
<h1>Loom Admin</h1>
<table>
    <tr><th>Instance</th><th>Port</th><th>Database</th><th>PID</th><th>Actions</th></tr>
    {rows}
</table>
<p class="refresh-note">Auto-refreshes every 5s &mdash; admin running on :{ADMIN_PORT}</p>
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
        setTimeout(() => location.reload(), 1500);
    }}
    setTimeout(() => location.reload(), 5000);
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

    if name in _child_procs and _child_procs[name].poll() is None:
        return JSONResponse({"error": f"{name} already running (PID {_child_procs[name].pid})"}, status_code=409)

    info = INSTANCES[name]
    env = os.environ.copy()
    env["LOOM_PORT"] = str(info["port"])
    env["LOOM_DB"] = info["db"]

    # Determine the server.py path — for test, use worktree if available
    server_dir = Path(__file__).parent
    server_py = server_dir / "server.py"

    log_file = server_dir / "server.log"
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
    return JSONResponse({"status": f"{name} starting on :{info['port']}", "pid": proc.pid})


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

    # Step 2: wait for port release, then start
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
