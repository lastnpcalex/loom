from server import _build_anthropic_model_dropdowns
from model_context import is_1m_anthropic, is_anthropic


def test_anthropic_refresh_keeps_fable_pinned_model():
    auto_models, pinned_models, by_family = _build_anthropic_model_dropdowns([
        {"id": "claude-fable-5", "display_name": "Claude Fable 5"},
        {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8"},
        {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
    ])

    assert "fable" in by_family
    assert [m["value"] for m in pinned_models][:2] == [
        "claude-fable-5",
        "claude-opus-4-8",
    ]
    assert {"value": "claude-fable-5", "label": "Fable 5"} in pinned_models
    assert {"value": "fable", "label": "Fable (latest)"} not in auto_models


def test_fable_routes_as_anthropic_1m_model():
    assert is_anthropic("claude-fable-5")
    assert is_1m_anthropic("claude-fable-5")
