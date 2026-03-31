"""Configuration for RP Harness (Loom)."""

from dataclasses import dataclass, field
import os
import json


@dataclass
class Config:
    # Ollama connection
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")

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

    def to_dict(self) -> dict:
        return {
            "ollama_host": self.ollama_host,
            "ollama_model": self.ollama_model,
            "max_context_tokens": self.max_context_tokens,
            "verbatim_window": self.verbatim_window,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "repeat_penalty": self.repeat_penalty,
        }

    def update_from_dict(self, d: dict):
        for key in ("ollama_host", "ollama_model", "max_context_tokens",
                     "verbatim_window", "temperature", "top_p",
                     "max_tokens", "repeat_penalty"):
            if key in d:
                val = type(getattr(self, key))(d[key])
                # Ensure ollama_host always has a protocol
                if key == "ollama_host" and val and not val.startswith(("http://", "https://")):
                    val = f"http://{val}"
                setattr(self, key, val)


config = Config()
