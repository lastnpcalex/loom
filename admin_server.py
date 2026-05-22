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


# ── Ollama / ComfyUI process control ──────────────────────────────────────
# Tracked launches go in _child_procs under fixed keys so stop knows which
# proc to kill if we started it. Stop also taskkills by image name as a
# fallback (covers desktop-app-launched / pre-existing instances).

OLLAMA_LAUNCH_CMD = os.getenv("OLLAMA_LAUNCH_CMD", "ollama serve")
COMFYUI_LAUNCH_CMD = os.getenv(
    "COMFYUI_LAUNCH_CMD",
    r'"C:\Users\exast\Downloads\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\run_nvidia_gpu.bat"',
)

VLLM_PORT = int(os.getenv("VLLM_PORT", "8000"))


def _build_ollama_env(env: dict) -> dict:
    """Apply operator-tuned Ollama env vars from config.json on top of inherited env.
    Existing env entries win so users can still pin via shell."""
    if _loom_config is None:
        # No config import — keep historical hardcoded defaults so admin still works.
        env.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
        env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
        env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
        return env
    cfg = _loom_config
    env.setdefault("OLLAMA_KV_CACHE_TYPE", cfg.ollama_kv_cache_type)
    env.setdefault("OLLAMA_FLASH_ATTENTION", "1" if cfg.ollama_flash_attention else "0")
    env.setdefault("OLLAMA_KEEP_ALIVE", cfg.ollama_keep_alive)
    env.setdefault("OLLAMA_NUM_PARALLEL", str(cfg.ollama_num_parallel))
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", str(cfg.ollama_max_loaded_models))
    env.setdefault("OLLAMA_CONTEXT_LENGTH", str(cfg.ollama_context_length))
    return env


def _build_vllm_cmd() -> str:
    """Build the `vllm serve ...` command from saved config. Returns empty string
    if no model is configured (caller must surface a friendly error)."""
    # Operator can still hard-override the entire command via env.
    override = os.getenv("VLLM_LAUNCH_CMD")
    if override:
        return override

    model = (os.getenv("VLLM_MODEL") or
             (_loom_config.vllm_model if _loom_config else "")).strip()
    if not model:
        return ""

    # Pick which vllm binary to invoke. The config field is named "python path"
    # for UX clarity, but vllm has no __main__ — we actually invoke vllm.exe
    # from the same Scripts dir. If the operator pointed at python.exe, swap
    # to the sibling vllm.exe; if they passed vllm.exe directly, use as-is;
    # if empty, fall back to `vllm` on PATH.
    vllm_py = (_loom_config.vllm_python_path if _loom_config else "").strip()
    if vllm_py:
        if vllm_py.lower().endswith("python.exe") or vllm_py.lower().endswith("python"):
            vllm_exe = vllm_py.rsplit("/", 1)[0].rsplit("\\", 1)[0] + "/vllm.exe"
        else:
            vllm_exe = vllm_py
        cmd_head = f'"{vllm_exe}" serve {model}'
    else:
        cmd_head = f"vllm serve {model}"
    parts = [cmd_head, f"--port {VLLM_PORT}"]

    if _loom_config is not None:
        cfg = _loom_config
        # vLLM accepts multiple --served-model-name values, so we register both:
        #   1. The full HF id (so Weave/OODA dropdowns show a meaningful name)
        #   2. The slash-free alias (so Claude Code can use it — CC chokes on "/")
        # Both route to the same loaded model. Order matters for /v1/models —
        # the FIRST name becomes the canonical id; we put the alias first so
        # vllm-* prefix detection in the dispatcher stays predictable.
        served_names = []
        if cfg.vllm_served_name:
            served_names.append(cfg.vllm_served_name)
        if model and model not in served_names:
            served_names.append(model)
        if served_names:
            parts.append("--served-model-name " + " ".join(served_names))
        if cfg.vllm_quantization and cfg.vllm_quantization != "none":
            parts.append(f"--quantization {cfg.vllm_quantization}")
        if cfg.vllm_kv_cache_dtype and cfg.vllm_kv_cache_dtype != "auto":
            parts.append(f"--kv-cache-dtype {cfg.vllm_kv_cache_dtype}")
        parts.append(f"--max-model-len {cfg.vllm_max_model_len}")
        parts.append(f"--gpu-memory-utilization {cfg.vllm_gpu_memory_utilization}")
        parts.append(f"--max-num-seqs {cfg.vllm_max_num_seqs}")
        if cfg.vllm_tensor_parallel_size > 1:
            parts.append(f"--tensor-parallel-size {cfg.vllm_tensor_parallel_size}")
        if cfg.vllm_enable_auto_tool_choice:
            parts.append("--enable-auto-tool-choice")
        if cfg.vllm_tool_call_parser and cfg.vllm_tool_call_parser != "none":
            parts.append(f"--tool-call-parser {cfg.vllm_tool_call_parser}")
        # Reasoning parser — required for Qwen3.6 thinking blocks + MTP.
        if cfg.vllm_reasoning_parser and cfg.vllm_reasoning_parser != "none":
            parts.append(f"--reasoning-parser {cfg.vllm_reasoning_parser}")
        # Speculative / MTP config — passed verbatim. JSON gets quoted so the
        # shell doesn't choke on the {} braces and embedded quotes.
        spec = cfg.vllm_speculative_config.strip()
        if spec:
            spec_quoted = spec.replace('"', r'\"')
            parts.append(f'--speculative-config "{spec_quoted}"')
        # Override the chat template's enable_thinking default — Qwen3.6's
        # template defaults to on, which produces multi-thousand-token reasoning
        # prefixes before any text. Off here means CC requests get fast text;
        # users opt in per-conv via chat_template_kwargs in their request.
        thinking = "true" if cfg.vllm_thinking_default else "false"
        parts.append(f'--default-chat-template-kwargs "{{\\"enable_thinking\\": {thinking}}}"')
        if cfg.vllm_extra_args.strip():
            parts.append(cfg.vllm_extra_args.strip())
    else:
        # Fallback when config isn't importable
        extra = os.getenv("VLLM_EXTRA_ARGS",
            "--quantization compressed-tensors --kv-cache-dtype fp8 --max-model-len 32768 "
            "--gpu-memory-utilization 0.92 --enable-auto-tool-choice --tool-call-parser hermes")
        parts.append(extra)
    return " ".join(parts)


def _spawn_detached(cmd: str, cwd: str | None = None) -> subprocess.Popen:
    """Spawn a long-running command in a detached process group on Windows.
    Pulls Ollama tuning from config.json each time so saving the settings panel
    takes effect on the next start without restarting admin.

    Routes stdout/stderr to a per-service log file in the working tree so that
    crashes and request errors are debuggable without a console window."""
    _reload_config()
    env = os.environ.copy()
    log_name = None
    if "ollama" in cmd.lower():
        env = _build_ollama_env(env)
        log_name = "ollama_admin.log"
    if cmd.startswith("vllm ") or "vllm.exe" in cmd.lower() or "-m vllm" in cmd:
        env.setdefault("VLLM_ATTENTION_BACKEND", "FLASH_ATTN")
        # Force UTF-8 so vLLM's box-drawing banner doesn't crash logging on cp1252.
        env.setdefault("PYTHONUTF8", "1")
        # flashinfer on Windows requires an explicit CUDA root path. Auto-detect
        # from CUDA_PATH (set by the toolkit installer) if CUDA_LIB_PATH isn't
        # already in env. Without this, vLLM crashes during attention backend init.
        cuda_root = env.get("CUDA_LIB_PATH") or env.get("CUDA_PATH") or env.get("CUDA_HOME")
        if cuda_root:
            env.setdefault("CUDA_LIB_PATH", cuda_root)
            env.setdefault("CUDA_HOME", cuda_root)
        log_name = "vllm_admin.log"
    if "comfyui" in cmd.lower() or "comfy" in cmd.lower():
        log_name = log_name or "comfyui_admin.log"

    log_dir = Path(__file__).parent
    if log_name:
        # Truncate-on-spawn so we don't accrete logs from prior runs.
        log_path = log_dir / log_name
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        stdout_target = log_handle
        stderr_target = subprocess.STDOUT
    else:
        stdout_target = subprocess.DEVNULL
        stderr_target = subprocess.DEVNULL

    return subprocess.Popen(
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


@app.post("/tools/ollama-start")
async def tool_ollama_start():
    """Launch Ollama if not already running. Inherits current env."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://127.0.0.1:11434/api/tags")
            if r.status_code == 200:
                return JSONResponse({"status": "ok", "output": "Ollama is already running on :11434"})
    except Exception:
        pass
    try:
        proc = _spawn_detached(OLLAMA_LAUNCH_CMD)
        _child_procs["ollama"] = proc
        # Wait briefly for it to become reachable
        for i in range(15):
            await asyncio.sleep(1)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get("http://127.0.0.1:11434/api/tags")
                    if r.status_code == 200:
                        return JSONResponse({
                            "status": "ok",
                            "output": f"Ollama launched and ready after {i+1}s (PID {proc.pid}).",
                        })
            except Exception:
                continue
        return JSONResponse({
            "status": "ok",
            "output": f"Ollama launched (PID {proc.pid}) but not yet responding after 15s. May still be coming up.",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "output": f"Failed to launch Ollama: {e}"})


@app.post("/tools/ollama-stop")
async def tool_ollama_stop():
    """Kill all ollama.exe processes (and any tracked Ollama launch)."""
    lines = []
    proc = _child_procs.pop("ollama", None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            lines.append(f"Terminated tracked Ollama proc (PID {proc.pid}).")
        except Exception as e:
            lines.append(f"Failed to terminate tracked proc: {e}")
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/IM", "ollama.exe", "/T"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        lines.append(out or f"taskkill exit {r.returncode}")
    except Exception as e:
        lines.append(f"taskkill failed: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "No Ollama processes found."})


@app.post("/tools/vllm-start")
async def tool_vllm_start():
    """Launch vLLM (OpenAI-compat server) on VLLM_PORT if not already running.
    The launch command is rebuilt from config.json each call so saved tuning
    takes effect on the next start without restarting admin."""
    _reload_config()
    cmd = _build_vllm_cmd()
    if not cmd:
        return JSONResponse({
            "status": "error",
            "output": "vLLM model is not set. Either save a model in Settings → Advanced → vLLM, set VLLM_MODEL in env, or set VLLM_LAUNCH_CMD to the full command.",
        })
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"http://127.0.0.1:{VLLM_PORT}/v1/models")
            if r.status_code == 200:
                return JSONResponse({"status": "ok", "output": f"vLLM is already running on :{VLLM_PORT}"})
    except Exception:
        pass
    try:
        proc = _spawn_detached(cmd)
        _child_procs["vllm"] = proc
        # vLLM cold-start with model load can be 30-90s depending on size.
        for i in range(120):
            await asyncio.sleep(1)
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get(f"http://127.0.0.1:{VLLM_PORT}/v1/models")
                    if r.status_code == 200:
                        return JSONResponse({
                            "status": "ok",
                            "output": f"vLLM launched and ready after {i+1}s (PID {proc.pid}).\nCmd: {cmd}",
                        })
            except Exception:
                continue
        return JSONResponse({
            "status": "ok",
            "output": f"vLLM launched (PID {proc.pid}) but not responding after 120s. Large models can need more — check again shortly.\nCmd: {cmd}",
        })
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "output": f"Failed to launch vLLM: {e}\n\nCmd: {cmd}",
        })


@app.post("/tools/vllm-stop")
async def tool_vllm_stop():
    """Terminate tracked vLLM proc; fall back to killing python on the port."""
    lines = []
    proc = _child_procs.pop("vllm", None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            lines.append(f"Terminated tracked vLLM proc (PID {proc.pid}).")
        except Exception as e:
            lines.append(f"Failed to terminate tracked proc: {e}")
    # Fallback: kill any python.exe whose command-line has 'vllm serve'
    try:
        r = subprocess.run(
            ["wmic", "process", "where",
             "name='python.exe' and CommandLine like '%vllm%serve%'",
             "delete"],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout or r.stderr or "").strip()
        if out:
            lines.append(out[:300])
    except Exception as e:
        lines.append(f"WMIC fallback failed: {e}")
    return JSONResponse({"status": "ok", "output": "\n".join(lines) or "No vLLM processes found."})


@app.post("/tools/vllm-restart")
async def tool_vllm_restart():
    """Stop the running vLLM (if any), wait for VRAM/port to free, then start
    a fresh instance using the current saved config. Used by the Settings
    panel's "Apply & Restart vLLM" button so changing vllm_model picks up
    cleanly without an admin terminal."""
    # Step 1 — tear down any existing instance
    await tool_vllm_stop()
    # Step 2 — give the socket and GPU a moment to release; vLLM holds onto
    # the port for ~3-5s after the python procs exit.
    for _ in range(10):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                r = await client.get(f"http://127.0.0.1:{VLLM_PORT}/v1/models")
                if r.status_code != 200:
                    break
        except Exception:
            break
    # Step 3 — start fresh from the latest config
    return await tool_vllm_start()


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
</style>
</head>
<body>
<h1>Loom Admin</h1>

<div class="quick-links">
    <a href="https://localhost:3000" target="_blank" class="quick-link">&#127760; Main Loom (:3000)</a>
    <a href="http://localhost:3001" target="_blank" class="quick-link">&#129514; Test Server (:3001)</a>
    <a href="http://localhost:11434" target="_blank" class="quick-link">&#129303; Ollama (:11434)</a>
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
    <button class="tool-btn" onclick="runTool('ollama-ps')">
        <span class="icon">&#128202;</span> Ollama PS
        <span class="label">Loaded models</span>
    </button>
    <button class="tool-btn" onclick="runTool('ollama-models')">
        <span class="icon">&#128451;</span> Model List
        <span class="label">Available models</span>
    </button>
    <button class="tool-btn" onclick="runTool('ollama-start')">
        <span class="icon">&#9658;</span> Start Ollama
        <span class="label">Launch ollama serve</span>
    </button>
    <button class="tool-btn" onclick="confirmTool('ollama-stop', 'Kill all ollama.exe processes?')">
        <span class="icon">&#9209;</span> Stop Ollama
        <span class="label">Kill all ollama.exe</span>
    </button>
    <button class="tool-btn" onclick="runTool('vllm-start')">
        <span class="icon">&#9658;</span> Start vLLM
        <span class="label">vllm serve (NVFP4)</span>
    </button>
    <button class="tool-btn" onclick="confirmTool('vllm-stop', 'Stop vLLM server?')">
        <span class="icon">&#9209;</span> Stop vLLM
        <span class="label">Terminate vLLM process</span>
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
            out.textContent = d.output || '(no output)';
            out.className = d.status === 'error' ? 'visible error' : 'visible';
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
