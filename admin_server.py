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
import importlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# Monkey-patch asyncio ProactorEventLoop to suppress Windows-specific ConnectionResetError
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _patched_call_connection_lost(self, exc):
            try:
                _orig_call_connection_lost(self, exc)
            except (ConnectionResetError, OSError, ConnectionAbortedError):
                pass

        _ProactorBasePipeTransport._call_connection_lost = _patched_call_connection_lost
    except Exception:
        pass


from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn
import httpx
import io

# Import the same Config the main server uses, so admin spawns Llama Server
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


def _load_initial_main_db():
    try:
        config_path = Path(__file__).parent / "config.json"
        if config_path.is_file():
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "db_path" in data and data["db_path"]:
                    INSTANCES["main"]["db"] = data["db_path"]
    except Exception:
        pass


_load_initial_main_db()

_server_ref: list = []
# Track child processes we've launched (for restart)
_child_procs: dict[str, subprocess.Popen] = {}
NROL_AO_REPO = Path(os.environ.get("NROL_AO_REPO", r"C:\Claude-Code\NROL-AO\temp-repo"))
NROL_AO_PORT = int(os.environ.get("ALPHA_OMEGA_PORT", "8098"))
NROL_SCAN_WORKSPACE = Path(
    os.environ.get(
        "NROL_SCAN_WORKSPACE",
        str(Path(__file__).parent / "workspaces" / "nrol_ao"),
    )
)

app = FastAPI(title="Loom Admin")

# The main Loom UI (https://host:3000) calls admin endpoints straight from the
# browser (e.g. Settings -> Apply & Restart llama-server). Without CORS headers
# the POST still executes but the browser can't read the response, so the UI
# reported "error contacting admin" for actions that actually succeeded.
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _llama_models_dir() -> Path:
    _reload_config()
    configured = (_loom_config.llama_models_dir or "").strip() if _loom_config else ""
    return Path(configured or LLAMA_MODELS_DIR)


@app.post("/tools/llama-models")
async def tool_llama_models():
    """List available .gguf model files from the models directory."""
    models_dir = _llama_models_dir()
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


@app.get("/api/llama-models")
async def api_llama_models():
    """Structured model list for the dashboard's switch-model control."""
    models_dir = _llama_models_dir()
    models = []
    if models_dir.exists():
        models = sorted(
            p.name for p in models_dir.glob("*.gguf")
            if not p.name.lower().startswith("mmproj")  # vision projectors aren't standalone models
        )
    configured = ""
    if _loom_config is not None:
        configured = (_loom_config.llama_model or "").strip()
    loaded = []
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{_get_llama_port()}/v1/models")
            if r.status_code == 200:
                loaded = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        pass
    return JSONResponse({"models": models, "configured": configured, "loaded": loaded})



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

    Mirrors the comfyui-status probe: checks the `hermes` CLI runs,
    reports the version + HERMES_HOME, and confirms the local model endpoint
    is up. Does NOT do a full `hermes acp` JSON-RPC round-trip here — that's
    heavy for a status poke.
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

    # Llama Server reachability (probes the endpoint Hermes/ACM agents use).
    try:
        llama_host = (_loom_config.llama_host_url() if _loom_config else "http://localhost:8000").rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{llama_host}/v1/models")
        if r.status_code == 200:
            models_data = r.json().get("data", [])
            n = len(models_data)
            lines.append(f"\nLlama Server: reachable on {llama_host} ({n} models)")
        else:
            lines.append(f"\nLlama Server: {llama_host} responded {r.status_code}")
    except Exception:
        llama_host_display = (_loom_config.llama_host_url() if _loom_config else "http://localhost:8000")
        lines.append(f"\nLlama Server: NOT reachable on {llama_host_display}")

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
    "llama-server",
)
LLAMA_MODELS_DIR = os.getenv("LLAMA_MODELS_DIR", r"C:\LlamaServer\models")
LLAMA_PORT = 11434

COMFYUI_LAUNCH_CMD = os.getenv(
    "COMFYUI_LAUNCH_CMD",
    "py -3.12 main.py --listen --use-pytorch-cross-attention",
)
COMFYUI_LAUNCH_CWD = os.getenv(
    "COMFYUI_LAUNCH_CWD",
    r"C:\ComfyUI2\ComfyUI",
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


def _resolve_llama_chat_template_file() -> str:
    """Return a launchable chat template path, or empty string to disable.

    The default is a repo-pinned Qwen 3.6 template. Operators can set
    LLAMA_CHAT_TEMPLATE_FILE or config.json's llama_chat_template_file to an
    absolute/relative path, or to an empty string to fall back to the GGUF
    embedded template.
    """
    raw = (
        getattr(_loom_config, "llama_chat_template_file", "")
        if _loom_config else
        os.getenv("LLAMA_CHAT_TEMPLATE_FILE", "")
    )
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).parent / p
    return str(p)


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
    extra_args = str(mc.get("extra_args") or "")
    chat_template_file = _resolve_llama_chat_template_file()
    has_template_override = "--chat-template" in extra_args or "--chat-template-file" in extra_args
    if chat_template_file and not has_template_override:
        if Path(chat_template_file).is_file():
            parts.append(f' --jinja --chat-template-file "{chat_template_file}"')
        else:
            print(f"[ADMIN] llama_chat_template_file not found; using embedded template: {chat_template_file}")
    if mc.get("extra_args"):
        parts.append(f' {extra_args}')
    return ''.join(parts)



def _spawn_detached(cmd: str, cwd: str | None = None, log_name: str | None = None) -> subprocess.Popen:
    """Spawn a long-running command in a detached process group on Windows.
    Routes stdout/stderr to a per-service log file so crashes are debuggable."""
    _reload_config()
    env = os.environ.copy()
    if log_name is None:
        if "llama-server" in cmd.lower():
            log_name = "llama_admin.log"
        elif "comfyui" in cmd.lower() or "comfy" in cmd.lower():
            log_name = "comfyui_admin.log"

    log_dir = Path(__file__).parent
    if log_name:
        log_path = log_dir / log_name
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        stdout_target = log_handle
        stderr_target = subprocess.STDOUT
    else:
        stdout_target = subprocess.DEVNULL
        stderr_target = subprocess.DEVNULL

    # DETACHED_PROCESS breaks stdout/stderr handle inheritance on Windows —
    # skip it when we have a log file so crash output actually lands there.
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if not log_name:
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=stdout_target,
        stderr=stderr_target,
        env=env,
        creationflags=flags,
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


@app.post("/tools/llama-unload")
async def tool_llama_unload():
    """Unload llama weights from VRAM by stopping llama-server.

    llama-server loads the GGUF at process startup, so the reliable unload path
    is to terminate the process. This endpoint exists as a clearer dashboard
    action for temporarily handing the GPU to ComfyUI.
    """
    response = await tool_llama_stop()
    payload = json.loads(response.body.decode("utf-8"))
    output = (payload.get("output") or "").strip()
    lines = ["Llama model unloaded by stopping llama-server."]
    if output:
        lines.append("")
        lines.append(output)
    return JSONResponse({"status": payload.get("status", "ok"), "output": "\n".join(lines)})


@app.post("/tools/llama-reload")
async def tool_llama_reload(model: str | None = None):
    """Reload llama-server with the selected model, or the configured default."""
    return await tool_llama_start(model=model)


# ── Dream Hermes (DiffusionGemma GPU orchestrator sidecar) ────────────────────
# Runs the nuspy OpenAI adapter (agent.openai_server) on the diffusion-capable
# llama.cpp fork. The sidecar JIT-loads the NVFP4 GGUF into VRAM on first request.
# Unload = kill the process, which releases BOTH VRAM and the process working set
# (system RAM). idle_timeout auto-unloads so the GPU/RAM is free between sessions.

DREAM_HOST = "http://127.0.0.1:8787"
# Track last activity for idle-timeout auto-unload.
_dream_last_activity: float = 0.0
_dream_idle_task_started: bool = False


def _dream_port() -> int:
    """Parse the dream port from config (default 8787)."""
    try:
        host = _loom_config.dream_host if _loom_config else os.getenv("DREAM_HOST", DREAM_HOST)
        if ":" in host and host.rsplit(":", 1)[-1].isdigit():
            return int(host.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        pass
    return 8787


def _dream_cwd() -> str:
    return (_loom_config.dream_cwd if _loom_config else os.getenv("DREAM_CWD", "")).strip()


def _dream_cmd() -> str:
    """Build the nuspy openai_server launch command."""
    py = sys.executable or "python"
    cwd = _dream_cwd()
    port = _dream_port()
    # agent.openai_server resolves ROOT from its own __file__, so cwd just needs
    # to be the nuspy repo root (where config.json + models/ live).
    return f'cd /d "{cwd}" && "{py}" -m agent.openai_server --host 127.0.0.1 --port {port}'


async def _dream_probe(timeout: float = 2.0) -> dict | None:
    """GET /health on the dream sidecar. Returns the JSON or None if down."""
    port = _dream_port()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"http://127.0.0.1:{port}/health")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


def _dream_proc_mem(pid: int | None) -> tuple[int, int]:
    """Return (working_set_mb, ram_cached_mb) for the dream sidecar process tree.
    The nuspy adapter spawns the visual-server as a CHILD process — the 17GB GGUF
    is mmap'd into the child's address space, not the adapter's. So we walk the
    whole process tree and sum RSS (working set) + the .gguf mmap footprint
    (standby cache) across adapter + child.

    If the tracked PID is missing (admin restarted while sidecar kept running),
    fall back to finding the process by command-line match."""
    import psutil
    ws_mb = 0
    cached_mb = 0
    procs: list = []
    if pid:
        try:
            parent = psutil.Process(pid)
            procs.append(parent)
            try:
                procs.extend(parent.children(recursive=True))
            except Exception:
                pass
        except Exception:
            pid = None  # fall through to the discover path
    if not procs:
        # Fallback: find the nuspy adapter + visual-server by cmdline signature.
        # This runs when the admin restarted but the sidecar survived (tracked
        # PID is stale). Without it the RAM numbers silently read as 0.
        try:
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cl = " ".join(p.info.get("cmdline") or [])
                    if "agent.openai_server" in cl or "llama-diffusion-gemma-visual-server" in (p.info.get("name") or ""):
                        procs.append(psutil.Process(p.info["pid"]))
                except Exception:
                    pass
        except Exception:
            pass
    for p in procs:
        try:
            mem = p.memory_info()
            ws_mb += int(mem.rss / 1048576)
        except Exception:
            pass
        try:
            maps = p.memory_maps(grouped=False)
            mapped = sum(m.rss for m in maps if getattr(m, "path", "").endswith(".gguf"))
            cached_mb += int(mapped / 1048576)
        except Exception:
            pass
    return (ws_mb, cached_mb)


@app.post("/tools/dream-start")
async def tool_dream_start():
    """Launch the nuspy DiffusionGemma OpenAI adapter (JIT loads model on first request)."""
    global _dream_last_activity
    port = _dream_port()
    # Already up?
    h = await _dream_probe()
    if h is not None:
        _dream_last_activity = time.time()
        return JSONResponse({"status": "ok", "output": f"Dream sidecar already running on :{port} (loaded: {h.get('loaded_model') or 'none'})."})
    cwd = _dream_cwd()
    if not cwd or not Path(cwd).is_dir():
        return JSONResponse({"status": "error", "output": f"dream_cwd not found: {cwd!r}. Set it in config.json."})
    cmd = _dream_cmd()
    try:
        proc = _spawn_detached(cmd, cwd=cwd, log_name="dream_admin.log")
        _child_procs["dream"] = proc
        _dream_last_activity = time.time()
        _ensure_dream_idle_watcher()
        # The adapter itself comes up fast (JIT load is deferred to first request).
        for i in range(30):
            await asyncio.sleep(1)
            if await _dream_probe() is not None:
                return JSONResponse({"status": "ok", "output": f"Dream sidecar ready on :{port} (PID {proc.pid}). Model JIT-loads on first request.\nCmd: {cmd}"})
        return JSONResponse({"status": "ok", "output": f"Dream sidecar launched (PID {proc.pid}) but adapter not responding after 30s.\nCmd: {cmd}"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Failed to launch Dream sidecar: {e}\nCmd: {cmd}"})


@app.post("/tools/dream-stop")
async def tool_dream_stop():
    """Terminate the dream sidecar process. Releases VRAM AND the process working
    set (system RAM). This is the real 'unload' — the nuspy /admin/unload only
    frees VRAM, leaving the 17GB GGUF cached in standby RAM."""
    lines = []
    proc = _child_procs.pop("dream", None)
    pid = proc.pid if proc and proc.poll() is None else None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            lines.append(f"Terminated dream sidecar (PID {proc.pid}).")
        except Exception as e:
            lines.append(f"Failed to terminate: {e}")
    # Also taskkill any stragglers by the python module pattern (best-effort).
    # We can't taskkill by image (would hit all python), so rely on the tracked
    # proc + the child visual-server process. Kill the visual-server too.
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "llama-diffusion-gemma-visual-server.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        if out:
            lines.append(out)
    except Exception as e:
        lines.append(f"taskkill visual-server: {e}")
    lines.append("VRAM + working set released.")
    return JSONResponse({"status": "ok", "output": "\n".join(lines)})


@app.post("/tools/dream-unload")
async def tool_dream_unload():
    """Unload Dream: stop the sidecar (frees VRAM + process RAM), then purge the
    GGUF from the OS standby cache so the 17GB is actually reclaimable."""
    resp = await tool_dream_stop()
    # Flush standby RAM so the mmap'd GGUF pages don't linger.
    flushed = _purge_standby_ram()
    payload = json.loads(resp.body.decode("utf-8"))
    out = (payload.get("output") or "").strip()
    extra = f"\nStandby RAM purge: {flushed}" if flushed else ""
    return JSONResponse({"status": payload.get("status", "ok"), "output": out + extra})


@app.post("/tools/dream-flush-ram")
async def tool_dream_flush_ram():
    """Purge OS standby file cache so the mmap'd GGUF pages are released back to
    free RAM, without stopping the sidecar. Useful when the model is still loaded
    in VRAM but you want the file-cache footprint gone."""
    flushed = _purge_standby_ram()
    if flushed:
        return JSONResponse({"status": "ok", "output": flushed})
    return JSONResponse({"status": "ok", "output": "Standby purge not available on this platform (Windows only, requires admin token). Working set left unchanged."})


def _purge_standby_ram() -> str:
    """Best-effort standby-RAM purge on Windows. Returns a status string.

    The reliable mechanism is the NT API NtSetSystemInformation with
    SystemMemoryListInformation (EmptyStandbyList = 4), but it requires the
    SeIncreaseQuotaPrivilege (admin token). We try it via ctypes; if it fails we
    fall back to a large-allocation churn that displaces standby pages, and
    report honestly which path ran."""
    if os.name != "nt":
        return ""
    # Try the NT API path first (admin token).
    nt_status = "not attempted"
    try:
        import ctypes
        ntdll = ctypes.WinDLL("ntdll")
        # NtSetSystemInformation(SystemMemoryListInformation=80, &op, sizeof(op))
        # op = MemoryListCommand (4 = EmptyStandbyList)
        SYS_MEM_LIST_INFO = 80
        EMPTY_STANDBY = 4
        ntdll.NtSetSystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        ntdll.NtSetSystemInformation.restype = ctypes.c_long
        op = ctypes.c_ulong(EMPTY_STANDBY)
        status = ntdll.NtSetSystemInformation(SYS_MEM_LIST_INFO, ctypes.byref(op), ctypes.sizeof(op))
        if status == 0:
            return "Emptied standby list via NT API (admin)."
        # Non-zero status (e.g. 0xC0000061 STATUS_PRIVILEGE_NOT_HELD) — fall
        # through to the churn fallback instead of returning a dead-end message.
        nt_status = status & 0xFFFFFFFF
    except Exception as e:
        nt_status = f"exception: {e}"
    # Allocation-churn fallback: alloc + touch + free a large chunk repeatedly.
    # Forces the OS to evict standby pages to satisfy the working set. Slower but
    # no admin token needed. This is the path that actually runs on Win11 Home.
    try:
        import ctypes
        chunk = ctypes.create_string_buffer(256 * 1024 * 1024)  # 256MB
        for _ in range(8):
            # Touch every page (4096B) to force it resident.
            for off in range(0, len(chunk), 4096):
                chunk[off] = b"\x01"
        del chunk
        return f"Displaced standby pages via allocation churn (no admin token; NT status was {nt_status}). ~2GB cycled."
    except Exception as e2:
        return f"Standby purge failed: NT status {nt_status}; churn error ({e2})."


def _dream_resolve_pid() -> int | None:
    """Find the dream sidecar PID: tracked proc first, else discover by cmdline.
    The admin tracks the PID when it launches the sidecar, but if the admin
    restarts while the sidecar survives, the tracked PID is gone — so we fall
    back to scanning for the nuspy adapter / visual-server process."""
    proc = _child_procs.get("dream")
    if proc and proc.poll() is None:
        return proc.pid
    import psutil
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cl = " ".join(p.info.get("cmdline") or [])
                if "agent.openai_server" in cl:
                    return p.info["pid"]
            except Exception:
                pass
    except Exception:
        pass
    return None


@app.get("/api/dream-models")
@app.post("/api/dream-models")
async def api_dream_models():
    """Structured status for the Loaded Models admin card: loaded model, available
    models, VRAM used, and the sidecar process's working-set + cached-RAM footprint."""
    global _dream_last_activity
    h = await _dream_probe()
    pid = _dream_resolve_pid()
    ws_mb, cached_mb = _dream_proc_mem(pid)
    # VRAM via nvidia-smi (process-specific if we can resolve it, else total).
    vram_used_mb = _gpu_vram_for_pid(pid) if pid else _gpu_total_vram_used_mb()
    loaded = (h or {}).get("loaded_model")
    if h is not None:
        _dream_last_activity = time.time()  # sidecar is up and responsive
    idle_secs = int(time.time() - _dream_last_activity) if _dream_last_activity else 0
    return {
        "running": h is not None,
        "loaded_model": loaded,
        "available": (h or {}).get("available", []),
        "maxtok": (h or {}).get("maxtok", 0),
        "pid": pid,
        "vram_used_mb": vram_used_mb,
        "ram_working_set_mb": ws_mb,
        "ram_cached_mb": cached_mb,
        "idle_secs": idle_secs,
        "idle_timeout_min": (_loom_config.dream_idle_timeout_min if _loom_config else 10),
        "host": (_loom_config.dream_host if _loom_config else DREAM_HOST),
    }


def _gpu_total_vram_used_mb() -> int:
    """Total VRAM in use across all GPUs (nvidia-smi). 0 if unavailable."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0:
            return sum(int(x.strip()) for x in r.stdout.splitlines() if x.strip().isdigit())
    except Exception:
        pass
    return 0


def _gpu_vram_for_pid(pid: int) -> int:
    """VRAM used by a specific PID (nvidia-smi pmon or compute-apps). Best-effort."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) == pid:
                    # used_memory is in MiB already
                    return int(parts[1])
    except Exception:
        pass
    return _gpu_total_vram_used_mb()


def _ensure_dream_idle_watcher():
    """Start the idle-timeout auto-unload background task once."""
    global _dream_idle_task_started
    if _dream_idle_task_started:
        return
    _dream_idle_task_started = True

    async def _watcher():
        global _dream_last_activity
        while True:
            await asyncio.sleep(60)
            if not _child_procs.get("dream"):
                continue
            timeout_min = (_loom_config.dream_idle_timeout_min if _loom_config else 10)
            if timeout_min <= 0:
                continue
            # Only auto-unload if the sidecar is up AND idle past the timeout.
            h = await _dream_probe(timeout=1.5)
            if h is None:
                continue  # sidecar down — nothing to unload
            if _dream_last_activity and (time.time() - _dream_last_activity) > timeout_min * 60:
                # Idle past timeout — unload to free VRAM + RAM.
                await tool_dream_unload()

    try:
        asyncio.get_event_loop().create_task(_watcher())
    except RuntimeError:
        pass  # no running loop yet


@app.post("/tools/dream-status")
async def tool_dream_status():
    """Human-readable Dream sidecar status for the admin card output pane."""
    h = await _dream_probe()
    port = _dream_port()
    if h is None:
        return JSONResponse({"status": "ok", "output": f"Dream sidecar NOT running on :{port}."})
    pid = _dream_resolve_pid()
    ws_mb, cached_mb = _dream_proc_mem(pid)
    vram_mb = _gpu_vram_for_pid(pid) if pid else _gpu_total_vram_used_mb()
    idle_secs = int(time.time() - _dream_last_activity) if _dream_last_activity else 0
    lines = [
        f"Dream sidecar running on :{port} (PID {pid or 'unknown'}).",
        f"Loaded model: {h.get('loaded_model') or 'none (JIT — loads on first request)'}",
        f"Available: {', '.join(h.get('available', [])) or 'none'}",
        f"maxtok (ctx): {h.get('maxtok', 0)}",
        f"VRAM used: {vram_mb} MB",
        f"RAM working set: {ws_mb} MB",
        f"RAM cached (GGUF mmap): {cached_mb} MB",
        f"Idle: {idle_secs}s (auto-unload after {(_loom_config.dream_idle_timeout_min if _loom_config else 10)} min)",
    ]
    return JSONResponse({"status": "ok", "output": "\n".join(lines)})


async def _probe_nrol_dashboard() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://127.0.0.1:{NROL_AO_PORT}/topics")
        if r.status_code == 200:
            topics = r.json()
            return True, f"NROL-AO dashboard running on :{NROL_AO_PORT} ({len(topics)} topics)"
        return False, f"NROL-AO dashboard responded HTTP {r.status_code} on :{NROL_AO_PORT}"
    except Exception as e:
        return False, f"NROL-AO dashboard is NOT running on :{NROL_AO_PORT}: {e}"


def _nrol_activity_path() -> Path:
    return Path(os.environ.get("NROL_AO_ACTIVITY_DIR", str(NROL_AO_REPO / "loom" / "mcp_activity")))


def _ensure_nrol_scan_workspace() -> Path:
    NROL_SCAN_WORKSPACE.mkdir(parents=True, exist_ok=True)
    (NROL_SCAN_WORKSPACE / "canvas").mkdir(parents=True, exist_ok=True)
    (NROL_SCAN_WORKSPACE / "canvas" / "triggers").mkdir(parents=True, exist_ok=True)
    return NROL_SCAN_WORKSPACE


def _nrol_env() -> dict:
    env = os.environ.copy()
    env["NROL_AO_REPO"] = str(NROL_AO_REPO)
    env["ALPHA_OMEGA_PORT"] = str(NROL_AO_PORT)
    env["NROL_AO_ACTIVITY_DIR"] = str(_nrol_activity_path())
    if _loom_config is not None:
        try:
            env.setdefault("NROL_AO_LLAMA_HOST", _loom_config.llama_host_url())
            if getattr(_loom_config, "llama_model", ""):
                env.setdefault("NROL_AO_LLAMA_MODEL", _loom_config.llama_model)
            if getattr(_loom_config, "llama_chat_template_file", ""):
                env.setdefault("NROL_AO_LLAMA_CHAT_TEMPLATE_FILE", _loom_config.llama_chat_template_file)
        except Exception:
            pass
    return env


def _nrol_mcp_server_config() -> dict:
    env = _nrol_env()
    env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    return {
        "nrol-ao": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "mcp_servers.nrol_ao.server"],
            "env": {
                key: value
                for key, value in env.items()
                if key.startswith("NROL_AO_")
                or key in {"ALPHA_OMEGA_PORT", "LOOM_PORT", "LOOM_CONV_ID", "PYTHONPATH"}
            },
        }
    }


def _call_nrol_mcp_tool(name: str, *args, **kwargs) -> dict:
    env = _nrol_env()
    keys = (
        "NROL_AO_REPO", "ALPHA_OMEGA_PORT", "NROL_AO_ACTIVITY_DIR",
        "NROL_AO_LLAMA_HOST", "NROL_AO_LLAMA_MODEL",
        "NROL_AO_LLAMA_CHAT_TEMPLATE_FILE",
    )
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            if key in env:
                os.environ[key] = env[key]
        nrol_mcp = importlib.import_module("mcp_servers.nrol_ao.server")
        raw = getattr(nrol_mcp, name)(*args, **kwargs)
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@app.post("/tools/nrol-status")
async def tool_nrol_status():
    """Check NROL-AO repo, dashboard, MCP bridge, activity, and llama reachability."""
    lines = [f"NROL-AO repo: {NROL_AO_REPO}"]
    lines.append(f"  exists: {NROL_AO_REPO.exists()}")
    lines.append(f"  engine.py: {(NROL_AO_REPO / 'engine.py').exists()}")
    lines.append(f"  server.py: {(NROL_AO_REPO / 'server.py').exists()}")
    workspace = _ensure_nrol_scan_workspace()
    lines.append(f"\nScan workspace: {workspace}")
    lines.append(f"  CLAUDE.md: {(workspace / 'CLAUDE.md').exists()}")
    lines.append(f"  canvas/index.html: {(workspace / 'canvas' / 'index.html').exists()}")

    mcp_server = Path(__file__).parent / "mcp_servers" / "nrol_ao" / "server.py"
    lines.append(f"\nMCP bridge: {mcp_server}")
    lines.append(f"  exists: {mcp_server.exists()}")
    lines.append("  transport: stdio, on-demand per Claude Code session")
    lines.append(f"  command: {sys.executable} {Path(__file__).parent / 'nrol_ao_mcp_server.py'}")

    up, dash_msg = await _probe_nrol_dashboard()
    lines.append(f"\nDashboard: {dash_msg}")
    lines.append(f"  URL: http://localhost:{NROL_AO_PORT}")
    proc = _child_procs.get("nrol_dashboard")
    if proc:
        lines.append(f"  tracked PID: {proc.pid}, poll={proc.poll()}")

    act_dir = _nrol_activity_path()
    snap = act_dir / "snapshot.json"
    lines.append(f"\nActivity snapshot: {snap}")
    lines.append(f"  exists: {snap.exists()}")
    if snap.exists():
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
            lines.append(f"  active jobs: {data.get('active', 0)}")
            lines.append(f"  jobs recorded: {len(data.get('jobs', []) or [])}")
            lines.append(f"  updated_at: {data.get('updated_at')}")
        except Exception as e:
            lines.append(f"  read error: {e}")

    llama_port = _get_llama_port()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://127.0.0.1:{llama_port}/v1/models")
        if r.status_code == 200:
            models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            lines.append(f"\nLlama Server: online on :{llama_port}")
            lines.append(f"  loaded: {', '.join(models) or '(none)'}")
        else:
            lines.append(f"\nLlama Server: HTTP {r.status_code} on :{llama_port}")
    except Exception as e:
        lines.append(f"\nLlama Server: not reachable on :{llama_port}: {e}")

    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        has_nrol = "nrol-ao" in output
        lines.append(f"\nClaude MCP registration: {'nrol-ao present' if has_nrol else 'nrol-ao not found'}")
        if output:
            lines.append(output[-2000:])
    except Exception as e:
        lines.append(f"\nClaude MCP registration check failed: {e}")

    return JSONResponse({"status": "ok" if up else "error", "output": "\n".join(lines)})


@app.post("/tools/nrol-topic-status")
async def tool_nrol_topic_status():
    """Show active NROL-AO topic freshness and governance queues."""
    try:
        data = _call_nrol_mcp_tool("topic_status")
        if data.get("error"):
            return JSONResponse({"status": "error", "output": data["error"]})
        topics = data.get("topics", []) or []
        lines = [f"Active topics: {len(topics)}", ""]
        for topic in topics:
            stale = "STALE" if topic.get("scanStale") else "fresh"
            age = topic.get("scanAgeHours")
            age_text = "never scanned" if age is None else f"{age}h old"
            parked_debt = topic.get("parkedReviewDebt") or {}
            parked_due = parked_debt.get("dueCount", 0)
            parked_total = parked_debt.get("parkedTotal", topic.get("flaggedForIndicatorReview", 0))
            queues = (
                f"work: parked_due={parked_due}, "
                f"parked_archive={parked_total}, "
                f"schema_gaps={topic.get('flaggedSchemaGaps', 0)}, "
                f"extensions={topic.get('proposedSchemaExtensions', 0)}"
            )
            lines.append(
                f"[{stale}] {topic.get('slug')} | {topic.get('classification')} | "
                f"{topic.get('governanceHealth') or 'unknown'} | {age_text}"
            )
            lines.append(f"  {topic.get('title')}")
            lines.append(f"  updated: {topic.get('lastUpdated') or '(unknown)'} | {queues}")
        return JSONResponse({"status": "ok", "output": "\n".join(lines).rstrip()})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"NROL topic status failed: {e}"})


@app.post("/tools/nrol-mcp-activity")
async def tool_nrol_mcp_activity():
    """Show recent NROL-AO MCP activity without launching operational work."""
    try:
        data = _call_nrol_mcp_tool("list_activity", limit=8)
        if data.get("error"):
            return JSONResponse({"status": "error", "output": data["error"]})
        jobs = data.get("jobs", []) or []
        lines = [
            "NROL MCP activity",
            f"updated: {data.get('updated_at') or '(unknown)'} | active: {data.get('active', 0)}",
            "",
        ]
        if not jobs:
            lines.append("No MCP jobs recorded.")
        for job in jobs:
            title = " | ".join(str(x) for x in [job.get("task"), job.get("slug")] if x)
            lines.append(f"{job.get('status', 'unknown')}: {title or job.get('job_id', 'job')}")
            if job.get("model"):
                lines.append(f"  model: {job['model']}")
            if job.get("summary"):
                lines.append(f"  summary: {json.dumps(job['summary'], ensure_ascii=False)[:500]}")
            if job.get("error"):
                lines.append(f"  error: {job['error']}")
        return JSONResponse({"status": "ok", "output": "\n".join(lines).rstrip()})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"NROL MCP activity failed: {e}"})


@app.post("/tools/nrol-dashboard-start")
async def tool_nrol_dashboard_start():
    """Launch the NROL-AO dashboard HTTP server."""
    up, msg = await _probe_nrol_dashboard()
    if up:
        return JSONResponse({"status": "ok", "output": msg})
    if not (NROL_AO_REPO / "server.py").exists():
        return JSONResponse({
            "status": "error",
            "output": f"NROL-AO server.py not found at {NROL_AO_REPO / 'server.py'}",
        })

    log_path = Path(__file__).parent / "nrol_ao_admin.log"
    env = _nrol_env()
    try:
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(NROL_AO_REPO),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        log_handle.close()
        _child_procs["nrol_dashboard"] = proc
        for i in range(15):
            await asyncio.sleep(1)
            up, msg = await _probe_nrol_dashboard()
            if up:
                return JSONResponse({
                    "status": "ok",
                    "output": f"{msg}\nPID: {proc.pid}\nLog: {log_path}",
                })
            if proc.poll() is not None:
                return JSONResponse({
                    "status": "error",
                    "output": f"NROL-AO dashboard exited with {proc.returncode}. Check {log_path}",
                })
        return JSONResponse({
            "status": "ok",
            "output": f"NROL-AO dashboard launched (PID {proc.pid}) but did not answer within 15s.\nLog: {log_path}",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Failed to launch NROL-AO dashboard: {e}"})


@app.post("/tools/nrol-dashboard-stop")
async def tool_nrol_dashboard_stop():
    """Stop the NROL-AO dashboard if this admin server launched it."""
    proc = _child_procs.pop("nrol_dashboard", None)
    if not proc:
        up, msg = await _probe_nrol_dashboard()
        return JSONResponse({
            "status": "error" if up else "ok",
            "output": (
                "No tracked NROL-AO dashboard process. "
                "If it is running, it was launched outside Loom admin and was not killed.\n"
                + msg
            ),
        })
    if proc.poll() is None:
        try:
            proc.terminate()
            return JSONResponse({"status": "ok", "output": f"Terminated NROL-AO dashboard PID {proc.pid}"})
        except Exception as e:
            return JSONResponse({"status": "error", "output": f"Failed to terminate PID {proc.pid}: {e}"})
    return JSONResponse({"status": "ok", "output": f"NROL-AO dashboard PID {proc.pid} already exited ({proc.returncode})"})


@app.post("/tools/nrol-mcp-smoke")
async def tool_nrol_mcp_smoke():
    """Run a short import/status smoke test for the stdio MCP bridge."""
    cmd = [
        sys.executable,
        "-c",
        "from mcp_servers.nrol_ao import server; print(server.nrol_status())",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).parent),
            env=_nrol_env(),
            capture_output=True,
            text=True,
            timeout=45,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        return JSONResponse({
            "status": "ok" if proc.returncode == 0 else "error",
            "output": f"exit_code: {proc.returncode}\n{output}",
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"status": "error", "output": "MCP smoke test timed out after 45s"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"MCP smoke test failed: {e}"})


@app.post("/tools/nrol-mcp-register")
async def tool_nrol_mcp_register():
    """Register the NROL-AO stdio MCP bridge with Claude Code user config."""
    cmd = [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "stdio",
        "nrol-ao",
        "--",
        sys.executable,
        str(Path(__file__).parent / "nrol_ao_mcp_server.py"),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path(__file__).parent),
            env=_nrol_env(),
            capture_output=True,
            text=True,
            timeout=45,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no output)"
        return JSONResponse({
            "status": "ok" if proc.returncode == 0 else "error",
            "output": " ".join(cmd) + f"\n\nexit_code: {proc.returncode}\n{output}",
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"status": "error", "output": "Claude MCP registration timed out after 45s"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Claude MCP registration failed: {e}"})








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
        proc = _spawn_detached(COMFYUI_LAUNCH_CMD, cwd=COMFYUI_LAUNCH_CWD, log_name="comfyui_admin.log")
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
            "output": f"Failed to launch ComfyUI: {e}\n\nCommand: {COMFYUI_LAUNCH_CMD}\nCWD: {COMFYUI_LAUNCH_CWD}\nSet env COMFYUI_LAUNCH_CMD or COMFYUI_LAUNCH_CWD to override.",
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
    # Best-effort: kill main.py-launched python.exe (covers portable .bat).
    # wmic.exe was removed in modern Windows 11 — use PowerShell instead.
    try:
        ps_script = (
            "$procs = Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" | "
            "Where-Object { $_.CommandLine -like '*main.py*--listen*' }; "
            "if ($procs) { foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; "
            "Write-Output (\"Killed PID \" + $p.ProcessId) } } "
            "else { Write-Output 'no-match' }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=15,
        )
        out = (r.stdout or r.stderr or "").strip()
        if out and out != "no-match":
            lines.append(out[:500])
        elif out == "no-match":
            lines.append("No untracked ComfyUI python.exe found.")
    except Exception as e:
        lines.append(f"PowerShell fallback failed: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "No ComfyUI processes found."})


@app.get("/tools/comfyui-log")
async def tool_comfyui_log(lines: int = 80):
    """Return the last N lines of comfyui_admin.log, plus diagnostics."""
    log_dir = Path(__file__).parent
    log_path = log_dir / "comfyui_admin.log"
    out = [
        f"Log path:    {log_path}",
        f"Log exists:  {log_path.exists()}",
        f"Launch cmd:  {COMFYUI_LAUNCH_CMD}",
        f"Launch cwd:  {COMFYUI_LAUNCH_CWD}",
        f"CWD exists:  {Path(COMFYUI_LAUNCH_CWD).exists()}",
        f"Tracked proc: {_child_procs.get('comfyui')}",
        "",
    ]
    proc = _child_procs.get("comfyui")
    if proc:
        out.append(f"Process poll: {proc.poll()} (None = still running)")
    if log_path.exists():
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            tail = "\n".join(text.splitlines()[-lines:])
            out.append(f"=== last {lines} lines ===")
            out.append(tail)
        except Exception as e:
            out.append(f"Failed to read log: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(out)})


@app.post("/tools/comfyui-fix-deps")
async def tool_comfyui_fix_deps():
    """Run pip upgrade for huggingface-hub and transformers in the ComfyUI Python env."""
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["py", "-3.12", "-m", "pip", "install", "huggingface-hub>=0.34.0", "transformers", "-U"],
            capture_output=True, text=True, timeout=180, cwd=COMFYUI_LAUNCH_CWD,
        )
        out = (r.stdout or "") + (r.stderr or "")
        status = "ok" if r.returncode == 0 else "error"
        return JSONResponse({"status": status, "output": out.strip() or f"exit {r.returncode}"})
    except Exception as e:
        return JSONResponse({"status": "error", "output": str(e)})


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
# ttyd — real web terminal (embedded in the dashboard Terminal tab)
# ---------------------------------------------------------------------------

TTYD_PORT = int(os.getenv("TTYD_PORT", "7681"))
# Localhost-only by default: ttyd is a full shell in a browser. Set
# TTYD_HOST=0.0.0.0 plus TTYD_CRED=user:pass to expose it over Tailscale/LAN.
TTYD_HOST = os.getenv("TTYD_HOST", "127.0.0.1")
TTYD_CRED = os.getenv("TTYD_CRED", "")

_TTYD_SHELLS = {
    "powershell": ["powershell.exe", "-NoLogo"],
    "cmd": ["cmd.exe"],
    "claude": ["claude"],
}


def _find_ttyd_exe() -> str:
    env_exe = os.getenv("TTYD_EXE", "")
    if env_exe and Path(env_exe).exists():
        return env_exe
    local = Path(__file__).parent / "bin" / "ttyd.exe"
    if local.exists():
        return str(local)
    import shutil
    return shutil.which("ttyd") or ""


def _ttyd_ssl_files() -> tuple[str, str]:
    base = Path(__file__).resolve().parent
    cert = os.getenv("LOOM_SSL_CERT", str(base / "certs" / "cert.pem"))
    key = os.getenv("LOOM_SSL_KEY", str(base / "certs" / "key.pem"))
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    return "", ""


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@app.get("/api/ttyd-status")
async def api_ttyd_status():
    proc = _child_procs.get("ttyd")
    if proc and proc.poll() is not None:
        _child_procs.pop("ttyd", None)
        proc = None
    running = _port_in_use(TTYD_PORT)
    cert, _key = _ttyd_ssl_files()
    return JSONResponse({
        "running": running,
        "pid": proc.pid if proc and running else None,
        "port": TTYD_PORT,
        "host": TTYD_HOST,
        "ssl": bool(cert),
        "auth": bool(TTYD_CRED),
        "exe": _find_ttyd_exe(),
        "shells": list(_TTYD_SHELLS.keys()),
    })


@app.post("/tools/ttyd-start")
async def tool_ttyd_start(body: dict = Body(default={})):
    """Launch ttyd serving an interactive shell over the web."""
    exe = _find_ttyd_exe()
    if not exe:
        return JSONResponse({
            "status": "error",
            "output": "ttyd.exe not found — put it at bin/ttyd.exe, set TTYD_EXE, "
                      "or add it to PATH (https://github.com/tsl0922/ttyd/releases)",
        })
    if _port_in_use(TTYD_PORT):
        return JSONResponse({"status": "ok", "output": f"ttyd already running on :{TTYD_PORT}"})

    shell_key = (body or {}).get("shell", "powershell")
    shell_cmd = _TTYD_SHELLS.get(shell_key, _TTYD_SHELLS["powershell"])
    if shell_key == "claude":
        import shutil
        resolved = shutil.which("claude")
        if not resolved:
            return JSONResponse({"status": "error", "output": "claude CLI not found on PATH"})
        shell_cmd = [resolved]

    cmd = [exe, "-p", str(TTYD_PORT), "-i", TTYD_HOST, "-W"]
    if TTYD_CRED:
        cmd += ["-c", TTYD_CRED]
    cert, key = _ttyd_ssl_files()
    if cert:
        # Same self-signed certs as the dashboard — if the page is https an
        # http iframe would be blocked as mixed content.
        cmd += ["-S", "-C", cert, "-K", key]
    cmd += shell_cmd

    log_path = Path(__file__).parent / "ttyd.log"
    try:
        log = open(log_path, "a")
        proc = subprocess.Popen(
            cmd, cwd=str(Path(__file__).parent),
            stdout=log, stderr=log,
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        )
        _child_procs["ttyd"] = proc
        await asyncio.sleep(1.0)
        if proc.poll() is not None:
            return JSONResponse({
                "status": "error",
                "output": f"ttyd exited immediately (code {proc.returncode}) — see ttyd.log",
            })
        scheme = "https" if cert else "http"
        return JSONResponse({
            "status": "ok",
            "output": f"ttyd running ({shell_key}) on {scheme}://{TTYD_HOST}:{TTYD_PORT} (PID {proc.pid})",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Failed to launch ttyd: {e}"})


@app.post("/tools/ttyd-stop")
async def tool_ttyd_stop():
    """Stop the tracked ttyd process (and any stray ttyd.exe)."""
    lines = []
    proc = _child_procs.pop("ttyd", None)
    if proc and proc.poll() is None:
        proc.kill()
        lines.append(f"Killed tracked ttyd (PID {proc.pid}).")
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "ttyd.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        if out:
            lines.append(out)
    except Exception as e:
        lines.append(f"taskkill: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "ttyd was not running."})


# ---------------------------------------------------------------------------
# Dashboard — static single-page app (static/admin/) over the JSON API
# ---------------------------------------------------------------------------

_ADMIN_STATIC = Path(__file__).parent / "static" / "admin"


@app.get("/api/meta")
async def api_meta():
    """Ports and link targets for the dashboard. The client rebuilds links
    with the page's own hostname so everything works over Tailscale."""
    cert, _ = _ttyd_ssl_files()
    return JSONResponse({
        "admin_port": ADMIN_PORT,
        "llama_port": _get_llama_port(),
        "nrol_port": NROL_AO_PORT,
        "comfy_port": 8188,
        "main_port": INSTANCES["main"]["port"],
        "test_port": INSTANCES["test"]["port"],
        "ttyd": {"port": TTYD_PORT, "host": TTYD_HOST, "ssl": bool(cert)},
    })


@app.get("/api/ports-status")
async def api_ports_status():
    """Cheap TCP liveness probes for the dashboard status dots."""
    ports = {
        "main": INSTANCES["main"]["port"],
        "test": INSTANCES["test"]["port"],
        "llama": _get_llama_port(),
        "nrol": NROL_AO_PORT,
        "comfy": 8188,
        "ttyd": TTYD_PORT,
        "dream": _dream_port(),
    }
    results = await asyncio.gather(
        *[asyncio.to_thread(_port_in_use, p) for p in ports.values()]
    )
    return JSONResponse({name: up for name, up in zip(ports, results)})


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index = _ADMIN_STATIC / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Loom Admin</h1><p>static/admin/index.html is missing.</p>", status_code=500)
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/assets/{filename}")
async def admin_asset(filename: str):
    """Serve dashboard assets without a StaticFiles mount (keeps deps slim)."""
    safe = Path(filename).name  # strip any path components
    path = _ADMIN_STATIC / safe
    if not path.exists() or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    media = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }.get(path.suffix.lower(), "application/octet-stream")
    return Response(path.read_bytes(), media_type=media)


@app.get("/api/databases")
async def api_databases():
    cwd = Path(__file__).parent
    dbs = [db.name for db in sorted(cwd.glob("*.db"))]
    return JSONResponse({"databases": dbs})


@app.post("/api/change-db")
async def api_change_db(db_name: str = Body(..., embed=True)):
    cwd = Path(__file__).parent
    db_path = cwd / db_name
    if not db_name.endswith(".db") or not db_path.is_file():
        return JSONResponse({"error": f"Invalid database file: {db_name}"}, status_code=400)

    config_file = cwd / "config.json"
    config_data = {}
    if config_file.is_file():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass

    config_data["db_path"] = db_name
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
    except Exception as e:
        return JSONResponse({"error": f"Failed to write config.json: {e}"}, status_code=500)

    INSTANCES["main"]["db"] = db_name

    # Auto-restart if running
    is_running = False
    proc = _child_procs.get("main")
    if proc:
        try:
            proc.poll()
            if proc.returncode is None:
                is_running = True
        except Exception:
            pass

    if is_running:
        await action_restart("main")

    return JSONResponse({"status": "success", "db": db_name, "restarted": is_running})


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


@app.get("/api/cron-help-proxy")
async def api_cron_help_proxy():
    for name, info in INSTANCES.items():
        try:
            r = await _get_instance(info["port"], "/api/cron/help")
            if r.status_code == 200:
                return JSONResponse(r.json())
        except Exception:
            continue
    return JSONResponse({"error": "no instance reachable"}, status_code=502)


@app.get("/api/cron-proxy")
async def api_cron_proxy(include_archived: bool = False):
    suffix = "true" if include_archived else "false"
    for name, info in INSTANCES.items():
        try:
            r = await _get_instance(info["port"], f"/api/cron/jobs?include_archived={suffix}")
            if r.status_code == 200:
                return JSONResponse(r.json())
        except Exception:
            continue
    return JSONResponse([])


@app.put("/api/cron-proxy/{job_id}")
async def api_cron_update_proxy(job_id: int, payload: dict = Body(...)):
    for name, info in INSTANCES.items():
        try:
            r = await _put_instance(info["port"], f"/api/cron/jobs/{job_id}", payload)
            if r.status_code in (200, 201):
                return JSONResponse(r.json())
        except Exception:
            continue
    return JSONResponse({"error": "no instance reachable"}, status_code=502)


@app.delete("/api/cron-proxy/{job_id}")
async def api_cron_archive_proxy(job_id: int):
    for name, info in INSTANCES.items():
        try:
            r = await _delete_instance(info["port"], f"/api/cron/jobs/{job_id}")
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


async def _put_instance(port: int, path: str, payload: dict) -> httpx.Response:
    """PUT JSON to an instance, trying HTTPS then HTTP."""
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                return await client.put(f"{scheme}://localhost:{port}{path}", json=payload)
        except Exception:
            continue
    raise ConnectionError(f"Cannot reach localhost:{port}")


async def _delete_instance(port: int, path: str) -> httpx.Response:
    """DELETE from an instance, trying HTTPS then HTTP."""
    for scheme in ("https", "http"):
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                return await client.delete(f"{scheme}://localhost:{port}{path}")
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
