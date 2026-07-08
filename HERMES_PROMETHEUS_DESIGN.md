# Prometheus + Attendant Hermes design

> Agreed 2026-07-07 in a design conversation. Not yet implemented.
> Persistent memory: `project_hermes_prometheus_design.md` (auto-loaded via MEMORY.md).

## Context

Loom currently runs Hermes as one or two held `hermes acp` processes (a llama-backed
soul + a dream-backed soul via `_ensure_dream_hermes_home`). Each held runtime is a
long-lived OS process parked in `_RUNTIMES` (`hermes_client.py:917`) until the server
dies — no idle eviction, no lifecycle binding to the model server underneath it. Two
problems follow: (1) a soul-bearing process can outlive the model it talks to, leaving
a warm ACP pointed at a dead `/v1` endpoint; (2) there's no always-available Hermes
agent when local models are down.

This design splits Hermes' two jobs — "carry a persistent identity" vs. "be an agent
right now" — into two runtime classes with different lifecycles:

- **Attendants** (ensouled, model-bound): the existing llama-soul and dream-soul.
  Bound to their model server's state; cleared when the model stops; re-init on next
  turn after the model returns (soul survives in the home dir; probe cache makes
  re-init cheap).
- **Prometheus** (incognito, always-warm, cloud-fallback): no soul, no memory, no
  SOUL.md. Decoupled from model servers. Always warm. Falls back to the Umans
  OpenAI-compatible cloud endpoint when no local model is up, so it's genuinely
  always-functional.

Per-folder souls (the original isolation idea) are **out of scope** — simplified to
one soul per attendant. The attendant routing is by which local model is up.

---

## Architecture: three Hermes runtimes

| Runtime | Soul | Memory | Lifecycle | Backend | Always functional? |
|---|---|---|---|---|---|
| **Llama attendant** | yes (llama home) | yes | bound to llama-server | llama-server `/v1` | only when llama up |
| **Dream attendant** | yes (dream home) | yes | bound to dream sidecar | dream `/v1` | only when dream up |
| **Prometheus** | no | no | independent (admin-managed) | local `/v1` → Umans cloud fallback | yes |

Runtime cache (`hermes_client.py:917`) holds at most: Prometheus (always) + one
attendant per active model. Bounded, no idle-stop gymnastics needed.

---

## Component design

### Prometheus (incognito, always-warm, cloud-fallback)

**Home generator** — clone of `_ensure_dream_hermes_home` (`server.py:6550-6629`),
call it `_ensure_prometheus_home`. Key differences from the dream generator:

- `memory_enabled: false`, `user_profile_enabled: false` (dream has both `true`)
- `curator.enabled: false` (no memory to curate)
- No `SOUL.md` seed (skip the `server.py:6616-6629` copy-from-base step, or seed an
  empty/incognito SOUL.md)
- `provider: "custom"`, `base_url` set by the router (see below), not a fixed local URL
- Home path: `config.hermes_home.with_name(f"{base.name}-prometheus")` (mirrors dream's
  `-dream` suffix at `server.py:6563`), under `%LOCALAPPDATA%` (off OneDrive — same
  safety as the base home, `config.py:114`)
- Seed `context_length_cache.yaml` for both the local model AND the Umans cloud model
  (probe cache pattern at `server.py:6604-6614`), so warm re-init skips the probe on
  either backend

**Cloud fallback router** — a function that picks Prometheus' `base_url` from liveness:
probe dream `/health` (`admin_server.py:961`) and llama `/v1/models`
(`admin_server.py:436`); if either is up, point Prometheus at that local `/v1`; if
both down, point at `https://api.code.umans.ai/v1` with `UMANS_AI_API_KEY`. Rewrite
Prometheus' `config.yaml` `base_url` + `api_key` + `default` model when the backend
changes, then `session/set_model` (`hermes_client.py:818`) to reload without a full
process restart. Umans is OpenAI-compatible (confirmed: `pi-provider-umans`,
`umans-local-proxy` both proxy `https://api.code.umans.ai/v1`; models `umans-glm-5.2`,
`umans-flash`, `umans-coder`). Hermes' `provider: "custom"` already speaks this shape
(the llama home uses it at `config.yaml:provider: "custom"`).

**Lifecycle** — decoupled. Started once (or on first Prometheus turn), held warm in
`_RUNTIMES` indefinitely. Admin button manages it independently (start/stop/restart/
status). NOT cleared when models stop — that's the point.

### Attendants (ensouled, model-bound)

**Existing** — dream home via `_ensure_dream_hermes_home` (`server.py:6550`), llama
home via `config.hermes_home` (`config.py:112`). Both already wired into dispatch at
`server.py:6701-6720` (dream) and `server.py:7008-7038` (llama). No new home generator
needed.

**New: lifecycle binding to model servers.** When llama-server is stopped
(`admin_server.py:897` "Unload llama weights from VRAM"), also stop the llama
attendant runtime: find the `_RUNTIMES` entry whose `home == config.hermes_home` and
call `runtime.stop()` (`hermes_client.py:885-914`, already Windows-safe via
`taskkill /F /T`). Same for dream: when the dream sidecar stops, stop the dream
attendant. The soul (home dir, state.db, memories) survives; only the warm process is
cleared. On next attendant turn after the model returns, `_RUNTIMES` miss → re-init
(pays spawn + handshake + memory re-read; skips the context-length probe via the cached
`context_length_cache.yaml`).

### Admin surface

**Existing model buttons get attendant-coupling** — the llama stop/unload path
(`admin_server.py:897-906`) and dream stop path gain a "also stop the bound attendant"
step. Start paths don't auto-start the attendant (attendant inits lazily on first turn,
matching current behavior).

**New Hermes-management button** — admin UI section exposing three runtimes:
- **Prometheus**: start / stop / restart / status (full lifecycle, independent)
- **Llama attendant**: check / stop (read-only + force-stop; starts lazily on turn)
- **Dream attendant**: check / stop (same)

Mirrors the existing admin status patterns (`admin_server.py:506-565` Hermes install
probe, `:556-565` llama reachable check). New endpoints under `/api/admin/hermes/`
for the three runtimes.

### UI indicator (Hermes chat spaces)

**Wire the existing liveness probes into the chat-space UI.** The probes already run
(`admin_server.py:2233` "Cheap TCP liveness probes for the dashboard status dots",
`:436` llama, `:961` dream). Surface the result as an up/down indicator on Hermes-only
conversation spaces: "local model up" (green, attendant functional) vs "local model
down" (amber/red, attendant unavailable — Prometheus still functional via cloud). The
indicator reflects the *model* state, which transitively reflects attendant
availability. Prometheus is implicitly always-up (no indicator needed, or a static
"always on" marker).

Transport: the probes' results flow to the browser via the existing WebSocket broadcast
(`server.py:3852` `_ws_send`, `:3900` `_ws_broadcast_all`) — same path status dots use.

### Routing (CONFIRMED)

Conversation-level `incognito` flag (orthogonal to `cc_model`, like `permission_mode`):

- **Incognito conversations** → always use Prometheus (always-functional, no soul).
- **Ensouled conversations** → use the attendant for the conversation's model
  (llama/dream). If that model is down: **Loom-specific error, NO generation, NO
  silent fallback to Prometheus.** The error names the down model and points the user
  to either start it (via admin) or toggle incognito for an always-functional no-soul
  path. This refuses rather than silently de-souling, because silent fallback would
  lose the soul mid-conversation — the exact contamination the design exists to
  prevent.

---

## Backend primitives reused (all exist)

| Primitive | Location | Reuse |
|---|---|---|
| Liveness probes (llama `/v1/models`, dream `/health`) | `admin_server.py:436, :556, :822, :961` | Feed Prometheus router + UI indicator + routing refuse-check |
| Detached admin-managed process spawn | `admin_server.py:773-812` (`_spawn_detached`) | Prometheus admin start |
| Home config generator pattern | `server.py:6550-6629` (`_ensure_dream_hermes_home`) | Clone for `_ensure_prometheus_home` |
| Runtime cache + Windows-safe stop | `hermes_client.py:917, :885-914` | All three runtimes; attendant-clear-on-model-stop |
| Probe cache (skip re-init probe) | `server.py:6604-6614` (`context_length_cache.yaml`) | Prometheus warm re-init on backend switch |
| ACP session/set_model reload | `hermes_client.py:818` | Prometheus backend switch without full restart |
| WebSocket broadcast to browser | `server.py:3852, :3900` | UI indicator transport |
| Umans OpenAI-compatible endpoint | `https://api.code.umans.ai/v1`, `UMANS_AI_API_KEY` | Prometheus cloud fallback |

## New surface to build

1. `_ensure_prometheus_home()` — incognito home generator (clone of dream generator)
2. Prometheus cloud-fallback router (liveness → base_url, rewrite config + `set_model`)
3. Attendant-clear-on-model-stop binding (llama stop path + dream stop path)
4. Admin Hermes-management endpoints + UI button (three runtimes)
5. UI indicator wire (probe → WS → Hermes chat-space header)
6. Conversation-level `incognito` flag (routing: Prometheus vs attendant)
7. Ensouled-refuses-when-down check + Loom-specific error message at turn dispatch

## Verification

**End-to-end smoke:**
1. Start llama-server via admin → send an ensouled Hermes turn → confirm it routes to
   the llama attendant and streams. Stop llama-server via admin → confirm the llama
   attendant is cleared from `_RUNTIMES` (check via admin Hermes-status endpoint).
2. With no local model up → send a Prometheus (incognito) turn → confirm it falls back
   to Umans cloud and streams. Confirm Prometheus stays warm across a llama
   start/stop cycle (same process, no re-init).
3. Admin Hermes-management button: stop Prometheus → confirm it's gone from
   `_RUNTIMES`; restart → confirm it re-inits and skips the context-length probe
   (check `context_length_cache.yaml` has the Umans entry).
4. UI indicator: with llama up, Hermes chat-space shows green; stop llama → indicator
   turns amber within probe interval.
5. **Ensouled-refuse:** with llama down, send an ensouled (non-incognito) Hermes turn →
   confirm a Loom-specific error (no generation, no Prometheus fallback), naming the
   down model and offering the two paths.

**Lifecycle invariant tests** (the "prevention" ask — this coupling is invisible until
it breaks; pin it):
```
def test_stop_llama_server_clears_llama_attendant_runtime():
    # Assert: after the llama unload path runs, _RUNTIMES contains no entry
    # whose home == config.hermes_home. Prevents a future change from silently
    # unbinding the attendant-clear and leaving a soul process against a dead model.

def test_stop_llama_does_not_clear_prometheus_or_dream():
    # Inverse: decoupling invariant. Stopping llama leaves Prometheus + dream
    # attendant untouched.

def test_ensouled_turn_refuses_when_model_down():
    # Assert: an ensouled (non-incognito) Hermes turn with the model down returns
    # a Loom-specific error and emits NO stream events. Prevents silent de-souling
    # via auto-fallback regressing back in.
```

**Probe-cache test:** after first Prometheus cloud turn, `context_length_cache.yaml`
in the prometheus home contains a `umans-glm-5.2@https://api.code.umans.ai/v1/` entry;
a forced Prometheus restart skips the probe (no probe-down log line in agent.log).

## Notes

- Orphaned homes (a deleted project's soul dir) are accepted — out of scope, per
  decision. "rip that soul tho."
- The attendant-clear-on-model-stop coupling is load-bearing on VRAM-toggling
  frequency: each llama stop/start cycle loses the attendant's warm context. Expected
  and correct — the soul survives, only the warm process is lost. Probe cache makes
  re-init skip the expensive part.
- Umans vision caveat: `umans-glm-5.2` vision runs server-side on the Anthropic
  Messages API, not the OpenAI endpoint (per `pi-provider-umans` docs). Prometheus
  via the OpenAI endpoint gets `umans-glm-5.2` text-only. Acceptable for an incognito
  agent; flag if vision matters.
- Repo is OneDrive-synced — keep all souls under `%LOCALAPPDATA%` (config.py:114
  precedent), NOT in project folders, to avoid state.db sync corruption.
- Run with `C:\Python314\python.exe` (not bare `python`).
- Windows: every subprocess spawn needs `CREATE_NO_WINDOW|CREATE_NEW_PROCESS_GROUP`
  (pattern at `hermes_client.py:635-637`); kills need `taskkill /F /T`
  (`hermes_client.py:900`).
