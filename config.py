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
    "max_context_tokens", "verbatim_window",
    "temperature", "top_p", "max_tokens", "repeat_penalty",
    "db_path",
)
_HOST_KEYS = ("llama_host",)


@dataclass
class Config:
    # Llama Server connection
    llama_host: str = os.getenv("LLAMA_HOST", "http://localhost:8000")
    llama_model: str = os.getenv("LLAMA_MODEL", "Qwen3.6-27B-NVFP4.gguf")
    vision_model: str = os.getenv("VISION_MODEL", "")  # for image description; empty = use llama_model

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
        host = self.llama_host
        if host and not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host

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
