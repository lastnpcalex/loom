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
    "ollama_host", "ollama_model", "vision_model",
    "local_backend", "vllm_host", "vllm_model",
    "max_context_tokens", "verbatim_window",
    "temperature", "top_p", "max_tokens", "repeat_penalty",
    # Ollama tuning
    "ollama_kv_cache_type", "ollama_flash_attention", "ollama_keep_alive",
    "ollama_num_parallel", "ollama_max_loaded_models", "ollama_context_length",
    # vLLM tuning
    "vllm_quantization", "vllm_kv_cache_dtype", "vllm_max_model_len",
    "vllm_gpu_memory_utilization", "vllm_max_num_seqs",
    "vllm_tensor_parallel_size", "vllm_tool_call_parser",
    "vllm_enable_auto_tool_choice", "vllm_extra_args",
    "vllm_speculative_config", "vllm_text_only", "vllm_reasoning_parser",
    "vllm_python_path", "vllm_served_name", "vllm_thinking_default",
)
_HOST_KEYS = ("ollama_host", "vllm_host")


@dataclass
class Config:
    # Ollama connection
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
    vision_model: str = os.getenv("VISION_MODEL", "")  # small/fast model for image description; empty = use ollama_model

    # Local-model backend: "ollama" (default) or "vllm". Selects which client
    # the Weave / OODA paths use. Loom/Braid (CC) are unaffected.
    local_backend: str = os.getenv("LOOM_LOCAL_BACKEND", "vllm")
    vllm_host: str = os.getenv("VLLM_HOST", "http://localhost:8000")
    vllm_model: str = os.getenv("VLLM_MODEL", "")  # empty = fall back to ollama_model

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
    max_tokens: int = 1024
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

    # Ollama runtime tuning — applied as env vars when admin_server spawns Ollama.
    # Env vars override these defaults so CLI users can still pin behavior.
    ollama_kv_cache_type: str = os.getenv("OLLAMA_KV_CACHE_TYPE", "q8_0")  # f16 | q8_0 | q4_0
    ollama_flash_attention: bool = _envbool("OLLAMA_FLASH_ATTENTION", True)
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
    ollama_num_parallel: int = int(os.getenv("OLLAMA_NUM_PARALLEL", "1"))
    ollama_max_loaded_models: int = int(os.getenv("OLLAMA_MAX_LOADED_MODELS", "0"))  # 0 = auto
    ollama_context_length: int = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "128000"))

    # vLLM launch tuning — built into CLI flags when admin_server spawns vLLM.
    vllm_quantization: str = os.getenv("VLLM_QUANTIZATION", "modelopt")  # none|awq|gptq|fp8|modelopt|nvfp4
    vllm_kv_cache_dtype: str = os.getenv("VLLM_KV_CACHE_DTYPE", "fp8")    # auto|fp8|fp8_e5m2
    vllm_max_model_len: int = int(os.getenv("VLLM_MAX_MODEL_LEN", "32768"))
    vllm_gpu_memory_utilization: float = float(os.getenv("VLLM_GPU_MEMORY_UTILIZATION", "0.92"))
    vllm_max_num_seqs: int = int(os.getenv("VLLM_MAX_NUM_SEQS", "16"))
    vllm_tensor_parallel_size: int = int(os.getenv("VLLM_TENSOR_PARALLEL_SIZE", "1"))
    vllm_tool_call_parser: str = os.getenv("VLLM_TOOL_CALL_PARSER", "hermes")  # hermes|mistral|llama3_json|none
    vllm_enable_auto_tool_choice: bool = _envbool("VLLM_ENABLE_AUTO_TOOL_CHOICE", True)
    vllm_extra_args: str = os.getenv("VLLM_EXTRA_ARGS_EXTRA", "")  # escape hatch for unmodeled flags
    # MTP / speculative decoding — JSON string passed to --speculative-config.
    # Example for Qwen3.6 MTP: {"method":"qwen3_next_mtp","num_speculative_tokens":2}
    vllm_speculative_config: str = os.getenv("VLLM_SPECULATIVE_CONFIG", "")
    # When True, vLLM serves a text-only model (e.g., the MTP-tuned variant)
    # and image describes get routed back to Ollama instead of the active backend.
    vllm_text_only: bool = _envbool("VLLM_TEXT_ONLY", False)
    # Reasoning parser for Qwen3.6 thinking/tool blocks. Required for MTP+thinking.
    vllm_reasoning_parser: str = os.getenv("VLLM_REASONING_PARSER", "qwen3")
    # Path to the venv-installed vllm executable. Empty = use system PATH.
    vllm_python_path: str = os.getenv("VLLM_PYTHON_PATH", "")
    # Short alias vLLM exposes alongside the full model id, used as the
    # Loom/Braid model picker entry that routes Claude Code at vLLM. Must start
    # with "vllm-" — server.py's dispatcher uses that prefix to detect the
    # engine. Default "vllm-local"; users can rename per-model (e.g. "vllm-qwen").
    vllm_served_name: str = os.getenv("VLLM_SERVED_NAME", "vllm-local")
    # Default value for the chat template's `enable_thinking` flag. ON by
    # default to match Qwen3.6's training distribution — its MTP draft was
    # trained with thinking enabled, and the model's quality on multi-step
    # tasks (and RP with state) leans on the reasoning scratch space. Flip
    # OFF when you specifically want fast bare-text responses.
    vllm_thinking_default: bool = _envbool("VLLM_THINKING_DEFAULT", True)

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
                if key in _HOST_KEYS and val and not val.startswith(("http://", "https://")):
                    val = f"http://{val}"
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
                if key in _HOST_KEYS and val and not val.startswith(("http://", "https://")):
                    val = f"http://{val}"
                setattr(self, key, val)
            print(f"[CONFIG] Loaded settings from config.json")
        except FileNotFoundError:
            pass  # No saved config yet, use defaults
        except Exception as e:
            print(f"[CONFIG] Failed to load config.json: {e}")


config = Config()
config.load()
