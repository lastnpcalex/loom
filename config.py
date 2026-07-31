"""Configuration for RP Harness (Loom)."""

from dataclasses import dataclass, field
import os
import json


def _envbool(name: str, default: bool) -> bool:
    """Parse a truthy env var. bool('False') is True in Python — don't use it."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "t", "y")


# Keys persisted to config.json. Centralized so to_dict / update_from_dict /
# load all stay in sync without three places to edit.
_PERSISTED_KEYS = (
    "llama_host", "llama_model", "llama_server_exe", "llama_models_dir",
    "llama_chat_template_file",
    "vision_model",
    "umans_model",
    "max_context_tokens", "verbatim_window",
    "temperature", "top_p", "max_tokens", "repeat_penalty",
    "db_path",
    # Dream Hermes — DiffusionGemma GPU orchestrator sidecar (nuspy OpenAI server).
    # dream_host is the OpenAI-compatible endpoint; dream_cwd is the nuspy repo dir
    # (holds config.json + models/ + .temp/). dream_idle_timeout_min is disabled
    # by default; manual unload is safer than killing long cold-start jobs.
    "dream_host", "dream_model", "dream_cwd", "dream_server_exe",
    "dream_model_path", "dream_context_size", "dream_diffusion_steps",
    "dream_cuda_mmq_max_x", "dream_gpu_layers", "dream_fit_target_mb", "dream_no_mmap", "dream_flash_attn", "dream_cache_type_k",
    "dream_cache_type_v", "dream_swa_full", "dream_idle_timeout_min",
)
_HOST_KEYS = ("llama_host", "dream_host")


def _normalize_host(val: str) -> str:
    """http:// prefix + pin localhost → 127.0.0.1.

    Windows resolves "localhost" IPv6-first and both local model servers
    (llama-server and the Dream sidecar) bind IPv4-only, so every fresh TCP
    connect via "localhost" eats a ~2s ::1 fallback before reaching them.
    """
    if val and not val.startswith(("http://", "https://")):
        val = f"http://{val}"
    return val.replace("//localhost", "//127.0.0.1") if val else val


@dataclass
class Config:
    # Llama Server connection
    llama_host: str = os.getenv("LLAMA_HOST", "http://localhost:8000")
    llama_model: str = os.getenv("LLAMA_MODEL", "Qwen3.6-27B-NVFP4.gguf")
    vision_model: str = os.getenv("VISION_MODEL", "")  # for image description; empty = use llama_model

    # Umans AI connection (remote, Anthropic/OpenAI-compatible endpoint)
    umans_model: str = os.getenv("UMANS_MODEL", "umans-coder")

    # Prometheus cloud fallback — the always-warm incognito Hermes runtime falls
    # back to the Umans OpenAI-compatible endpoint when no local model is up. These
    # are env-only (install-specific, like hermes_home) so they stay out of the
    # persisted config.json. The key env var is UMANS_API_KEY (same one claude_client
    # reads at :893 for the Anthropic-format path); Prometheus uses the OpenAI path.
    prometheus_cloud_base_url: str = os.getenv(
        "UMANS_BASE_URL", "https://api.code.umans.ai/v1"
    )
    prometheus_cloud_model: str = os.getenv("UMANS_MODEL", "umans-glm-5.2")
    prometheus_cloud_context: int = int(os.getenv("PROMETHEUS_CLOUD_CONTEXT", "200000"))

    # Llama Server binary and models directory
    llama_server_exe: str = os.getenv(
        "LLAMA_SERVER_EXE",
        "llama-server",
    )
    llama_models_dir: str = os.getenv("LLAMA_MODELS_DIR", r"C:\LlamaServer\models")
    llama_chat_template_file: str = os.getenv(
        "LLAMA_CHAT_TEMPLATE_FILE",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "templates",
            "qwen3.6-froggeric-v20-chat_template.jinja",
        ),
    )

    # Context budget
    max_context_tokens: int = 32768
    verbatim_window: int = 6  # last N turns kept verbatim
    summary_target_tokens: int = 800
    summary_temperature: float = 0.3

    # Style nudge rotation
    nudge_rotation_interval: int = 3  # turns between style changes

    # Repetition detection thresholds
    ngram_repeat_threshold: int = 2  # appearances across last N messages
    ngram_lookback: int = 6  # how many assistant messages to scan
    overused_word_multiplier: float = 3.0

    # Server
    host: str = "0.0.0.0"
    port: int = int(os.getenv("LOOM_PORT", "3000"))

    # SSL
    ssl_certfile: str = os.getenv("LOOM_SSL_CERT", "certs/cert.pem")
    ssl_keyfile: str = os.getenv("LOOM_SSL_KEY", "certs/key.pem")

    # Paths
    db_path: str = os.getenv("LOOM_DB", "loom.db")
    upload_dir: str = "uploads"
    characters_dir: str = "characters"

    # Generation defaults
    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 16384
    repeat_penalty: float = 1.08

    # --- Hermes Agent (ACP mode) — native Windows install ---
    # NousResearch's install.ps1 (and our manual `uv pip install -e .[acp]`)
    # puts Hermes under %LOCALAPPDATA%\hermes; config.yaml / .env live there.
    # Not persisted to config.json — these are install-specific, set via env.
    hermes_home: str = os.getenv(
        "HERMES_HOME",
        os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hermes"),
    )
    # Explicit path to the `hermes` CLI executable. Empty -> derive from hermes_home.
    hermes_exe: str = os.getenv("HERMES_EXE", "")
    # Master switch for Hermes mode. The Phase-4 UI stays hidden until this is on.
    enable_hermes: bool = _envbool("LOOM_ENABLE_HERMES", False)

    # --- Dream Hermes (DiffusionGemma GPU orchestrator sidecar) ---
    # Runs the nuspy OpenAI adapter (agent.openai_server) on a diffusion-capable
    # llama.cpp fork (PR #24423). The sidecar JIT-loads the NVFP4 GGUF into VRAM on
    # first request. enable_dream is env-only like enable_hermes. Automatic idle
    # unload is disabled by default because it can surprise chat generations.
    # Default 127.0.0.1 (IPv4) + 8787 (the live sidecar port). The legacy
    # "http://localhost:18081" default was wrong on both axes: 18081 is a dead
    # port, and "localhost" resolves IPv6-first on Windows while the sidecar
    # listens on IPv4 only — each request ate a ~2s ::1 connect fallback.
    # See _chat_host_for_model (llama_client.py) + _dream_openai_base_url
    # (server.py) which both rewrite localhost->127.0.0.1 as a belt-and-braces
    # measure, but the source default should be correct too.
    dream_host: str = os.getenv("DREAM_HOST", "http://127.0.0.1:8787")
    dream_model: str = os.getenv("DREAM_MODEL", "diffusiongemma-26b-a4b-it-nvfp4")
    dream_cwd: str = os.getenv(
        "DREAM_CWD",
        r"C:\tmp\llama-diffusion-gemma-pr",
    )
    dream_server_exe: str = os.getenv(
        "DREAM_SERVER_EXE",
        r"C:\tmp\llama-diffusion-gemma-pr\build\bin\llama-diffusion-gemma-server.exe",
    )
    dream_model_path: str = os.getenv(
        "DREAM_MODEL_PATH",
        r"C:\Users\exast\OneDrive\Documents\Loom-Projects\llama-diffusion\models\diffusiongemma-26b-a4b-it-nvfp4.gguf",
    )
    dream_context_size: int = int(os.getenv("DREAM_CONTEXT_SIZE", "131072"))
    dream_diffusion_steps: int = int(os.getenv("DREAM_DIFFUSION_STEPS", "48"))
    dream_cuda_mmq_max_x: int = int(os.getenv("DREAM_CUDA_MMQ_MAX_X", "64"))
    dream_gpu_layers: int = int(os.getenv("DREAM_GPU_LAYERS", "-1"))
    dream_fit_target_mb: int = int(os.getenv("DREAM_FIT_TARGET_MB", "0"))
    dream_no_mmap: bool = _envbool("DREAM_NO_MMAP", False)
    dream_flash_attn: str = os.getenv("DREAM_FLASH_ATTN", "on")
    dream_cache_type_k: str = os.getenv("DREAM_CACHE_TYPE_K", "q8_0")
    dream_cache_type_v: str = os.getenv("DREAM_CACHE_TYPE_V", "q8_0")
    dream_swa_full: bool = _envbool("DREAM_SWA_FULL", False)
    dream_enable_thinking: bool = _envbool("DREAM_ENABLE_THINKING", True)
    dream_thinking_min_tokens: int = int(os.getenv("DREAM_THINKING_MIN_TOKENS", "4096"))
    dream_idle_timeout_min: int = int(os.getenv("DREAM_IDLE_TIMEOUT_MIN", "0"))
    enable_dream: bool = _envbool("LOOM_ENABLE_DREAM", False)

    def hermes_executable(self) -> str:
        """Resolve the `hermes` CLI path: explicit HERMES_EXE if set, else the
        venv Scripts binary under hermes_home, else bare 'hermes' on PATH."""
        if self.hermes_exe:
            return self.hermes_exe
        exe_name = "hermes.exe" if os.name == "nt" else "hermes"
        cand = os.path.join(
            self.hermes_home, "hermes-agent", ".venv",
            "Scripts" if os.name == "nt" else "bin", exe_name,
        )
        return cand if os.path.exists(cand) else "hermes"

    def llama_host_url(self) -> str:
        """Return the Llama Server host URL with http:// prefix."""
        return _normalize_host(self.llama_host)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in _PERSISTED_KEYS}

    def update_from_dict(self, d: dict):
        for key in _PERSISTED_KEYS:
            if key in d:
                cur = getattr(self, key)
                # Booleans need explicit handling — bool('False') == True.
                if isinstance(cur, bool):
                    val = d[key]
                    if isinstance(val, str):
                        val = val.strip().lower() in ("1", "true", "yes", "on", "t", "y")
                    else:
                        val = bool(val)
                else:
                    val = type(cur)(d[key])
                if key in _HOST_KEYS:
                    val = _normalize_host(val)
                setattr(self, key, val)
        self.save()

    def save(self):
        """Persist user-editable settings to config.json."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception as e:
            print(f"[CONFIG] Failed to save config.json: {e}")

    def load(self):
        """Load saved settings from config.json if it exists."""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key in _PERSISTED_KEYS:
                if key not in saved:
                    continue
                cur = getattr(self, key)
                if isinstance(cur, bool):
                    val = saved[key]
                    if isinstance(val, str):
                        val = val.strip().lower() in ("1", "true", "yes", "on", "t", "y")
                    else:
                        val = bool(val)
                else:
                    val = type(cur)(saved[key])
                if key in _HOST_KEYS:
                    val = _normalize_host(val)
                setattr(self, key, val)
            print(f"[CONFIG] Loaded settings from config.json")
        except FileNotFoundError:
            pass  # No saved config yet, use defaults
        except Exception as e:
            print(f"[CONFIG] Failed to load config.json: {e}")


config = Config()
config.load()
