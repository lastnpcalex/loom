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

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
import httpx
import io

# Import the same Config the main server uses, so admin spawns Ollama / vLLM
# with whatever the operator has saved in config.json. Falls back gracefully
# if the import fails (admin still works, just without config-driven tuning).
try:
    from config import config as _loom_config
except Exception as _cfg_err:
    print(f"[ADMIN] Could not import Loom config ({_cfg_err}); using env-only defaults")
    _loom_config = None


def _reload_config():
    """Re-read config.json so admin always sees the operator's latest tuning,
    even if the main server wrote it after admin started."""
    if _loom_config is not None:
        try:
            _loom_config.load()
        except Exception as e:
            print(f"[ADMIN] config reload failed: {e}")


def _get_llama_port() -> int:
    """Get the llama-server port dynamically from config, falling back to 8000."""
    _reload_config()
    if _loom_config is not None and getattr(_loom_config, "llama_host", None):
        try:
            from urllib.parse import urlparse
            url = _loom_config.llama_host_url()
            parsed = urlparse(url)
            if parsed.port:
                return parsed.port
        except Exception as e:
            print(f"[ADMIN] Failed to parse llama_host port: {e}")
    # Fallback to env or 8000
    host = os.getenv("LLAMA_HOST", "http://localhost:8000")
    try:
        from urllib.parse import urlparse
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        parsed = urlparse(host)
        if parsed.port:
            return parsed.port
    except Exception:
        pass
    return 8000


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


@app.post("/tools/auth-submit-code")
async def tool_auth_submit_code(request: Request):
    """Forward the OAuth authorization code from the browser callback into the
    running `claude auth login` subprocess via stdin. Required when the host
    is accessed over Tailscale/LAN — the login redirect lands on the user's
    browser, not on the box running CC, so the subprocess can't intercept it
    automatically and is left blocking on stdin for the pasted code."""
    global _auth_login_proc
    if _auth_login_proc is None:
        return JSONResponse({"status": "error", "output": "No login in progress."})
    if _auth_login_proc.poll() is not None:
        _auth_login_proc = None
        return JSONResponse({"status": "error", "output": "Login process already exited."})
    body = await request.json()
    code = (body.get("code") or "").strip()
    if not code:
        return JSONResponse({"status": "error", "output": "No code provided."})
    try:
        _auth_login_proc.stdin.write(code + "\n")
        _auth_login_proc.stdin.flush()
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Stdin write failed: {e}"})
    return JSONResponse({"status": "ok", "output": "Code submitted — waiting for tokens…"})


@app.post("/tools/clear-vram")
async def tool_clear_vram():
    """Stop llama-server to clear all VRAM."""
    lines = []
    port = _get_llama_port()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/v1/models")
            if r.status_code == 200:
                lines.append("Llama Server is running \u2014 stopping to free VRAM...")
            else:
                lines.append("Llama Server not responding (already stopped?).")
    except Exception:
        lines.append("Llama Server not reachable (already stopped).")
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        lines.append(out or "llama-server.exe terminated.")
    except Exception as e:
        lines.append(f"taskkill failed: {e}")
    _child_procs.pop("llama", None)
    lines.append("VRAM freed.")
    return JSONResponse({"status": "ok", "output": "\n".join(lines)})


@app.post("/tools/llama-status")
async def tool_llama_status():
    """Check if llama-server is running and show loaded model info."""
    port = _get_llama_port()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/v1/models")
            if r.status_code == 200:
                data = r.json()
                models = [m["id"] for m in data.get("data", [])]
                return JSONResponse({
                    "status": "ok",
                    "output": f"Llama Server running on :{port}\nLoaded: {', '.join(models) or '(none)'}",
                })
            return JSONResponse({"status": "ok", "output": f"Llama Server responded {r.status_code}"})
    except Exception:
        return JSONResponse({"status": "ok", "output": f"Llama Server is NOT running on :{port}"})


@app.post("/tools/llama-models")
async def tool_llama_models():
    """List available .gguf model files from the models directory."""
    from pathlib import Path
    models_dir = Path(r"C:\LlamaServer\models")
    try:
        if not models_dir.exists():
            return JSONResponse({"status": "error", "output": f"Models dir not found: {models_dir}"})
        models = sorted(p.name for p in models_dir.glob("*.gguf"))
        return JSONResponse({
            "status": "ok",
            "output": "\n".join(models) if models else "(no .gguf files found)",
        })
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


def _hermes_exe_path() -> str:
    """Resolve the `hermes` CLI path the same way the main server does."""
    if _loom_config is not None:
        try:
            return _loom_config.hermes_executable()
        except Exception:
            pass
    explicit = os.environ.get("HERMES_EXE")
    if explicit:
        return explicit
    home = os.environ.get(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )
    exe_name = "hermes.exe" if os.name == "nt" else "hermes"
    cand = os.path.join(home, "hermes-agent", ".venv",
                        "Scripts" if os.name == "nt" else "bin", exe_name)
    return cand if os.path.exists(cand) else "hermes"


@app.post("/tools/hermes-status")
async def tool_hermes_status():
    """Probe whether Hermes Agent (ACP mode) is installed and reachable.

    Mirrors the comfyui-status / vLLM probes: checks the `hermes` CLI runs,
    reports the version + HERMES_HOME, and confirms the local Ollama endpoint
    (which Hermes is configured to use) is up. Does NOT do a full `hermes acp`
    JSON-RPC round-trip here — that's heavy for a status poke.
    """
    home = os.environ.get(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )
    exe = _hermes_exe_path()
    lines: list[str] = []
    available = False
    version = ""
    try:
        env = {**os.environ, "HERMES_HOME": home}
        proc = await asyncio.create_subprocess_exec(
            exe, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("`hermes --version` timed out after 15s")
        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0:
            available = True
            version = text.splitlines()[0] if text else ""
            lines.append(f"Hermes Agent is INSTALLED ({version})")
            lines.append(f"  exe: {exe}")
            lines.append(f"  HERMES_HOME: {home}")
            cfg = os.path.join(home, "config.yaml")
            lines.append(f"  config.yaml: {'present' if os.path.exists(cfg) else 'MISSING'}")
            for extra in text.splitlines()[1:]:
                lines.append(f"  {extra.strip()}")
        else:
            lines.append(f"Hermes CLI exited {proc.returncode}:")
            lines.append(text or "(no output)")
    except FileNotFoundError:
        lines.append(f"Hermes is NOT installed — `{exe}` not found.")
        lines.append("Install: irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1 | iex")
    except Exception as e:
        lines.append(f"Error probing Hermes: {e}")

    # Ollama reachability (Hermes is configured to talk to it via config.yaml).
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("http://localhost:11434/api/tags")
        if r.status_code == 200:
            n = len(r.json().get("models", []))
            lines.append(f"\nOllama: reachable on :11434 ({n} models)")
        else:
            lines.append(f"\nOllama: :11434 responded {r.status_code}")
    except Exception:
        lines.append("\nOllama: NOT reachable on :11434 — Hermes turns will fail")

    return JSONResponse({
        "status": "ok" if available else "error",
        "available": available,
        "version": version,
        "output": "\n".join(lines),
    })


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


# ── Llama Server / ComfyUI process control ────────────────────────────────────
# Tracked launches go in _child_procs under fixed keys so stop knows which
# proc to kill if we started it. Stop also taskkills by image name as a
# fallback (covers desktop-app-launched / pre-existing instances).

LLAMA_SERVER_EXE = os.getenv(
    "LLAMA_SERVER_EXE",
    r"C:\Users\exast\OneDrive\Documents\LS\bin\llama-server.exe",
)
LLAMA_MODELS_DIR = os.getenv("LLAMA_MODELS_DIR", r"C:\LlamaServer\models")
LLAMA_PORT = 11434

COMFYUI_LAUNCH_CMD = os.getenv(
    "COMFYUI_LAUNCH_CMD",
    r'"C:\Users\exast\Downloads\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\run_nvidia_gpu.bat"',
)


# ── Per-model configuration ────────────────────────────────────────────────
# models_config.json lives next to admin_server.py.
# Format: { "ModelName.gguf": { ctx_size: 150000, ngl: 999, flash_attn: true,
#             kv_quant: "q8_0", threads: 8, batch: 2048, ubatch: 1024,
#             mlock: false, extra_args: "--no-mmap" } }

# Per-model config lives in project root (same file server.py writes).
MODEL_CONFIG_PATH = Path(__file__).parent / "models_config.json"


def _load_model_configs() -> dict:
    if MODEL_CONFIG_PATH.exists():
        try:
            with open(MODEL_CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get_model_config(model_name: str) -> dict:
    """Get config for a model, auto-creating with defaults if missing."""
    import re
    cfg = _load_model_configs()
    if model_name not in cfg:
        m = re.search(r'(\d+)b', model_name, re.IGNORECASE)
        if m:
            size = int(m.group(1))
            if size <= 3: ctx_size = 200000
            elif size <= 8: ctx_size = 150000
            elif size <= 14: ctx_size = 100000
            elif size <= 27: ctx_size = 80000
            elif size <= 70: ctx_size = 40000
            else: ctx_size = 20000
        else:
            ctx_size = 150000
        cfg[model_name] = {
            "ctx_size": ctx_size,
            "ngl": 999,
            "flash_attn": True,
            "kv_quant": "none",
            "threads": None,
            "batch": None,
            "ubatch": None,
            "mlock": False,
            "mmproj": None,
            "extra_args": "",
        }
    return cfg[model_name]


def _build_llama_cmd(model_name: str | None = None) -> str:
    """Build the llama-server launch command from per-model config."""
    override = os.getenv("LLAMA_LAUNCH_CMD")
    if override:
        return override
    _reload_config()
    model = (model_name or
             (os.getenv("LLAMA_MODEL") or
              (_loom_config.llama_model if _loom_config else "Qwen3.6-27B-NVFP4.gguf"))).strip()
    exe = (_loom_config.llama_server_exe if _loom_config else LLAMA_SERVER_EXE).strip()
    models_dir = (_loom_config.llama_models_dir if _loom_config else LLAMA_MODELS_DIR).strip()
    import os.path as _osp
    model_path = model if _osp.isabs(model) else _osp.join(models_dir, model)

    # Read per-model config
    mc = _get_model_config(model)
    port = _get_llama_port()
    parts = [
        f'"{exe}"',
        f' -m "{model_path}"',
        f' --port {port}',
        f' --ctx-size {mc["ctx_size"]}',
        f' --parallel 1',
    ]
    parts.append(f' -ngl {mc["ngl"]}')
    parts.append(f' --flash-attn on' if mc["flash_attn"] else '')
    if mc.get("kv_quant") and mc["kv_quant"] != "none":
        kv_val = mc["kv_quant"].lower().replace("k", "q")
        parts.append(f' --cache-type-k {kv_val} --cache-type-v {kv_val}')
    if mc.get("threads"):
        parts.append(f' --threads {mc["threads"]}')
    if mc.get("batch"):
        parts.append(f' --batch {mc["batch"]}')
    if mc.get("ubatch"):
        parts.append(f' --ubatch {mc["ubatch"]}')
    if mc["mlock"]:
        parts.append(' --mlock')
    if mc.get("mmproj"):
        mmproj_path = mc["mmproj"] if _osp.isabs(mc["mmproj"]) else _osp.join(models_dir, mc["mmproj"])
        parts.append(f' --mmproj "{mmproj_path}"')
    if mc.get("extra_args"):
        parts.append(f' {mc["extra_args"]}')
    return ''.join(parts)



def _spawn_detached(cmd: str, cwd: str | None = None) -> subprocess.Popen:
    """Spawn a long-running command in a detached process group on Windows.
    Routes stdout/stderr to a per-service log file so crashes are debuggable."""
    _reload_config()
    env = os.environ.copy()
    log_name = None
    if "llama-server" in cmd.lower():
        log_name = "llama_admin.log"
    if "comfyui" in cmd.lower() or "comfy" in cmd.lower():
        log_name = log_name or "comfyui_admin.log"

    log_dir = Path(__file__).parent
    if log_name:
        log_path = log_dir / log_name
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        stdout_target = log_handle
        stderr_target = subprocess.STDOUT
    else:
        stdout_target = subprocess.DEVNULL
        stderr_target = subprocess.DEVNULL

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=stdout_target,
        stderr=stderr_target,
        env=env,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if log_name:
        log_handle.close()
    return proc


@app.post("/tools/llama-start")
async def tool_llama_start(model: str | None = None):
    """Launch llama-server.exe with the currently configured model.
    Pass ?model=<filename.gguf> to override the model for this run."""
    port = _get_llama_port()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/v1/models")
            if r.status_code == 200:
                return JSONResponse({"status": "ok", "output": f"Llama Server is already running on :{port}"})
    except Exception:
        pass
    _reload_config()
    cmd = _build_llama_cmd(model)
    try:
        proc = _spawn_detached(cmd)
        _child_procs["llama"] = proc
        # llama-server loads the model into VRAM before accepting requests
        for i in range(90):
            await asyncio.sleep(1)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(f"http://127.0.0.1:{port}/v1/models")
                    if r.status_code == 200:
                        return JSONResponse({
                            "status": "ok",
                            "output": f"Llama Server ready after {i+1}s (PID {proc.pid}).\nCmd: {cmd}",
                        })
            except Exception:
                continue
        return JSONResponse({
            "status": "ok",
            "output": f"Llama Server launched (PID {proc.pid}) but not responding after 90s. Large models may need more time.\nCmd: {cmd}",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Failed to launch Llama Server: {e}\nCmd: {cmd}"})


@app.post("/tools/llama-stop")
async def tool_llama_stop():
    """Terminate tracked llama-server proc and kill any remaining llama-server.exe processes."""
    lines = []
    proc = _child_procs.pop("llama", None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            lines.append(f"Terminated tracked Llama Server proc (PID {proc.pid}).")
        except Exception as e:
            lines.append(f"Failed to terminate tracked proc: {e}")
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        lines.append(out or f"taskkill exit {r.returncode}")
    except Exception as e:
        lines.append(f"taskkill failed: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "No Llama Server processes found."})


@app.post("/tools/llama-restart")
async def tool_llama_restart(model: str | None = None):
    """Stop the running llama-server (if any), wait for port to free, then
    start a fresh instance with the current config (or the provided model)."""
    await tool_llama_stop()
    await asyncio.sleep(2)  # Give Windows OS time to release the port socket
    port = _get_llama_port()
    for _ in range(8):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                r = await client.get(f"http://127.0.0.1:{port}/v1/models")
                if r.status_code != 200:
                    break
        except Exception:
            break
    return await tool_llama_start(model=model)








@app.post("/tools/comfyui-start")
async def tool_comfyui_start():
    """Launch ComfyUI if not already running."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{COMFYUI_URL}/system_stats")
            if r.status_code == 200:
                return JSONResponse({"status": "ok", "output": "ComfyUI is already running on :8188"})
    except Exception:
        pass
    try:
        proc = _spawn_detached(COMFYUI_LAUNCH_CMD)
        _child_procs["comfyui"] = proc
        # Wait briefly for it to become reachable (ComfyUI cold-starts can be slow)
        for i in range(30):
            await asyncio.sleep(1)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(f"{COMFYUI_URL}/system_stats")
                    if r.status_code == 200:
                        return JSONResponse({
                            "status": "ok",
                            "output": f"ComfyUI launched and ready after {i+1}s (PID {proc.pid}).",
                        })
            except Exception:
                continue
        return JSONResponse({
            "status": "ok",
            "output": f"ComfyUI launched (PID {proc.pid}) but not responding after 30s. Cold-start may need more time — try ComfyUI Status in a moment.",
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "output": f"Failed to launch ComfyUI: {e}\n\nCommand: {COMFYUI_LAUNCH_CMD}\nSet env COMFYUI_LAUNCH_CMD to override.",
        })


@app.post("/tools/comfyui-stop")
async def tool_comfyui_stop():
    """Kill ComfyUI: terminate tracked proc + taskkill any python.exe in the
    portable ComfyUI directory tree (matches by command-line). Falls back to
    just terminating the tracked handle if WMIC isn't available."""
    lines = []
    proc = _child_procs.pop("comfyui", None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            lines.append(f"Terminated tracked ComfyUI proc (PID {proc.pid}).")
        except Exception as e:
            lines.append(f"Failed to terminate tracked proc: {e}")
    # Best-effort: kill main.py-launched python.exe (covers portable .bat)
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' and CommandLine like '%ComfyUI%main.py%'",
             "delete"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        if "deleted successfully" in out.lower() or "instances of" in out.lower():
            lines.append("Killed ComfyUI python.exe via WMIC.")
        elif out:
            lines.append(out[:300])
    except Exception as e:
        lines.append(f"WMIC fallback failed: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "No ComfyUI processes found."})


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


def _run_powershell_cmd_sync(cmd: str) -> str:
    """Run a PowerShell command synchronously."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=8,
            cwd=str(Path(__file__).parent)
        )
        return proc.stdout or ""
    except Exception as e:
        return f"Error: {e}"


async def run_powershell_cmd_async(cmd: str) -> str:
    """Run a PowerShell command asynchronously using asyncio.to_thread."""
    return await asyncio.to_thread(_run_powershell_cmd_sync, cmd)


@app.post("/tools/system-specs")
async def tool_system_specs():
    """System specs diagnostic (CPU-Z / VRAM style)."""
    # 1. CPU Query
    cpu_info = {"Name": "Unknown CPU", "Cores": "?", "Threads": "?", "Speed": "?", "Load": "?"}
    try:
        cmd = "Get-CimInstance Win32_Processor | Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed | ConvertTo-Json"
        out = await run_powershell_cmd_async(cmd)
        if out.strip():
            data = json.loads(out)
            if isinstance(data, list):
                data = data[0] if data else {}
            cpu_info["Name"] = data.get("Name", "Unknown CPU").strip()
            cpu_info["Cores"] = data.get("NumberOfCores", "?")
            cpu_info["Threads"] = data.get("NumberOfLogicalProcessors", "?")
            cpu_info["Speed"] = data.get("MaxClockSpeed", "?")
    except Exception as e:
        cpu_info["Error"] = str(e)

    # Get CPU Load
    try:
        cmd = "Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average | Select-Object -ExpandProperty Average"
        out = await run_powershell_cmd_async(cmd)
        if out.strip():
            cpu_info["Load"] = int(out.strip())
    except Exception:
        pass

    # 2. RAM Query
    ram_info = {"TotalGB": 0, "FreeGB": 0, "UsedGB": 0, "PercentUsed": 0}
    try:
        cmd = "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory | ConvertTo-Json"
        out = await run_powershell_cmd_async(cmd)
        if out.strip():
            data = json.loads(out)
            if isinstance(data, list):
                data = data[0] if data else {}
            total_kb = data.get("TotalVisibleMemorySize", 0)
            free_kb = data.get("FreePhysicalMemory", 0)
            if total_kb:
                ram_info["TotalGB"] = total_kb / (1024 * 1024)
                ram_info["FreeGB"] = free_kb / (1024 * 1024)
                ram_info["UsedGB"] = ram_info["TotalGB"] - ram_info["FreeGB"]
                ram_info["PercentUsed"] = (ram_info["UsedGB"] / ram_info["TotalGB"]) * 100
    except Exception as e:
        ram_info["Error"] = str(e)

    # 3. GPU/VRAM Query
    gpu_list = []
    has_nvidia = False

    # Try nvidia-smi first
    try:
        def _run_nvidia_smi():
            return subprocess.run(
                ["nvidia-smi", "--query-gpu=gpu_name,memory.total,memory.used,memory.free,driver_version", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                cwd=str(Path(__file__).parent)
            )
        proc = await asyncio.to_thread(_run_nvidia_smi)
        if proc.returncode == 0 and proc.stdout.strip():
            has_nvidia = True
            for line in proc.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    name = parts[0]
                    total_mib = float(parts[1])
                    used_mib = float(parts[2])
                    free_mib = float(parts[3])
                    driver = parts[4]
                    percent_used = (used_mib / total_mib) * 100 if total_mib else 0
                    gpu_list.append({
                        "Source": "nvidia-smi",
                        "Name": name,
                        "TotalGB": total_mib / 1024,
                        "UsedGB": used_mib / 1024,
                        "FreeGB": free_mib / 1024,
                        "PercentUsed": percent_used,
                        "Driver": driver
                    })
    except Exception:
        pass

    # If nvidia-smi failed or returned nothing, try Win32_VideoController fallback
    if not has_nvidia:
        try:
            cmd = "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion | ConvertTo-Json"
            out = await run_powershell_cmd_async(cmd)
            if out.strip():
                data = json.loads(out)
                if not isinstance(data, list):
                    data = [data]
                for item in data:
                    name = item.get("Name", "Unknown GPU")
                    raw_ram = item.get("AdapterRAM", 0)
                    driver = item.get("DriverVersion", "Unknown")
                    ram_gb = raw_ram / (1024 * 1024 * 1024) if raw_ram and raw_ram > 0 else 0
                    gpu_list.append({
                        "Source": "WMI",
                        "Name": name,
                        "TotalGB": ram_gb,
                        "UsedGB": 0,
                        "FreeGB": 0,
                        "PercentUsed": 0,
                        "Driver": driver
                    })
        except Exception:
            pass

    # Build the HTML output
    html = []

    # ── CPU CARD ──
    cpu_speed_ghz = f"{cpu_info['Speed']}"
    if isinstance(cpu_info['Speed'], (int, float)):
        cpu_speed_ghz = f"{cpu_info['Speed']/1000:.2f} GHz"
    elif isinstance(cpu_info['Speed'], str) and cpu_info['Speed'].isdigit():
        cpu_speed_ghz = f"{int(cpu_info['Speed'])/1000:.2f} GHz"
        
    cpu_load_section = ""
    if cpu_info['Load'] != "?":
        cpu_load_section = f"""
        <div class="spec-bar-container">
            <div class="spec-bar-label"><span>Current Load</span><span>{cpu_info['Load']}%</span></div>
            <div class="spec-bar-track">
                <div class="spec-bar-fill fill-cpu" style="width: {cpu_info['Load']}%"></div>
            </div>
        </div>
        """
    html.append(f"""
    <div class="spec-card">
        <div class="spec-header">
            <span class="spec-icon">&#128187;</span>
            <span class="spec-title">CPU Check</span>
        </div>
        <div class="spec-body">
            <div class="spec-row"><span class="spec-lbl">Processor:</span> <span class="spec-val">{cpu_info['Name']}</span></div>
            <div class="spec-row"><span class="spec-lbl">Cores / Threads:</span> <span class="spec-val">{cpu_info['Cores']} Cores / {cpu_info['Threads']} Threads</span></div>
            <div class="spec-row"><span class="spec-lbl">Base/Max Speed:</span> <span class="spec-val">{cpu_speed_ghz}</span></div>
            {cpu_load_section}
        </div>
    </div>
    """)

    # ── RAM CARD ──
    ram_section = ""
    if ram_info["TotalGB"] > 0:
        ram_section = f"""
        <div class="spec-row"><span class="spec-lbl">Total Installed:</span> <span class="spec-val">{ram_info['TotalGB']:.2f} GB</span></div>
        <div class="spec-row"><span class="spec-lbl">Memory In Use:</span> <span class="spec-val">{ram_info['UsedGB']:.2f} GB</span></div>
        <div class="spec-row"><span class="spec-lbl">Memory Free:</span> <span class="spec-val">{ram_info['FreeGB']:.2f} GB</span></div>
        <div class="spec-bar-container">
            <div class="spec-bar-label"><span>Utilization</span><span>{ram_info['PercentUsed']:.1f}%</span></div>
            <div class="spec-bar-track">
                <div class="spec-bar-fill fill-ram" style="width: {ram_info['PercentUsed']}%"></div>
            </div>
        </div>
        """
    else:
        ram_section = f"<div class='spec-error'>Failed to load RAM details</div>"
    html.append(f"""
    <div class="spec-card">
        <div class="spec-header">
            <span class="spec-icon">&#128190;</span>
            <span class="spec-title">System Memory (RAM)</span>
        </div>
        <div class="spec-body">
            {ram_section}
        </div>
    </div>
    """)

    # ── GPU / VRAM CARDS ──
    for idx, gpu in enumerate(gpu_list):
        gpu_details = ""
        if gpu["Source"] == "nvidia-smi":
            gpu_details = f"""
            <div class="spec-row"><span class="spec-lbl">Model:</span> <span class="spec-val" style="color:#0f6; font-weight:600;">{gpu['Name']}</span></div>
            <div class="spec-row"><span class="spec-lbl">Driver Version:</span> <span class="spec-val">{gpu['Driver']}</span></div>
            <div class="spec-row"><span class="spec-lbl">Total VRAM:</span> <span class="spec-val">{gpu['TotalGB']:.2f} GB</span></div>
            <div class="spec-row"><span class="spec-lbl">Used VRAM:</span> <span class="spec-val">{gpu['UsedGB']:.2f} GB</span></div>
            <div class="spec-row"><span class="spec-lbl">Free VRAM:</span> <span class="spec-val">{gpu['FreeGB']:.2f} GB</span></div>
            <div class="spec-bar-container">
                <div class="spec-bar-label"><span>VRAM Utilization</span><span>{gpu['PercentUsed']:.1f}%</span></div>
                <div class="spec-bar-track">
                    <div class="spec-bar-fill fill-gpu" style="width: {gpu['PercentUsed']}%"></div>
                </div>
            </div>
            """
        else:
            ram_str = f"{gpu['TotalGB']:.2f} GB" if gpu['TotalGB'] > 0 else "N/A or Dynamic"
            gpu_details = f"""
            <div class="spec-row"><span class="spec-lbl">Model:</span> <span class="spec-val">{gpu['Name']}</span></div>
            <div class="spec-row"><span class="spec-lbl">Driver Version:</span> <span class="spec-val">{gpu['Driver']}</span></div>
            <div class="spec-row"><span class="spec-lbl">Reported Video RAM:</span> <span class="spec-val">{ram_str}</span></div>
            <div class="spec-note" style="font-size:10px; color:#666; margin-top:8px;">* Detailed VRAM metrics require an active NVIDIA driver and nvidia-smi tool.</div>
            """

        html.append(f"""
        <div class="spec-card">
            <div class="spec-header">
                <span class="spec-icon">&#128451;</span>
                <span class="spec-title">GPU {idx}: {gpu['Name'].split(' ')[0]}</span>
            </div>
            <div class="spec-body">
                {gpu_details}
            </div>
        </div>
        """)

    wrapped_html = f"""
    <div class="specs-grid">
        {"".join(html)}
    </div>
    """
    return JSONResponse({"status": "ok_html", "output": wrapped_html})


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
    .quick-links {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }}
    .quick-link {{ padding: 8px 14px; border: 1px solid #0ff; border-radius: 6px; color: #0ff; text-decoration: none; font-size: 13px; background: rgba(0,255,255,0.05); transition: 0.2s; }}
    .quick-link:hover {{ background: rgba(0,255,255,0.18); }}
    
    /* System Specs Styles */
    .specs-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-top: 10px; font-family: 'Segoe UI', sans-serif; }}
    .spec-card {{ background: rgba(255,255,255,0.02); border: 1px solid rgba(0,255,255,0.1); border-radius: 8px; padding: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); transition: transform 0.2s, border-color 0.2s; }}
    .spec-card:hover {{ transform: translateY(-2px); border-color: rgba(0,255,255,0.3); background: rgba(255,255,255,0.04); }}
    .spec-header {{ display: flex; align-items: center; gap: 8px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 8px; margin-bottom: 12px; }}
    .spec-icon {{ font-size: 20px; }}
    .spec-title {{ font-size: 14px; font-weight: bold; color: #0ff; text-transform: uppercase; letter-spacing: 0.5px; }}
    .spec-body {{ display: flex; flex-direction: column; gap: 6px; }}
    .spec-row {{ display: flex; justify-content: space-between; font-size: 12px; }}
    .spec-lbl {{ color: #888; }}
    .spec-val {{ color: #fff; font-weight: 500; text-align: right; }}
    .spec-bar-container {{ margin-top: 12px; }}
    .spec-bar-label {{ font-size: 11px; color: #aaa; margin-bottom: 4px; display: flex; justify-content: space-between; }}
    .spec-bar-track {{ background: rgba(255,255,255,0.08); height: 8px; border-radius: 4px; overflow: hidden; border: 1px solid rgba(255,255,255,0.05); }}
    .spec-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1); }}
    .fill-cpu {{ background: linear-gradient(90deg, #00c6ff, #0072ff); box-shadow: 0 0 8px rgba(0, 198, 255, 0.5); }}
    .fill-ram {{ background: linear-gradient(90deg, #00f2fe, #4facfe); box-shadow: 0 0 8px rgba(0, 242, 254, 0.5); }}
    .fill-gpu {{ background: linear-gradient(90deg, #f9d423, #ff4e50); box-shadow: 0 0 8px rgba(255, 78, 80, 0.5); }}
    .spec-error {{ font-size: 12px; color: #f66; padding: 10px; background: rgba(255,102,102,0.1); border-radius: 4px; text-align: center; }}
    #tool-output.html-mode {{ font-family: inherit; color: inherit; white-space: normal; background: #0d0d21; border-color: rgba(0,255,255,0.15); max-height: none; }}
</style>
</head>
<body>
<h1>Loom Admin</h1>

<div class="quick-links">
    <a href="https://localhost:3000" target="_blank" class="quick-link">&#127760; Main Loom (:3000)</a>
    <a href="http://localhost:3001" target="_blank" class="quick-link">&#129514; Test Server (:3001)</a>
    <a href="http://localhost:{_get_llama_port()}" target="_blank" class="quick-link">&#129303; Llama Server (:{_get_llama_port()})</a>
    <a href="http://localhost:8188" target="_blank" class="quick-link">&#127912; ComfyUI (:8188)</a>
</div>

<table id="instances-table">
    <tr><th>Instance</th><th>Port</th><th>Database</th><th>PID</th><th>Actions</th></tr>
    <tbody id="instances-body">{rows}</tbody>
</table>

<h2>Active Generations <span id="gens-count" style="color:#888; font-size:12px; font-weight:normal;"></span></h2>
<div id="generations-panel">
    <div id="generations-empty" style="color:#666; font-size:12px; padding:8px 0;">None tracked.</div>
    <table id="generations-table" style="display:none;">
        <tr><th>Conv</th><th>Draft</th><th>PID</th><th>Status</th><th>Mode</th><th>Started</th><th></th></tr>
        <tbody id="generations-body"></tbody>
    </table>
</div>

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
    <button class="tool-btn" onclick="runTool('llama-status')">
        <span class="icon">&#128202;</span> Llama Status
        <span class="label">Running server info</span>
    </button>
    <button class="tool-btn" onclick="runTool('llama-models')">
        <span class="icon">&#128451;</span> Model List
        <span class="label">Available .gguf files</span>
    </button>
    <button class="tool-btn" onclick="runTool('llama-start')">
        <span class="icon">&#9658;</span> Start Llama
        <span class="label">Launch llama-server</span>
    </button>
    <button class="tool-btn" onclick="confirmTool('llama-stop', 'Stop Llama Server?')">
        <span class="icon">&#9209;</span> Stop Llama
        <span class="label">Terminate llama-server</span>
    </button>
    <button class="tool-btn" onclick="runTool('llama-restart')">
        <span class="icon">&#128260;</span> Restart Llama
        <span class="label">Stop + start llama-server</span>
    </button>
    <button class="tool-btn" onclick="runTool('comfyui-status')">
        <span class="icon">&#127912;</span> ComfyUI Status
        <span class="label">Is it running?</span>
    </button>
    <button class="tool-btn" onclick="runTool('comfyui-free')">
        <span class="icon">&#128165;</span> ComfyUI Free
        <span class="label">Unload models &amp; free VRAM</span>
    </button>
    <button class="tool-btn" onclick="runTool('comfyui-start')">
        <span class="icon">&#9658;</span> Start ComfyUI
        <span class="label">Launch run_nvidia_gpu.bat</span>
    </button>
    <button class="tool-btn" onclick="confirmTool('comfyui-stop', 'Kill ComfyUI?')">
        <span class="icon">&#9209;</span> Stop ComfyUI
        <span class="label">Terminate ComfyUI process</span>
    </button>
    <button class="tool-btn" onclick="runTool('disk-usage')">
        <span class="icon">&#128190;</span> Disk Usage
        <span class="label">DB &amp; log sizes</span>
    </button>
    <button class="tool-btn" onclick="runTool('system-specs')">
        <span class="icon">&#128187;</span> System Specs
        <span class="label">CPU-Z / VRAM check</span>
    </button>
</div>
<div id="tool-output"></div>

<h2>Admin Server</h2>
<div style="margin-bottom: 12px;">
    <button onclick="adminAction('restart')" class="btn btn-cyan">Restart Admin</button>
    <button onclick="adminAction('shutdown')" class="btn btn-warn">Shutdown Admin</button>
    <span style="color:#666; font-size:12px; margin-left:10px;">(connection drops; page auto-reloads after restart)</span>
</div>
<p class="refresh-note">Live updates via AJAX (no page reload) &mdash; admin running on :{ADMIN_PORT}</p>
<div id="toast"></div>
<script>
    // Rewrite the static localhost hrefs in the quick-links to use the page's
    // own hostname, so the row of "Main Loom / Test / Ollama / ComfyUI" links
    // works over Tailscale without hardcoding the host IP.
    document.addEventListener('DOMContentLoaded', () => {{
        document.querySelectorAll('a[href*="localhost"], a[href*="127.0.0.1"]').forEach(a => {{
            try {{
                const u = new URL(a.href);
                u.hostname = location.hostname;
                a.href = u.toString();
            }} catch (e) {{}}
        }});
    }});
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
        setTimeout(refreshAll, 1500);
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

    // ── AJAX status polling (no full-page reloads) ──
    let refreshTimer = null;
    function scheduleRefresh(delay = 10000) {{
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(refreshAll, delay);
    }}

    async function refreshAll() {{
        try {{
            await Promise.all([refreshInstances(), refreshGenerations()]);
        }} catch (e) {{ /* keep ticking */ }}
        scheduleRefresh();
    }}

    async function refreshInstances() {{
        const r = await fetch('/api/status', {{cache: 'no-store'}});
        if (!r.ok) return;
        const d = await r.json();
        const body = document.getElementById('instances-body');
        if (!body) return;
        const rows = (d.instances || []).map(s => {{
            const color = s.status === 'online' ? '#0f6' : '#f44';
            const dot = '<span style="color:' + color + '; font-size:20px;">&#9679;</span>';
            const managedTag = s.managed ? ' <span class="tag">managed</span>' : '';
            const pidInfo = s.pid ? 'PID ' + s.pid : '\u2014';
            let actions = '';
            if (s.status === 'online') {{
                actions += '<button onclick="doAction(\\'' + s.name + '\\', \\'shutdown\\')" class="btn btn-warn">Shutdown</button> ';
                actions += '<button onclick="doAction(\\'' + s.name + '\\', \\'restart\\')" class="btn btn-cyan">Restart</button>';
            }} else {{
                actions += '<button onclick="doAction(\\'' + s.name + '\\', \\'start\\')" class="btn btn-green">Start</button>';
            }}
            return '<tr><td>' + dot + ' ' + s.label + managedTag + '</td>' +
                '<td>:' + s.port + '</td><td>' + s.db + '</td><td>' + pidInfo + '</td>' +
                '<td>' + actions + '</td></tr>';
        }}).join('');
        body.innerHTML = rows;
    }}

    async function refreshGenerations() {{
        let gens = [];
        try {{
            const r = await fetch('/api/generations-proxy', {{cache: 'no-store'}});
            if (r.ok) gens = await r.json();
        }} catch (e) {{ /* ignore */ }}
        const empty = document.getElementById('generations-empty');
        const table = document.getElementById('generations-table');
        const body = document.getElementById('generations-body');
        const count = document.getElementById('gens-count');
        if (!gens.length) {{
            empty.style.display = 'block';
            table.style.display = 'none';
            count.textContent = '';
            return;
        }}
        empty.style.display = 'none';
        table.style.display = 'table';
        count.textContent = '(' + gens.length + ')';
        body.innerHTML = gens.map(g => {{
            const status = g.in_memory ? 'running' : (g.pid_alive ? 'orphan' : 'dead');
            const statusColor = status === 'running' ? '#0f6' : (status === 'orphan' ? '#f90' : '#666');
            const age = g.started_at ? Math.round(Date.now()/1000 - g.started_at) + 's' : '—';
            return '<tr>' +
                '<td>' + g.conv_id + '</td>' +
                '<td>#' + g.draft_msg_id + '</td>' +
                '<td>' + (g.pid || '—') + '</td>' +
                '<td><span style="color:' + statusColor + '">' + status + '</span></td>' +
                '<td>' + (g.mode || '—') + '</td>' +
                '<td>' + age + '</td>' +
                '<td><button class="btn btn-warn" onclick="killGen(' + g.draft_msg_id + ')">Kill</button></td>' +
                '</tr>';
        }}).join('');
    }}

    async function killGen(draftId) {{
        if (!confirm('Kill generation #' + draftId + '?')) return;
        showToast('killing #' + draftId);
        try {{
            const r = await fetch('/api/generations-proxy/' + draftId + '/kill', {{method: 'POST'}});
            if (r.ok) {{ const d = await r.json(); showToast(d.status || 'killed'); }}
            else showToast('kill failed');
        }} catch (e) {{ showToast('kill failed: ' + e); }}
        setTimeout(refreshAll, 500);
    }}

    // Kick off first refresh shortly after load (server already SSRed initial state).
    scheduleRefresh(2000);

    function confirmTool(name, msg) {{
        if (confirm(msg)) runTool(name);
    }}

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
                    '<br><br><div style="margin-top:8px;">' +
                    'After authenticating, paste the code from the callback page:<br>' +
                    '<input id="auth-code-input" type="text" placeholder="paste authorization code" ' +
                    'style="width:60%; min-width:280px; padding:6px; margin-top:6px; ' +
                    'background:#111; color:#0ff; border:1px solid #0ff; font-family:monospace;">' +
                    ' <button class="btn btn-cyan" onclick="submitAuthCode()" style="margin-left:6px;">Submit</button>' +
                    '</div>' +
                    '<br><span id="login-poll" style="color:#888;">Waiting for login to complete...</span>';
                pollLoginStatus();
                const inp = document.getElementById('auth-code-input');
                if (inp) {{
                    inp.addEventListener('keydown', (e) => {{ if (e.key === 'Enter') submitAuthCode(); }});
                    inp.focus();
                }}
                return;
            }}
            if (d.status === 'ok_html') {{
                out.innerHTML = d.output;
                out.className = 'visible html-mode';
            }} else {{
                out.textContent = d.output || '(no output)';
                out.className = d.status === 'error' ? 'visible error' : 'visible';
            }}
        }} catch (e) {{
            out.textContent = 'Request failed: ' + e;
            out.className = 'visible error';
        }}
        scheduleRefresh(30000);
    }}

    async function submitAuthCode() {{
        const inp = document.getElementById('auth-code-input');
        if (!inp) return;
        const code = inp.value.trim();
        if (!code) {{ showToast('Paste the authorization code first'); return; }}
        inp.disabled = true;
        try {{
            const r = await fetch('/tools/auth-submit-code', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ code: code }}),
            }});
            const d = await r.json();
            showToast(d.output || (d.status === 'ok' ? 'submitted' : 'failed'));
            if (d.status !== 'ok') inp.disabled = false;
        }} catch (e) {{
            showToast('submit failed: ' + e);
            inp.disabled = false;
        }}
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
                scheduleRefresh(5000);
            }} else {{
                poll.style.color = '#f66';
                poll.textContent = d.output;
                scheduleRefresh(10000);
            }}
        }} catch (e) {{
            poll.textContent = 'Poll failed: ' + e;
            refreshTimer = setTimeout(() => location.reload(), 10000);
        }}
    }}

    // Terminal WebSocket — use the page's own host so this works over
    // Tailscale (or any non-localhost access) without hardcoding the IP.
    const termSocket = new WebSocket(`ws://${{location.host}}/ws/terminal`);
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


@app.get("/api/generations-proxy")
async def api_generations_proxy():
    """Proxy /api/generations from whichever Loom instance is online.
    Dashboard can't call main:3000 directly due to mixed-content (HTTPS from
    HTTP admin) and cert-prompt issues."""
    for name, info in INSTANCES.items():
        try:
            r = await _get_instance(info["port"], "/api/generations")
            if r.status_code == 200:
                return JSONResponse(r.json())
        except Exception:
            continue
    return JSONResponse([])


@app.post("/api/generations-proxy/{draft_msg_id}/kill")
async def api_generations_kill_proxy(draft_msg_id: int):
    """Proxy a kill request to the Loom instance that owns this draft."""
    for name, info in INSTANCES.items():
        try:
            r = await _post_instance(info["port"], f"/api/generations/{draft_msg_id}/kill")
            if r.status_code == 200:
                return JSONResponse(r.json())
        except Exception:
            continue
    return JSONResponse({"error": "no instance reachable"}, status_code=502)


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
        creationflags=0x08000000 | 0x00000200, # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
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
        creationflags=0x08000000 | 0x00000200, # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
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
    base_dir = Path(__file__).resolve().parent
    ssl_cert = os.getenv("LOOM_SSL_CERT", str(base_dir / "certs" / "cert.pem"))
    ssl_key = os.getenv("LOOM_SSL_KEY", str(base_dir / "certs" / "key.pem"))
    print(f"[ADMIN] Checking certs at: {ssl_cert}")
    ssl_kwargs = {}
    if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        print(f"[ADMIN] SSL enabled — cert={ssl_cert}")
        ssl_kwargs = {"ssl_certfile": ssl_cert, "ssl_keyfile": ssl_key}
    else:
        print(f"[ADMIN] SSL NOT enabled — files missing")

    print(f"[ADMIN] Starting admin server on :{ADMIN_PORT}")
    scheme = "https" if ssl_kwargs else "http"
    print(f"[ADMIN] Dashboard: {scheme}://localhost:{ADMIN_PORT}")

    uv_config = uvicorn.Config(
        app, host="0.0.0.0", port=ADMIN_PORT,
        log_level="warning",
        **ssl_kwargs
    )
    server = uvicorn.Server(uv_config)
    _server_ref.append(server)
    server.run()
