#!/usr/bin/env python3
"""Phase-1 probe: capture the Hermes Agent ACP (Agent Client Protocol) wire format.

Native-Windows path (Hermes installed via the .ps1 installer or `uv pip install -e .`
into %LOCALAPPDATA%\\hermes\\hermes-agent). Spawns `hermes acp` (or `python -m
acp_adapter`), runs a JSON-RPC 2.0 handshake over stdio, then issues a tool-using
prompt so we can observe a `session/request_permission` frame.  Every frame in
both directions is printed with a direction marker.  A watchdog kills the whole
thing after WATCHDOG_S.

This is a *probe*, not production code.  Run it once, eyeball the output, and
commit it as tools/probe_output.txt.  Phase 2 (hermes_client.py) keys off the
field names this captures.

Usage:
    python tools/probe_hermes_acp.py
    python tools/probe_hermes_acp.py --hermes "C:\\path\\to\\hermes.exe" --prompt "..."

The local model (config.yaml model.default) must support >=64k context (Hermes
refuses otherwise).  We point Hermes at the local model via %HERMES_HOME%\\config.yaml.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

WATCHDOG_S = 150

_DEFAULT_HERMES_HOME = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes")
_DEFAULT_HERMES_EXE = os.path.join(
    _DEFAULT_HERMES_HOME, "hermes-agent", ".venv", "Scripts", "hermes.exe"
)

_id = 0
_lock = threading.Lock()


def next_id() -> int:
    global _id
    with _lock:
        _id += 1
        return _id


def log(direction: str, obj) -> None:
    """direction in {'>>','<<','==','!!'}"""
    ts = time.strftime("%H:%M:%S")
    if isinstance(obj, (dict, list)):
        body = json.dumps(obj, ensure_ascii=False)
    else:
        body = str(obj)
    print(f"[{ts}] {direction} {body}", flush=True)


def send(proc: subprocess.Popen, msg: dict) -> None:
    log(">>", msg)
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(data)
    proc.stdin.flush()


def make_request(method: str, params: dict | None = None) -> dict:
    m = {"jsonrpc": "2.0", "id": next_id(), "method": method}
    if params is not None:
        m["params"] = params
    return m


def make_response(req_id, result=None, error=None) -> dict:
    m = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        m["error"] = error
    else:
        m["result"] = result if result is not None else {}
    return m


def stderr_pump(proc: subprocess.Popen) -> None:
    assert proc.stderr is not None
    for raw in proc.stderr:
        try:
            line = raw.decode("utf-8", "replace").rstrip()
        except Exception:
            line = repr(raw)
        log("==", f"[stderr] {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes", default=_DEFAULT_HERMES_EXE,
                    help="Path to hermes.exe (default: venv Scripts under %%LOCALAPPDATA%%\\hermes).")
    ap.add_argument("--hermes-home", default=_DEFAULT_HERMES_HOME)
    ap.add_argument(
        "--prompt",
        default="Use your terminal tool to run the shell command `echo HELLO_FROM_HERMES` "
                "and then tell me exactly what it printed. Use the terminal tool — do not guess.",
        help="A prompt that should force at least one tool call / permission ask.",
    )
    args = ap.parse_args()

    env = dict(os.environ)
    env["HERMES_HOME"] = args.hermes_home
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"

    if os.path.exists(args.hermes):
        cmd = [args.hermes, "acp"]
    else:
        # Fall back to `python -m acp_adapter` from the venv.
        venv_py = os.path.join(args.hermes_home, "hermes-agent", ".venv", "Scripts", "python.exe")
        cmd = [venv_py if os.path.exists(venv_py) else sys.executable, "-m", "acp_adapter"]

    log("==", f"spawn: {cmd}  (HERMES_HOME={args.hermes_home})")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env,
        cwd=os.getcwd(),
    )

    threading.Thread(target=stderr_pump, args=(proc,), daemon=True).start()

    done = threading.Event()

    def watchdog() -> None:
        if not done.wait(WATCHDOG_S):
            log("!!", f"watchdog fired after {WATCHDOG_S}s -- killing")
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=watchdog, daemon=True).start()

    session_id: str | None = None
    sent_prompt = False
    end_seen = False

    init_id = next_id()
    send(proc, {
        "jsonrpc": "2.0", "id": init_id, "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
            "clientInfo": {"name": "loom-acp-probe", "version": "0.0.1"},
        },
    })

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log("<<", f"[non-JSON stdout] {line[:400]!r}")
                continue
            log("<<", msg)

            mid = msg.get("id")
            method = msg.get("method")

            # ---- incoming requests from the agent (must answer) ----
            if method and mid is not None:
                params = msg.get("params") or {}
                if method in ("session/request_permission", "session/requestPermission"):
                    options = params.get("options") or []
                    chosen = None
                    for opt in options:
                        k = (opt.get("kind") or "").lower()
                        if k == "allow_once":
                            chosen = opt
                            break
                    if chosen is None and options:
                        chosen = options[0]
                    if chosen is not None:
                        oid = chosen.get("optionId") or chosen.get("option_id") or chosen.get("id")
                        result = {"outcome": {"outcome": "selected", "optionId": oid}}
                    else:
                        result = {"outcome": {"outcome": "cancelled"}}
                    log("==", f"answering permission with optionId={result['outcome'].get('optionId')!r}")
                    send(proc, make_response(mid, result=result))
                elif method.startswith("fs/"):
                    send(proc, make_response(mid, error={"code": -32601, "message": "probe does not serve fs"}))
                elif method.startswith("terminal/"):
                    send(proc, make_response(mid, error={"code": -32601, "message": "probe does not serve terminals"}))
                else:
                    log("!!", f"unhandled incoming request method={method!r} -> empty result")
                    send(proc, make_response(mid, result={}))
                continue

            # ---- notifications from the agent ----
            if method and mid is None:
                if method in ("session/update", "session/updated"):
                    upd = (msg.get("params") or {}).get("update") or {}
                    kind = upd.get("sessionUpdate") or upd.get("session_update")
                    if kind:
                        log("==", f"  (session/update kind = {kind!r})")
                continue

            # ---- responses to our requests ----
            if mid is not None and ("result" in msg or "error" in msg):
                if mid == init_id:
                    log("==", "initialize complete; sending session/new")
                    send(proc, make_request("session/new", {
                        "cwd": os.getcwd().replace("\\", "/"),
                        "mcpServers": [],
                    }))
                elif session_id is None and isinstance(msg.get("result"), dict) and (
                    "sessionId" in msg["result"] or "session_id" in msg["result"]
                ):
                    session_id = msg["result"].get("sessionId") or msg["result"].get("session_id")
                    log("==", f"session established: {session_id}")
                    if not sent_prompt:
                        sent_prompt = True
                        send(proc, make_request("session/prompt", {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": args.prompt}],
                        }))
                elif sent_prompt:
                    log("==", "session/prompt returned -- turn complete")
                    if isinstance(msg.get("result"), dict):
                        log("==", f"  stopReason = {msg['result'].get('stopReason') or msg['result'].get('stop_reason')!r}")
                    end_seen = True

            if end_seen:
                log("==", "stopping read loop")
                break
    finally:
        done.set()
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log("==", f"exit code: {proc.returncode}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
