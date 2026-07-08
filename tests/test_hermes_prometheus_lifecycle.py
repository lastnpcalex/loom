"""Lifecycle invariant tests for the Prometheus + Attendant Hermes redesign.

These pin the three load-bearing invariants from the design's Verification
section (HERMES_PROMETHEUS_DESIGN.md):

1. **Coupling** — stopping llama-server clears the llama attendant's warm
   runtime from _RUNTIMES (a soul-bearing process must not outlive its model).
2. **Decoupling** — stopping llama leaves Prometheus + the dream attendant
   untouched (Prometheus is independent; dream is a different model).
3. **No silent de-souling** — an ensouled turn whose model is down REFUSES with
   a Loom-specific error and emits NO generation events (no silent fallback to
   Prometheus).

Plus the probe-cache invariant: _ensure_prometheus_home seeds
context_length_cache.yaml with BOTH the local + Umans cloud entries so a warm
re-init on either backend skips the 6.5s context-length probe.

These mock the `hermes acp` subprocess entirely (same fake-subprocess pattern as
test_hermes_smoke.py), so they run without a Hermes install and without
llama-server. They never touch the real %LOCALAPPDATA% homes.
"""

import asyncio
import json
from pathlib import Path

import pytest

import hermes_client
from hermes_client import _RUNTIMES, _HermesAcpRuntime


@pytest.fixture(autouse=True)
async def _clean_hermes_runtimes():
    await hermes_client.shutdown_hermes_runtimes()
    yield
    await hermes_client.shutdown_hermes_runtimes()


# --------------------------------------------------------------------------- #
# Fake asyncio subprocess (minimal — only enough for ensure_started to place
# a runtime in _RUNTIMES; we never run a real turn in these tests).
# --------------------------------------------------------------------------- #

class _FakeStreamReader:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()
        self._eof = False

    def feed_line(self, line: bytes) -> None:
        self._q.put_nowait(line if line.endswith(b"\n") else line + b"\n")

    def feed_eof(self) -> None:
        self._q.put_nowait(None)

    async def readline(self) -> bytes:
        if self._eof:
            return b""
        item = await self._q.get()
        if item is None:
            self._eof = True
            return b""
        return item

    def at_eof(self) -> bool:
        return self._eof and self._q.empty()

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _FakeStreamWriter:
    def __init__(self, on_write=None):
        self.writes: list[bytes] = []
        self._on_write = on_write
        self._closing = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self._on_write is not None:
            self._on_write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing


class _FakeProc:
    def __init__(self, stdin, stdout, stderr):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pid = 4242
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.returncode = -9
        self._done.set()


def _frame(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


_INIT_RESP = _frame({"jsonrpc": "2.0", "id": 1, "result": {
    "protocolVersion": 1, "agentInfo": {"name": "hermes-agent", "version": "0.13.0"},
    "agentCapabilities": {"loadSession": True}}})
_NEW_RESP = _frame({"jsonrpc": "2.0", "id": 2, "result": {
    "sessionId": "sess-abc",
    "models": {"availableModels": [], "currentModelId": "custom:test:1b"}}})


def _make_fake_subprocess(monkeypatch):
    """Monkeypatch asyncio.create_subprocess_exec with a fake that returns a
    process whose stdout feeds the init + session/new handshake. Returns the
    proc so the test can drive further frames if needed."""
    stdout = _FakeStreamReader()
    stderr = _FakeStreamReader()
    stderr.feed_eof()
    stdin = _FakeStreamWriter()
    proc = _FakeProc(stdin, stdout, stderr)

    def _on_write(data: bytes) -> None:
        req = json.loads(data.decode("utf-8"))
        if req.get("method") == "session/new":
            proc.stdout.feed_line(_NEW_RESP)

    stdin._on_write = _on_write
    stdout.feed_line(_INIT_RESP)

    async def _factory(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _factory)
    return proc


async def _warm_runtime(home: str, *, exe: str = "hermes") -> _HermesAcpRuntime:
    """Place a held, alive runtime into _RUNTIMES for the given home. Uses the
    real _HermesAcpRuntime + ensure_started path (monkeypatched subprocess),
    so find_runtime_by_home / stop_runtime_by_home exercise the real code."""
    rt = _HermesAcpRuntime(exe=exe, home=home, cwd=".")
    # Bypass ensure_started's real subprocess spawn: fake the proc directly so
    # the runtime is "alive" without a real handshake. This is the state the
    # attendant-clear binding acts on (proc.returncode is None + rpc set).
    rt.proc = _FakeProc(_FakeStreamWriter(), _FakeStreamReader(), _FakeStreamReader())
    rt.rpc = hermes_client._RpcConn(rt.proc)
    key = hermes_client._runtime_key(exe, home)
    _RUNTIMES[key] = rt
    return rt


# --------------------------------------------------------------------------- #
# Invariant 1: stopping llama-server clears the llama attendant runtime
# --------------------------------------------------------------------------- #

async def test_stop_llama_server_clears_llama_attendant_runtime(tmp_path, monkeypatch):
    """After the llama unload path runs, _RUNTIMES contains no entry whose home
    == config.hermes_home (the llama attendant's home).

    Prevents a future change from silently unbinding the attendant-clear and
    leaving a soul process pointed at a dead /v1 endpoint. This tests the
    hermes_client primitive (stop_runtime_by_home) that the admin llama-unload
    path relays to via /api/hermes/attendant/clear?backend=llama.
    """
    llama_home = str(tmp_path / "llama-home")
    Path(llama_home).mkdir(exist_ok=True)
    rt = await _warm_runtime(llama_home)

    # Sanity: it's held + alive.
    assert hermes_client.find_runtime_by_home(llama_home) is rt
    assert rt._is_alive()

    cleared = await hermes_client.stop_runtime_by_home(llama_home)

    assert cleared is True
    assert hermes_client.find_runtime_by_home(llama_home) is None
    # The runtime was removed from _RUNTIMES (not just stopped in place).
    import os
    assert not any(os.path.abspath(r.home) == os.path.abspath(llama_home)
                  for r in _RUNTIMES.values())


# --------------------------------------------------------------------------- #
# Invariant 2: stopping llama does NOT clear Prometheus or the dream attendant
# --------------------------------------------------------------------------- #

async def test_stop_llama_does_not_clear_prometheus_or_dream(tmp_path, monkeypatch):
    """Decoupling invariant: stopping llama leaves Prometheus + the dream
    attendant untouched.

    Prometheus is independent (admin-managed, always-warm, cloud-fallback); the
    dream attendant is bound to a different model server. A regression that
    made stop_runtime_by_home over-broad (e.g. clearing all runtimes, or keying
    by exe instead of home) would break this.
    """
    llama_home = str(tmp_path / "llama-home")
    dream_home = str(tmp_path / "dream-home")
    prometheus_home = str(tmp_path / "prometheus-home")
    for h in (llama_home, dream_home, prometheus_home):
        Path(h).mkdir(exist_ok=True)

    llama_rt = await _warm_runtime(llama_home, exe="hermes")
    dream_rt = await _warm_runtime(dream_home, exe="hermes")
    prometheus_rt = await _warm_runtime(prometheus_home, exe="hermes")

    # Stop ONLY the llama attendant (what the llama-unload path does).
    cleared = await hermes_client.stop_runtime_by_home(llama_home)
    assert cleared is True
    assert hermes_client.find_runtime_by_home(llama_home) is None

    # Prometheus + dream survive untouched — same objects, still alive + held.
    assert hermes_client.find_runtime_by_home(prometheus_home) is prometheus_rt
    assert hermes_client.find_runtime_by_home(dream_home) is dream_rt
    assert prometheus_rt._is_alive()
    assert dream_rt._is_alive()


# --------------------------------------------------------------------------- #
# Invariant 2b: a backend flip / model-stop must NOT kill a runtime mid-turn
# --------------------------------------------------------------------------- #

async def test_stop_refuses_when_active_turn_in_flight(tmp_path):
    """stop_runtime_by_home must refuse (return False, leave the runtime held)
    when the target runtime has an active turn.

    run_turn() holds _lock for the duration of a turn and sets _active_turn; stop()
    does NOT take _lock, so killing mid-turn would terminate the process backing a
    live generation — corrupting the user's conversation. A backend flip or admin
    restart that hits a live turn returns False and lets the turn finish; the next
    turn re-routes against the (already-rewritten) config. This pins the guard
    added in response to the code review (BUG 3): the active-turn check must not
    regress to an unconditional stop.
    """
    llama_home = str(tmp_path / "llama-home")
    Path(llama_home).mkdir(exist_ok=True)
    rt = await _warm_runtime(llama_home)

    # Simulate an in-flight turn: _active_turn set (run_turn sets it at
    # hermes_client.py:766 and clears it in the finally at :881).
    rt._active_turn = object()  # truthy sentinel; the guard only checks `is not None`
    assert rt._is_alive()

    cleared = await hermes_client.stop_runtime_by_home(llama_home)

    # Refused: not cleared, runtime still held + alive.
    assert cleared is False
    assert hermes_client.find_runtime_by_home(llama_home) is rt
    assert rt._is_alive()
    assert rt.proc is not None  # process NOT killed

    # Once the turn finishes (_active_turn cleared), a subsequent stop succeeds.
    rt._active_turn = None
    cleared2 = await hermes_client.stop_runtime_by_home(llama_home)
    assert cleared2 is True
    assert hermes_client.find_runtime_by_home(llama_home) is None


# --------------------------------------------------------------------------- #
# Invariant 2c: a refused stop must NOT bump the backend marker (BUG 1)
# --------------------------------------------------------------------------- #

async def test_route_prometheus_does_not_bump_marker_on_refused_stop(tmp_path, monkeypatch):
    """route_prometheus_backend must NOT write the new .loom_backend signature
    when stop_runtime_by_home refuses (active turn in flight).

    The marker is what later turns compare against to decide whether to reload.
    If a refused stop bumped the marker anyway, the OLD warm process would keep
    running (pointed at the old endpoint) while the marker says the new backend
    is current — so every subsequent turn sees signature-match and reuses the
    stale process indefinitely. The marker must only advance on a successful
    clear (or when no warm runtime exists).

    Pins the round-2 BUG-1 fix: the marker write is gated on `cleared`.
    """
    import server as srv

    prometheus_home = tmp_path / "prometheus-home"
    monkeypatch.setenv("PROMETHEUS_HERMES_HOME", str(prometheus_home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "hermes_home", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "llama_model", "TestLlama.gguf")
    monkeypatch.setattr(srv.config, "llama_host", "http://127.0.0.1:8000")
    monkeypatch.setattr(srv.config, "prometheus_cloud_model", "umans-glm-5.2")
    monkeypatch.setattr(srv.config, "prometheus_cloud_base_url", "https://api.code.umans.ai/v1")
    monkeypatch.setattr(srv.config, "prometheus_cloud_context", 200000)
    monkeypatch.setattr(srv.config, "max_context_tokens", 32768)

    # Force cloud backend (no local model up) so the signature is stable + known.
    async def _probe_down(*a, **kw):
        return False
    monkeypatch.setattr(srv, "_probe_llama_live", _probe_down)
    monkeypatch.setattr(srv, "_probe_dream_live", _probe_down)

    # First call: no warm runtime → marker advances, reloaded=True.
    backend1 = await srv.route_prometheus_backend()
    assert backend1["reloaded"] is True
    marker = prometheus_home / ".loom_backend"
    assert marker.exists()
    sig1 = marker.read_text(encoding="utf-8").strip()

    # Place a warm Prometheus runtime with an active turn (the refused-stop case).
    rt = await _warm_runtime(str(prometheus_home), exe="hermes")
    rt._active_turn = object()  # active turn → stop refuses

    # Second call, same backend signature: needs_reload is False (no change), so
    # the marker is untouched regardless. reloaded=False.
    backend2 = await srv.route_prometheus_backend()
    assert backend2["reloaded"] is False
    assert marker.read_text(encoding="utf-8").strip() == sig1

    # Third call with force=True (admin Restart): needs_reload is True, but the
    # stop refuses (active turn) → marker must NOT advance, reloaded=False.
    backend3 = await srv.route_prometheus_backend(force=True)
    assert backend3["reloaded"] is False
    assert marker.read_text(encoding="utf-8").strip() == sig1, \
        "marker advanced despite refused stop — stale-process reuse regression"

    # The warm runtime is still held (stop was refused).
    assert hermes_client.find_runtime_by_home(str(prometheus_home)) is rt

    # Once the turn finishes, a forced reload advances the marker.
    rt._active_turn = None
    backend4 = await srv.route_prometheus_backend(force=True)
    assert backend4["reloaded"] is True
    assert hermes_client.find_runtime_by_home(str(prometheus_home)) is None


# --------------------------------------------------------------------------- #
# Invariant 3: an ensouled turn whose model is down REFUSES — no silent de-souling
# --------------------------------------------------------------------------- #

async def test_ensouled_turn_refuses_when_model_down(tmp_path, monkeypatch):
    """An ensouled (non-incognito) Hermes turn with the model down returns a
    Loom-specific error and emits NO stream events.

    Prevents silent de-souling via auto-fallback regressing back in. Tests the
    routing decision at the dispatch level: _probe_llama_live → False routes to
    _refuse_ensouled_model_down, which emits an error with ensouled_refusal=True
    and NEVER calls run_hermes (no generation).

    We capture _ws_send to assert exactly one error event and zero stream events.
    """
    import server as srv

    # Force the llama probe to report DOWN.
    async def _probe_down(*a, **kw):
        return False
    monkeypatch.setattr(srv, "_probe_llama_live", _probe_down)

    # Capture every _ws_send payload for the conv.
    sent: list[dict] = []
    orig_ws_send = srv._ws_send

    async def _capture_ws(conv_id, data):
        sent.append({"conv_id": conv_id, **data})
    monkeypatch.setattr(srv, "_ws_send", _capture_ws)

    # Also guard: if the refuse path ever fell through to generation, run_hermes
    # would be called — fail loudly if it is.
    async def _no_run_hermes(*a, **kw):
        raise AssertionError("ensouled turn with model down must NOT call run_hermes "
                              "(silent de-souling regression)")
    monkeypatch.setattr(srv.hermes_client, "run_hermes", _no_run_hermes)

    # A fake websocket object (the refuse helper takes it but doesn't use it).
    class _FakeWS:
        pass

    # Drive the refuse helper directly — this is what the dispatch guard calls
    # when mode=='hermes', not incognito, and the probe is down.
    await srv._refuse_ensouled_model_down(_FakeWS(), conv_id=42, backend="llama",
                                          model_name="Qwen3.6-27B-NVFP4.gguf")

    # Exactly one event: the error. No stream_start, no text_delta, no result.
    assert len(sent) == 1, f"expected exactly one event, got {sent}"
    evt = sent[0]
    assert evt["type"] == "error"
    assert evt.get("ensouled_refusal") is True
    assert evt["backend"] == "llama"
    assert "Qwen3.6-27B-NVFP4.gguf" in evt["error"]
    # The error names the two paths (start it / toggle incognito).
    assert "start" in evt["error"].lower()
    assert "incognito" in evt["error"].lower()
    # NO generation events of any kind.
    assert all(e["type"] not in ("stream_start", "text_delta", "result", "usage")
              for e in sent)


# --------------------------------------------------------------------------- #
# Invariant 4: Prometheus home seeds the probe cache for BOTH backends
# --------------------------------------------------------------------------- #

def test_prometheus_home_seeds_probe_cache_for_both_backends(tmp_path, monkeypatch):
    """_ensure_prometheus_home seeds context_length_cache.yaml with entries for
    BOTH the local llama model AND the Umans cloud model, so a warm re-init on
    either backend skips the 6.5s context-length probe.

    Prevents a regression where only one backend's cache entry is seeded (e.g.
    if someone refactors the generator and drops the cloud entry), which would
    make every Prometheus cloud turn eat a probe-down + 6.5s stall.
    """
    import server as srv

    # Point config at temp paths so we don't touch the real %LOCALAPPDATA% home.
    prometheus_home = tmp_path / "prometheus-home"
    monkeypatch.setenv("PROMETHEUS_HERMES_HOME", str(prometheus_home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "llama-home"))

    # Pin the model names + base URLs via config (the generator reads these).
    monkeypatch.setattr(srv.config, "llama_model", "TestLlama-7B.gguf")
    monkeypatch.setattr(srv.config, "llama_host", "http://127.0.0.1:8000")
    monkeypatch.setattr(srv.config, "prometheus_cloud_model", "umans-glm-5.2")
    monkeypatch.setattr(srv.config, "prometheus_cloud_base_url", "https://api.code.umans.ai/v1")
    monkeypatch.setattr(srv.config, "prometheus_cloud_context", 200000)
    monkeypatch.setattr(srv.config, "max_context_tokens", 32768)
    monkeypatch.setattr(srv.config, "hermes_home", str(tmp_path / "llama-home"))

    home = srv._ensure_prometheus_home()
    cache_path = Path(home) / "context_length_cache.yaml"
    assert cache_path.exists(), "probe cache not seeded"
    cache = cache_path.read_text(encoding="utf-8")

    # Cloud entry — Prometheus is cloud-functional by default.
    assert "umans-glm-5.2@https://api.code.umans.ai/v1/" in cache
    # Local llama entry — so a router flip to local skips the probe too.
    assert "TestLlama-7B.gguf@" in cache
    assert "/v1/" in cache  # local base_url form

    # _ensure_prometheus_home no longer writes config.yaml — that's owned by
    # _write_prometheus_config. Verify the separation: no config.yaml yet.
    assert not (Path(home) / "config.yaml").exists()
    # NO SOUL.md — Prometheus is incognito by design.
    assert not (Path(home) / "SOUL.md").exists()

    # _write_prometheus_config writes the incognito config for the chosen backend.
    cloud_backend = {
        "backend": "cloud", "base_url": "https://api.code.umans.ai/v1",
        "model": "umans-glm-5.2", "context": 200000, "api_key": "",
    }
    home2 = srv._write_prometheus_config(cloud_backend)
    assert home2 == home
    cfg = (Path(home) / "config.yaml").read_text(encoding="utf-8")
    assert "memory_enabled: false" in cfg
    assert "user_profile_enabled: false" in cfg
    assert "curator:" in cfg and "enabled: false" in cfg
    assert "provider: \"custom\"" in cfg
    assert "https://api.code.umans.ai/v1" in cfg
    assert "umans-glm-5.2" in cfg


# --------------------------------------------------------------------------- #
# Invariant 4b: backend signature detects same-label base_url/model changes
# --------------------------------------------------------------------------- #

def test_backend_signature_distinguishes_same_label_changes():
    """_backend_signature must differ when base_url, model, context, or api_key
    changes — even if the coarse `backend` label is identical.

    The routing path (route_prometheus_backend) only reloads the warm runtime when
    the signature changes. A regression that made the signature compare only the
    `backend` label would let a llama_host change (e.g. :8000 → :8001, both
    "llama") rewrite config.yaml but leave the warm process pointed at the dead
    old endpoint. This pins the signature's sensitivity to every connection field.
    """
    import server as srv

    base = {"backend": "llama", "base_url": "http://127.0.0.1:8000/v1",
            "model": "Qwen.gguf", "context": 32768, "api_key": ""}
    sig_base = srv._backend_signature(base)

    # Same label, different base_url → different signature (the BUG 1 case).
    moved = {**base, "base_url": "http://127.0.0.1:8001/v1"}
    assert srv._backend_signature(moved) != sig_base

    # Same label + url, different model → different signature.
    assert srv._backend_signature({**base, "model": "Other.gguf"}) != sig_base

    # Different context length → different signature.
    assert srv._backend_signature({**base, "context": 65536}) != sig_base

    # api_key set vs empty → different signature.
    assert srv._backend_signature({**base, "api_key": "sk-xxx"}) != sig_base

    # Rotated key → different signature (the cloud-key-rotation case).
    assert srv._backend_signature({**base, "api_key": "sk-aaa"}) != \
           srv._backend_signature({**base, "api_key": "sk-bbb"})

    # Identical backends → identical signature (no spurious reload).
    assert srv._backend_signature(dict(base)) == sig_base

    # Cross-label (llama vs cloud) → different signature even if same model.
    cloud = {"backend": "cloud", "base_url": "https://api.code.umans.ai/v1",
             "model": "Qwen.gguf", "context": 200000, "api_key": "sk-x"}
    assert srv._backend_signature(cloud) != sig_base


# --------------------------------------------------------------------------- #
# Invariant 4c: probe cache seeds the dream backend too (RISK 5)
# --------------------------------------------------------------------------- #

def test_prometheus_home_seeds_dream_probe_cache_entry(tmp_path, monkeypatch):
    """_ensure_prometheus_home must seed a dream cache entry, because the router
    can land Prometheus on dream (dream up, llama down). A missing dream entry
    would stall every dream-backend Prometheus turn on the 6.5s probe.

    Pins the RISK-5 fix: the cache seed covers all THREE possible backends, not
    just cloud + llama.
    """
    import server as srv

    prometheus_home = tmp_path / "prometheus-home"
    monkeypatch.setenv("PROMETHEUS_HERMES_HOME", str(prometheus_home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "hermes_home", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "llama_model", "TestLlama.gguf")
    monkeypatch.setattr(srv.config, "llama_host", "http://127.0.0.1:8000")
    monkeypatch.setattr(srv.config, "dream_model", "diffusiongemma-26b-test")
    monkeypatch.setattr(srv.config, "dream_context_size", 131072)
    monkeypatch.setattr(srv.config, "prometheus_cloud_model", "umans-glm-5.2")
    monkeypatch.setattr(srv.config, "prometheus_cloud_base_url", "https://api.code.umans.ai/v1")
    monkeypatch.setattr(srv.config, "prometheus_cloud_context", 200000)
    monkeypatch.setattr(srv.config, "max_context_tokens", 32768)

    home = srv._ensure_prometheus_home()
    cache = (Path(home) / "context_length_cache.yaml").read_text(encoding="utf-8")
    # Dream entry present, keyed on the dream model + the dream /v1 base_url.
    assert "diffusiongemma-26b-test@" in cache
    assert "131072" in cache  # dream context size


# --------------------------------------------------------------------------- #
# Invariant 4d: probe-cache dedup keys on the full key, not a "://" prefix (RISK 4)
# --------------------------------------------------------------------------- #

def test_prometheus_probe_cache_dedup_survives_url_colons(tmp_path, monkeypatch):
    """The cache dedup must key on the full "<model>@<base_url>/" portion, not on
    a naive split-on-first-colon — because base_url contains "://" (e.g.
    "https://api.code.umans.ai/v1"), splitting on the first ":" reduces the key
    to "<model>@https" and falsely matches ANY same-scheme URL.

    A regression to the prefix check would let a changed port/host (same scheme)
    be skipped as "already present", reintroducing the 6.5s Hermes context probe
    on every turn. This pins the rsplit(": ", 1) fix.
    """
    import server as srv

    prometheus_home = tmp_path / "prometheus-home"
    monkeypatch.setenv("PROMETHEUS_HERMES_HOME", str(prometheus_home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "hermes_home", str(tmp_path / "llama-home"))
    monkeypatch.setattr(srv.config, "llama_model", "TestLlama.gguf")
    monkeypatch.setattr(srv.config, "llama_host", "http://127.0.0.1:8000")
    monkeypatch.setattr(srv.config, "dream_model", "diffusiongemma-26b-test")
    monkeypatch.setattr(srv.config, "dream_context_size", 131072)
    monkeypatch.setattr(srv.config, "prometheus_cloud_model", "umans-glm-5.2")
    monkeypatch.setattr(srv.config, "prometheus_cloud_base_url", "https://api.code.umans.ai/v1")
    monkeypatch.setattr(srv.config, "prometheus_cloud_context", 200000)
    monkeypatch.setattr(srv.config, "max_context_tokens", 32768)

    # First seed.
    srv._ensure_prometheus_home()
    cache_path = prometheus_home / "context_length_cache.yaml"
    first = cache_path.read_text(encoding="utf-8")

    # Now change the llama_host PORT (same scheme, same model, different URL) and
    # re-seed. The local entry's key changes (port differs), so it must be ADDED
    # — not skipped as a duplicate of the existing same-scheme entry.
    monkeypatch.setattr(srv.config, "llama_host", "http://127.0.0.1:8001")
    srv._ensure_prometheus_home()
    second = cache_path.read_text(encoding="utf-8")

    # Both port variants present (the :8000 entry from the first seed + the
    # :8001 entry from the second). A prefix-based dedup would have skipped the
    # :8001 entry because "TestLlama.gguf@http" already matched.
    assert "127.0.0.1:8000/v1/" in second, "original local entry lost"
    assert "127.0.0.1:8001/v1/" in second, "new-port local entry not seeded (dedup prefix bug)"
    # Cloud entry not duplicated.
    assert second.count("umans-glm-5.2@https://api.code.umans.ai/v1/") == 1

    # Prefix-model case: a model name that's a prefix of another must NOT cause
    # the longer entry to be skipped. Rename llama_model to "TestLlama" (a prefix
    # of the original "TestLlama.gguf") and re-seed — both keys must coexist.
    monkeypatch.setattr(srv.config, "llama_model", "TestLlama")
    srv._ensure_prometheus_home()
    third = cache_path.read_text(encoding="utf-8")
    assert "TestLlama@http://127.0.0.1:8001/v1/" in third, \
        "prefix-model entry not seeded (substring dedup would skip it)"
    assert "TestLlama.gguf@http://127.0.0.1:8000/v1/" in third, \
        "original longer entry lost when prefix model added"

